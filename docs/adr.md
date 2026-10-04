# 设计取舍索引（ADR）

> 本文是把散落在各设计笔记里的**关键决策**收敛成一张可扫读的索引。每条一行：
> 决策 · 选了 · 没选 · 为什么 · 代价。完整推理见各自源文档，不在这里展开。

## 记忆系统

源：`docs/memory-design-notes.md`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| 判重机制 | 全量 LLM judge（flash，三分类 duplicate/conflict/new） | cosine 阈值 / reranker | 实测三类样本在 0.86~0.95 重叠，任何廉价阈值都误杀 | 每条记忆一次 LLM 调用（无 budget，见代价项） |
| 廉价信号定位 | 只召回，不判重 | 快速判重短路 | 「召回归召回，判重归 judge」——廉价信号分不清同义/换值/同模板不同主体 | 召回漏 target 时 judge 判错（靠 BM25 bigram 捞实体缓解） |
| 冲突处理 | 版本化退役（写新行 + 旧行 `superseded_by`） | 原地 UPDATE / DELETE | 保留历史、可回滚、能答「之前是什么」 | 表随更新膨胀（500/用户上限兜底） |
| 一致性强度 | 宁可重复，不丢记忆（fail-open） | DB 唯一约束 + 层 2 fencing 强一致 | 强一致实现太重、对记忆场景不成比例；丢一条不可恢复 | 极端并发下可能写重一条 |
| 召回方式 | 混合召回（向量 ∪ BM25/IDF bigram） | 纯向量 / 纯词法 | BM25 bigram 能捞实体（户0/j3），防值词干扰把 target 挤出 top-k | 口语化无实体编号的记忆仍难召回 |
| 思维链 prompt | 回退（无 CoT） | 两步思维链 | 无 benchmark 证据、加 token | — |

## 沙箱

源：`docs/cube-sandbox-design-notes.md`、`README.md`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| 隔离形态 | CubeSandbox microVM（云端默认） | Docker（本地） | 多租户恶意负载需要 VM 级隔离；docker 是「防失误」不是「防恶意」 | 依赖独立平台（见部署） |
| 容器生命周期 | 会话级复用池（懒加载 + LRU + 空闲 TTL） | 每次调用新建 | 冷启动 ~0.1s vs 每次付完整生命周期；纯聊天回合零 VM | 进程内 dict 状态，多副本不共享 |
| workspace 位置 | 在 VM 内 `/workspace`（tar 一次性同步） | 每次调用同步 | 会话内 read/write/edit 走 VM 文件系统，无 per-call 开销 | >100MB workspace 加载被拒 |
| 命令超时 | GNU `timeout` 在 VM 内 SIGTERM | SDK 连接超时 | 进程被 timeout(1) 杀掉、无孤儿；exit 124 即超时 | 后台长任务需 `setsid nohup` + 轮询绕过 |
| SDK 连接超时 | 对齐工具 timeout +30s 缓冲 | e2b 默认 60s | 默认 60s 会先于工具 timeout 掐断长命令（pip install 被杀 bug） | — |

## Run 持久化

源：`docs/run-durability-design.md`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| 落库粒度 | write-ahead + 逐轮增量落库 | 全量一次性 | 硬崩溃丢失窗口 ≤ 当前轮，用户「继续」可续跑 | 中间态可能留残缺消息（已由 `_repair_tool_sequence` 兜底） |
| checkpoint/resume | 降级为可选增强 | 完整 run 状态机 | 按主流产品形态非必需，暂缓 | 断连无法无缝 resume 到同一进程 |

## RAG

源：`src/pi/rag/`、`docs/production-deployment.md`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| schema 自举 | server 也跑幂等 `CREATE TABLE IF NOT EXISTS` | 纯依赖 alembic | 全新库漏跑 0008 会静默 500（lazy 建表被跳过） | 与 alembic 双轨并存（技术债） |
| 检索 | 向量 + BM25 词法混合 | 纯向量 | 词法兜底、向量语义，互补 | — |

## LLM 层

源：`src/pi/llm/fallback.py`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| 容错 | fallback 链（每模型 2 次重试 + 指数退避 + 模型链降级） | 单模型直连 | 单厂商并发上限 / 429 / 5xx 需要自动切换 | 增加了模型链配置复杂度 |
| 代理 | `trust_env=False` | 继承环境代理 | 环境里的死代理/SOCKS 会劫持模型流量 | — |

## 安全

源：`docs/hardening-roadmap.md`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| 高危操作 | 计划 B1 HITL（人工确认） | 全自动 | 高危副作用需人把关 | 交互复杂、依赖 checkpoint/resume |
| 沙箱逃逸验证 | B2 逃逸用例进 CI | 只测功能 | 当前 521 用例全绿但无一真实逃逸尝试 | — |
| 工具执行隔离 | 默认 fail-closed（`PI_SANDBOX` 未知值启动即拒） | 静默回退 app 进程内 | 曾回退到继承 `PI_JWT_SECRET` 的进程内执行 | — |

## 部署 / 架构

源：`docs/hardening-roadmap.md` B4、`ROADMAP.md`

| 决策 | 选了 | 没选 | 为什么 | 代价 |
|---|---|---|---|---|
| 部署形态 | 单机单进程（asyncio + 会话锁） | 多副本/多 worker | 沙箱池是进程内 dict、全局信号量是进程内；单节点足够 | 横向扩展需先外置沙箱池状态 |
| CubeSandbox 平台 | 独立安装包（13 个 systemd 服务） | 打进 docker compose | 平台是 vendor 发布物，与 app 解耦 | compose 只能覆盖 app + 中间件 |
| SSE 断连 | 客户端重连 + 增量落库续跑 | broker（Redis pub/sub） | 单机够用；asyncio 长连接不是瓶颈 | 无无缝 resume |

## 已知边界（诚实标注，非缺陷）

- 冷 CLI 沙箱（`PI_SANDBOX_POOL=0`）超时只杀 client、容器继续跑（README 自承，待修）。
- Docker 沙箱是「防失误」不是「防恶意」，多租户必走 microVM。
- Memory Judge 无成本预算（每条记忆一次 LLM，大用户可能反超 chat 消耗）。
- 工具结果无 prompt 注入防御（间接注入风险）。
- 无 Run 取消 API。
