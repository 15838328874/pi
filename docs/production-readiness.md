# 沙箱层生产就绪审计报告（Production Readiness Review）

> 审计方式：不是口头 checklist，而是**真机故障注入探针** —— 在真实 CubeSandbox
> 沙箱上主动制造超时、沙箱假死、超大工作区上传等故障，验证系统行为。
> 结论：核心链路就绪，本轮修复 7 个真实缺口，剩余 4 项为已知限制/建议。

---

## 一、已就绪项（带证据）

| 线上维度 | 状态 | 证据 |
|---|---|---|
| 命令超时 | ✅ 本次修复后可靠 | `sleep 30` timeout=3 → 3.2s 返回，`timed_out=True`，消息"command timed out after 3s" |
| 超时进程清理 | ✅ 零残留 | 超时后 `pgrep -c sleep` = 0（沙箱内 GNU timeout 负责收尸） |
| 沙箱中途死亡（平台回收/失联） | ✅ 降级干净 | 外部 DELETE 沙箱后：run 返回 `Error: cube sandbox failed (SandboxException)`，close 的 save 失败仅记 warning、kill 继续，turn 不悬挂 |
| 平台资源耗尽（no more resource） | ✅ 快速失败 + 不泄漏 | SDK 直连实测返回 `SandboxException 500`；服务端每 turn finally close → 资源不累积 |
| 并发上限与平台容量匹配 | ✅ | semaphore=8（PI_MAX_CONCURRENT_RUNS），实测平台安全并发 6-8、上限 ~12 |
| turn 总超时 | ✅ | run_timeout=600s（PI_RUN_TIMEOUT_SECONDS），超时事件化 |
| VM 泄漏防线（三层） | ✅ | ① turn finally close ② 平台 600s 空闲回收 ③ close kill 失败现在有 warning 日志 |
| 越界防护（两层） | ✅ | policy 声明式 + fs 数据通路（见设计笔记 §3） |
| 异常归一 | ✅ | run 一律 CommandResult；工具一律 ToolResult(is_error)；清理一律不抛 |
| 会话闭环归档 | ✅ | 9 turns 实测，diff 精确（设计笔记 §9） |
| 会话/用户并发锁 | ✅ 部分 | per-session Redis 锁正确；但 per-user 工作区跨会话互斥缺失（见 §三-3） |

## 二、本轮修复的 7 个真实缺口

1. **`timed_out` 语义失效**（原实现依赖 e2b 内部超时异常名，实际不触发）→ 改为
   沙箱内 GNU `timeout {n}s bash -lc '<cmd>'` 包装：超时语义由退出码 124 识别，
   进程由沙箱内 timeout 杀死（零残留），SDK 正常返回。实测 3s 超时 3.2s 触发。
2. **非零退出码被吞成 -1**（e2b `CommandExitException` 带真实 exit_code，原
   except 一律 -1 且把超时 124 也丢掉）→ except 提取 `exit_code`：1/2 等真实
   返回，124 转 `timed_out=True`。模型现在能看到"命令以 exit 1 失败"与
   "命令超时"的区别。
3. **超大工作区装载把 413 屏成技术错误**（envd files API 拒绝 >10MB 上传，
   HTML 413 作为 SandboxException 直接甩给模型）→ 装载前检查 tar 体积，
   >10MB 报可操作错误（"workspace too large ... clean venv/build/cache"）。
4. **close 的 kill 失败静默 pass**（违反"清理必留日志"原则，且是 VM 泄漏事故
   的帮凶）→ 改 warning + exc_info + `pi_sandbox_close_failures_total` 计数。
5. **入参容错**：run() 的 cwd 若传 str 会 `'str' object has no attribute
   'resolve'` → 入口统一 `Path(cwd)`（协议标注 Path，但弱类型入参一律兼容）。
6. **close/save 无总超时**（turn 结束可被慢 tar 拖住）→ finally 里
   `wait_for(to_thread(close), timeout=90)`，超时则 turn 先走、清理线程继续
   收尾（VM 必死）。`PI_SANDBOX_CLOSE_TIMEOUT_SECONDS` 可配。
7. **沙箱健康指标缺失** → Metrics 增 4 系列（创建耗时直方图 + 创建失败/命令
   超时/close 失败计数），runner 装配时注入 `runner.metrics`，standalone 无
   metrics 时全部 no-op（鸭子类型，零耦合）。

## 三、已知限制 / 待办（分级）

| 级别 | 项 | 说明 |
|---|---|---|
| ⚠️ 中 | **同用户多会话并发写同一工作区无互斥** | **决策：不修**（多并发是用户自己的选择，工作区共享语义保留；每会话有独立锁防自身重复提交） |
| ✅ 已修 | **close/save 无总超时** | finally 里 `wait_for(to_thread(close), timeout=90)`（`PI_SANDBOX_CLOSE_TIMEOUT_SECONDS` 可配）：超时后 turn 继续，清理线程继续跑完 save+kill，VM 必死、turn 不被 tar 拖住 |
| ✅ 已修 | **沙箱指标缺失** | `/metrics` 新增 4 系列：`pi_sandbox_create_failures_total` / `pi_sandbox_command_timeouts_total` / `pi_sandbox_close_failures_total` / `pi_sandbox_create_duration_seconds`；真实任务实测 count 观测成功（创建耗时、失败、超时、close 失败四路告警素材齐了） |
| 🟡 低 | **后台进程继承 stdout 会耗尽连接 deadline** | `(sleep 300 &)` 这类写法被 e2b 判定为流未结束 → TimeoutException。属 e2b 语义；工业惯例：后台任务须重定向 stdout/stderr。可选：系统提示中提醒模型 |
| 🟡 低 | **平台 CLI 视图陈旧** | `cubemastercli list` 与 3000 API 不同步，运维排查要以 API/服务日志为准（已记入设计笔记） |
| 🟡 低 | **出网白名单未落地** | 模板级 network 配置不强制，真正断网需宿主层（待办） |
| ⬜ 建议 | eval 任务库扩到 10+ | 当前 5 任务可作为回归基线；扩库后可做正式跑分 |

## 四、回归保障（修复后全绿）

- LocalFS 工具回归 13/13 ✅
- SandboxFS 真实沙箱组合 11/11 ✅（越界拒绝 / close 回传 / 写后 bash 可见）
- standalone（会话级工作区 + 快照回滚）✅
- 真实模型端到端冒烟 ✅（29s / 4 工具 / 断言全过）
- 故障注入探针 `/tmp/prod_probe.py` ✅（超时 / 残留下 / 沙箱假死 / 413 —— 可重复执行）