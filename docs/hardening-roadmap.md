# 安全硬化路线图（B1–B5）

> 来源：一次外部评审的对照核查，把"能立即改的 A 类"与"需要慢慢改的 B 类"分开。
> A 类（文档漂移、参数接线、路径沙箱递归、审计留存期、成本上限、多厂商文档）已落地；
> 本文只记 B 类演进计划，按优先级排列。B2/B4/B5 见 §7 的 FAQ。

**一条贯穿全部 B 项的总原则**：审批/身份/审计/集群都不许削弱沙箱这个唯一硬边界。
任何 B 项做完，若让"越界变成可确认的"，就是做错了方向。

---

## B1 高危操作人工确认（HITL）

**验收不变量**（这是 B1 成败的判据，不是 UI 约定，是系统保证）：

> 用户确认只能批准「沙箱内、自己名下的破坏性操作」。任何越界/越权，在**边界层**
> （sandbox/cwd/ACL）就已经被 deny，确认按钮根本没有机会出现。确认授权的是**意图**，
> 从来不是**越界**。

**关键设计决策**（对照 Claude Code / Codex / DSH 三者，见 §6）：

1. **审批单位是「能力」不是「命令正则」**（Codex/DSH 路线）。pi-py 已有
   `capabilities`（`filesystem.write` / `process.execute` / `agent.delegate` /
   `knowledge.retrieve`），审批挂在能力上，而不是继续加 `deny_command_patterns`。
2. **三态决策** `allow / deny / ask`（Claude Code 路线）。`PolicyDecision.allowed: bool`
   改成枚举；`policy.json` 加 `require_approval_capabilities` / `require_approval_patterns`。
3. **规则前缀 DSL + 批准写回**（Claude Code 路线）。`ask` 命中后，批准时按
   `工具(参数前缀)` 写回一条 allow 规则（同一会话/用户），下次同类操作不再问。
4. **answerer 可插拔**（DSH 路线）。"人工确认"只是 answerer 的一种实现；接口抽象成
   `answerer(decision_request) -> allow|deny`，后续可换 LLM 审批 / 审计-only / 自动规则。
5. **边界判定先于审批**。`_run_tool` 先跑边界；边界拒绝的路径永远不 emit
   `ApprovalRequested`。bash 的边界只有 runner 档位：

   | runner | 破坏性命令的行为 |
   |---|---|
   | `None`（本地进程） | **硬 deny，不给确认**——边界兜不住，确认就是在宿主裸奔 |
   | Docker 池 / CubeSandbox | 可以确认——爆炸半径被容器/VM 兜住，`rm -rf /` 只是清掉一次性沙箱，回合归档可恢复 |

**现状与依赖**：`PolicyDecision` 二态；SSE 是 server→client 单向，无反向通道；
`Checkpoint/resume` 机制已写好（`loop.py`，`test_durable.py` 在测）但**没接 HTTP**；
无聊天 UI（app.html 是管理台）。与 ROADMAP §3「交互式 run」共享 checkpoint/resume 依赖。

**两步走**：
- **B1a 能力级预授权**（小，≈ DSH `approval-gate`）：危险能力 deny-by-default，会话经
  admin 显式授权后放开。改策略 + 一个会话授权字段/migration + admin 授权接口。不碰传输/状态机。
- **B1b 运行时 ask + 规则写回**（大，≈ Claude Code）：三态 + `ApprovalRequested` 事件 +
  `asyncio.Future` 挂起 + 审批端点 + 锁续期（或 checkpoint→释放锁→批准→resume）+ 前端待批面板。

**自验证**：可。单元测三态判定；集成测"边界 deny 不产生 ask""ask 超时 fail-closed 拒"。

---

## B2 对抗性验证（沙箱逃逸用例集）

**目标**：证明"单元测试全绿"不等于"逃不出去"。单元测试里的
`tests/test_sandbox_pool.py` 全程 `FakeTransport` 纯 mock，从没有用例真的尝试逃逸。

**⚠️ 优先级澄清（2026-10-03）**：生产只用 **CubeSandbox microVM**（`PI_SANDBOX=cubesandbox`），
docker 只是**本地无 cube 时的降级路径**，不是生产边界。所以 B2 的主战场是 **cube microVM 逃逸**，
docker 逃逸优先级降为低。

**✅ docker 级（降级路径，已落地、价值有限）**：`tests/test_sandbox_escape.py`，8 个用例对真实
`DockerRunner` 打逃逸（fork bomb / 内存 / 断网 / 密钥不泄漏 / docker.sock / 非 root / workspace
唯一可见 / smoke）。`skipif` 无 docker，CI 可跑。**只验证本地降级路径，不验证生产边界。**

**🎯 cube microVM 级（生产边界，待做）**：本机 Cubelet 平台在跑、生产档位就是
`PI_SANDBOX=cubesandbox`，`/opt/pi-venv` 有 `e2b_code_interpreter` SDK，经
`PI_CUBE_API_URL=http://127.0.0.1:3000` + `PI_SANDBOX_TEMPLATE` 即可建 VM。逃逸用例应覆盖
microVM 与 docker 不同的攻击面：

| 类别 | 用例 | 验证的边界 |
|---|---|---|
| 网络 | 从 VM 内连宿主内网 MySQL(13306)/Redis(16379)、云元数据(100.96.0.96)、cube 控制面(127.0.0.1:3000)、外网 | `--network none`（`PI_SANDBOX_NET` 关闭）在 microVM 的 eBPF 网络隔离 |
| 文件系统 | 读宿主文件（workspace 之外）、`/proc/1` 看宿主 init、宿主机名/挂载点 | 独立根 fs + 仅 tar 进出 workspace |
| 设备/内核 | `/dev/kvm`、`/dev/mem`、`/dev/sd*`、`dmesg`、`mount`、加载内核模块 | 独立内核 + 设备最小化 |
| 控制面 | VM 内访问 cube API / envd 控制通道 | microVM 隔离到宿主控制面的通道 |
| 资源 | fork bomb、内存超限、磁盘填满（VM 磁盘配额） | 256M/1vcpu 模板 + 平台配额 |
| 逃逸后横向 | 逃出后能否到别的会话 VM / 别的租户 | 会话级隔离 |

**不能自验证的部分**：kernel CVE、供应链投毒、利用沙箱内合法组件的零日（HF 事件那一类）。
"无法证明一个否定"——这类只能靠外部红队/持续研究，不做进仓库，但要在文档里明确边界。

**运行 runbook（待执行，不进 CI）**：cube 逃逸在**线上共享 cube 平台**上跑，会真建 VM、
吃平台配额（历史踩过 `no more resource` 事故），所以做成 manual、限并发、跑完必校验回收。
执行时照下面走：

1. **环境**：`source .env.local`（`PI_CUBE_API_URL` / `PI_CUBE_API_KEY` / `PI_CUBE_TEMPLATE` /
   `PI_CUBE_DOMAIN` / `PI_SANDBOX_CA_FILE` 全在）；SDK = `/opt/pi-venv` 的
   `e2b_code_interpreter`，`Sandbox.create(...)` 或复用 `CubeSandboxRunner`。
2. **harness 形态**：单 VM 顺序跑探测，`finally` 里 `sbx.kill()`；跑前用
   `cubemastercli list`（`/usr/local/services/cubetoolbox/CubeMaster/bin/cubemastercli`，或
   `PI_CUBE_CLI`）记 VM 数基线，跑后再查，确认回落（历史事故就是 VM 泄漏吃满配额）。
3. **两档分开**：
   - **第一档（非破坏）**：网络（连宿主内网 MySQL/Redis、云元数据、cube 控制面、外网——全是
     "连不上"的失败探测）；文件系统（`/proc/1`、`dmesg`、挂载点、宿主文件不可见）；
     设备（`/dev/kvm`、`/dev/mem`、`/dev/sd*` 是否存在）；控制面（VM 内能否摸到 cube API/envd）。
   - **第二档（资源耗尽，单独跑、跑前再确认）**：fork bomb、内存超限、磁盘填满——验证
     256M/1vcpu 模板的限额。
4. **判定**：网络/文件系统/设备/控制面探测**必须全部失败**（连不上、读不到、不存在）；
   资源耗尽探测**必须被限额拦住**（进程被杀/分配失败），且 VM 本体不死、宿主不受影响。
5. **结论落档**：每条 PASS/FAIL + 机器规格 + 模板 id 写进 `docs/`（如
   `docs/cube-sandbox-escape-report.md`），FAIL 的转 issue。

---

## B3 身份与权限（OIDC/SAML/MFA + 细粒度 RBAC + 租户级密钥）

**现状**：只有 JWT HS256 + PBKDF2(200k) + `is_admin` 一位；无 SSO、无分级权限、
无租户级密钥、无数据驻留/导出/删除的证据链。

**目标分层**：
1. **SSO**：OIDC/SAML/LDAP（先 OIDC，企业最通用），MFA（TOTP 起步）。
2. **RBAC**：用户→角色→权限，至少 `user / operator / admin` 三级 + 项目/租户维度隔离。
3. **租户级密钥与数据生命周期**：每租户独立加密密钥；数据驻留（地域）、导出、删除
   （GDPR/个保法）的接口与证据链。

**依赖**：身份模型 + Alembic 迁移；token 签发改走标准库/IdP 校验。RBAC 要先有权限模型，
否则"部门/项目级隔离"只能靠配置约定（现在正是这个状态）。

---

## B4 集群化 / 高可用（现状 OK，本节是"扩容迁移手册"）

**当前形态（2026-10-03 核实）**：`pi-py serve` 调 `uvicorn.run(...)` 未传 `workers`，默认
**1 worker = 1 进程**；app + Cubelet + pi-py 自己的 MySQL/Redis/Milvus 全部同机。在这个状态下
B4 的所有条目**都不是问题**：

- 全局并发 `asyncio.Semaphore(PI_MAX_CONCURRENT_RUNS)`：单进程时就是全局上限；
- 锁 `MemoryBackend`（进程本地）：单进程时就是正确语义；
- 会话沙箱池 `sandbox_pool`（进程内 dict）：单进程时池命中正常；池里装的是 runner
  **客户端句柄**（sandbox id + SDK 连接），VM 本体在 cube/docker 侧；
- 内存压力自适应 `_effective_pool_ttl` 读 `/proc/meminfo`：同机时读的就是 cube 宿主，**正确**。

**⚠️ 最大陷阱：单服务器 ≠ 单进程。** 一旦给 uvicorn 加 `--workers N`（或换 gunicorn），哪怕
还在这**同一台机器**上，就已经是多进程：并发上限变 N×8、进程本地锁失效、会话池按 worker
分裂。"扩容"不等于"上 K8s"——先看清是加了进程还是加了机器。

**扩容迁移清单（触发一条，执行到对应步）：**

| # | 触发条件 | 动作 |
|---|---|---|
| 1 | app 起多进程（同机 `--workers N` 或上多机） | 锁必须走 `PI_REDIS_URL`（`RedisBackend` 已实现）；否则同会话会并发跑两个 run |
| 2 | 同上 | 全局并发按"每副本上限 = 总预算 ÷ 副本数"摊平（信号量仍是每进程的） |
| 3 | 同上 | 会话粘性（LB sticky by session）或沙箱池外置；否则同会话跨 worker 各建 VM（丢复用 + 短期双 VM 吃配额，idle TTL 自愈） |
| 4 | cube 拆到独立机器 | 修 `_effective_pool_ttl`：同机时读 `/proc/meminfo` 是对的，分机后就读错了宿主——改为从 cube 平台取内存/配额信号，或退化为固定 TTL + cube 平台配额兜底 |
| 5 | 多副本上 LB | SSE 透传（关闭缓冲、足够超时、连接数上限），`PI_FORWARDED_ALLOW_IPS` 按新拓扑重配 |
| 6 | 交付要可复制 | Helm/K8s（当前只有 compose） |
| 7 | 多副本（进阶） | 锁加 fencing token + 崩溃未释放锁的恢复（锁 TTL = `run_timeout+60` = 660s 已覆盖正常回合，非紧急） |

---

## B5 审计不可变存储（WORM / 哈希链 / 双控）

**现状**：append-only 日滚动 JSONL + DB 镜像（`audit_events` 表），无 WORM、无哈希链、
root 可改文件、有 DB 权限的 admin 可删行。对要合规的企业，**无法证明未被篡改的审计日志
价值很低**。

**目标分层**：
1. **哈希链（tamper-evident，便宜先行）**：每条记录带 `prev = sha256(上一条)`，周期锚点
   落外部（S3 Object Lock / 日志服务）。检测篡改，而非阻止。
2. **WORM**：S3 Object Lock（compliance 模式），复用 `server/archive.py` 已有的
   `PI_ARCHIVE_S3_*` boto3 管道。
3. **双控分离**：运维平台的 admin 与审计存储的写入/删除权限分离（独立 IAM/账号），
   admin 不可删审计。
4. **留存与导出**：明确留存期（A 类已加 JSONL 侧 `PI_AUDIT_RETENTION_DAYS`；DB 镜像侧未管），
   支持导出。

---

## 6. 三个参照对象的审批哲学（B1 设计依据）

| 工具 | 审批对象 | 机制 | 可抄的 |
|---|---|---|---|
| Claude Code | 工具+参数前缀 | 三值规则 allow/ask/deny + 同步弹窗 + 批准写回 + hooks | 三态 + 规则前缀 DSL + 写回 |
| Codex | 沙箱档位 + 自主权 | `sandbox_mode`（边界）+ `approval_policy`（untrusted/on-failure/on-request/never） | 边界与审批分层；on-request 由模型发起确认 |
| DSH | 能力越权 | 低权限档起步，升级须 `sandbox_permissions`+justification 触发 ask/never；answerer 插拔（人/LLM/自动） | 能力为审批单位 + 可插拔 answerer |

**结论**：pi-py 没有 CLI 终端，没有天然在场的人，所以 B1 的 answerer 天然是
"admin 面板 + HTTP 回调"，但接口按可插拔设计。

---

## 7. FAQ

**B2 我们自己能验证吗？** 能，而且这台机器两层都能：docker（29.x 已装）+ microVM
（`/dev/kvm` 在、Cubelet 平台在跑、生产档位就是 `PI_SANDBOX=cubesandbox`，经
`PI_CUBE_API_URL` 建 VM 即可从 VM 内打逃逸）。不能自验证的只有 kernel CVE / 供应链 /
零日那类"证明否定"的问题。

**B4 咋回事？** 只在「app 本身要多副本」时才需要管：会话沙箱池是进程内 dict（单进程时正常）、
全局并发信号量是每进程（单进程时就是全局）、锁默认进程本地（单进程时就是正确语义）。
生产若是 **app 单节点 + cube 独立集群**，B4 非阻塞；唯一要提前记的是 cube 分机后
`_effective_pool_ttl` 读错宿主 `/proc/meminfo` 的坑。

**B5 咋回事？** 审计现在"append-only"但 root/admin 其实能改/删，合规上≈无效证据。
要补的是让篡改**可被检测**（哈希链）、**难以发生**（WORM/S3 Object Lock）、**admin 碰不到**
（双控分离），并明确留存期与导出。

---

## 8. 优先级与依赖

| 项 | 优先级 | 依赖 | 自验证 |
|---|---|---|---|
| B2 docker 级逃逸（已落地） | **低**（仅本地降级路径，非生产边界） | 无 | ✅ |
| B1a 能力级预授权 | 高（一天可落地） | 无 | ✅ |
| B3 OIDC + RBAC | 高（企业准入硬门槛） | 身份模型/migration | ✅ |
| B5 哈希链（tamper-evident） | 中（便宜、合规先行） | 无 | ✅ |
| B1b 运行时 ask + 写回 | 中 | B1a + checkpoint/resume 接 HTTP | ✅ |
| B4 集群化 | 低（仅「app 多副本」时相关；app 单节点 + cube 独立集群时非阻塞） | 会话粘性/全局信号量/锁 fencing | ✅ |
| B2 cube microVM 级逃逸 harness | **最高**（唯一验证生产边界；绿测≠逃不出去） | Cubelet 平台 | ✅ |
| B5 WORM / 双控 | 低（依赖外部存储/运维） | S3 Object Lock、IAM 分离 | 部分 |

---

## 9. 借鉴弘则（CubeSandbox 同源参照，2026-10-03）

来源：王正凯（弘则弥道 CTO）《金融投研 Agent 沙箱化》——弘则 4as 与 pi-py **同源**
（都用 Pi coding agent SDK），是其 TS 上游的另一个生产落地，几乎每段都能对照 pi-py。

**借鉴项（按性价比）：**

1. **CubeEgress 出口凭据注入**（对应评审 #7，最该补）。长期 key 留在出口侧、按目标服务
   匹配注入；guest 不存 key、template 不随 key 重建、凭据集中轮换/撤销。pi-py 现状是二态
   （`--network none` 或全开），缺"目标白名单 + 出口注入"中间态。本机 cube 平台已带 egress
   组件（`cube-egress` 镜像），接入配置即可，不自研。
2. **Host Mount 只读版本化分发**（RAG/skills/共享数据集）。revision 化不可变只读挂载 +
   挂载清单=授权清单 + 多沙箱共享宿主缓存，"未挂载即不可见"作 ACL 纵深。替代现在大资源
   tar 进出 + MinIO 预签名重搬。
3. **端到端数据路径自检**。`/readyz` 现在探 API+DNS，已内化"控制面≠数据面"的教训，还差
   "真建沙箱跑 create→写→执行→回收"。补 `/readyz/deep` 或发布验收脚本，低成本立刻可做。

**运维经验（记入 runbook）：**
- pause/resume 有 seccomp 兼容问题，保守内核下关闭自动 pause、只留 idle 回收。
- CoreDNS 抖动会传导到 Egress 策略路由，接 egress 时加周期校验。
- Cube 大版本升级宿主配置不自动保留，需人工对照清单核对。

**不照搬**：弘则把整个 loop（含模型调用）跑进 VM，威胁模型是"全隔离"；pi-py 是
"代码执行隔离"（loop 在可信 app 进程，仅 bash/文件进 VM）。对防代码逃逸够用，激进
下沉是战略选择，非当前必做。
