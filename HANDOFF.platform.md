# 交接文档：企业 Agent 底座——战略定稿 + #54 Run 地基

> 写给接手这个项目的下一个模型。读完这一份 + 旧 `HANDOFF.md` 就能干活。
> **先读 `DESIGN.platform.md` v2.0（平台总体设计：理念/画像/架构/不变量 I1–I11/分期/竞品；
> v2.0 起分两部分——第一部分设计 §1–§9，第二部分现状与竞品）——本文是它的任务书，
> 只给语义需求与验收，不给具体实现（用户明确要求，避免把未经验证的设计当标准答案）。**
> 生成时间：2026-09-05（同步 DESIGN v2.0：定位上移为"Agent 开发与运行底座 + 第一方参照 Agent"、
> 不变量扩至 I1–I11、分期重排）。项目根目录：`/root/pi/pi-python`。
> 旧 `HANDOFF.md` 是 #45（submit_plan）的交接，其 §0/§6 已过时（文档其实写完了、#45 已标 completed），
> 但它的 §1 环境坑、§4–§5 循环语义、§9–§10 测试约束仍然全部有效，**务必读**。

---

## 0. 一句话现状

产品方向已与用户逐轮确认定稿（§1–§2，含 2026-09-05 的定位修订，**不要再提、不要再问**）。
代码现状良好（2026-09-04 晚实测：测试约 280 个全绿、文档密度高、契约测试看住 SSE）。
下一个任务是 **#54：Run 地基**——把 run 升格为一等持久化实体 + append-only 事件日志，
它是 #46/#47/#52/#53 四个待办的共同病根，也是企业底座"可回溯/可审计/可恢复"的地基。
**任务书（语义需求 + 验收，实现自定）在 §4**。

**⚠️ 动手前必读 §3.1：另一个会话正在开发记忆功能（任务 #13–#17），`src/pi/memory/`、
`tests/test_memory.py`、`src/pi/server/runner.py`、`src/pi/server/app.py` 都有它的改动。
#54 与它改同一批文件，且本仓库没有 git——先和用户确认记忆系列是否收尾，再开工。**

---

## 1. 产品战略（用户已拍板，不要重新讨论）

### 1.1 愿景与判据

- 产品是**面向企业的 Agent 开发与运行底座，附第一方参照 Agent**（DESIGN §1.2）。
  编码 Agent 是第一个第一方参照 Agent——证明底座好用；企业自建 Agent 是 Phase 4 的一等能力
  ——证明底座通用。不做纯"来我这建平台"的空转底座（绝大多数企业没有能从零定义 Agent 的
  平台团队，冷启动靠第一方 Agent），也不做封闭工作台。
- **底座的判据**：某个东西属不属于底座，看下一个 Agent 进来时是否还得把它再写一遍。
  每个 Agent 都要重写的才是底座；只有某个 Agent 用的，是它的定义内容（AgentDefinition）。
- 底座最小集合五件事（按产品认知排序，DESIGN §1.3）：**Agent 运行时、身份与策略、
  数据与访问面、治理、契约与扩展**。现状里运行时/治理/契约已有骨架，真正的缺口是
  **Run 实体**（→ #54）与**组织维度**（→ Phase 2）。

### 1.2 已锁定的决定

| 决定 | 内容 |
|---|---|
| 定位 | "底座 + 第一方参照 Agent"（DESIGN §1.2 [已锁定]）：第一方 Agent 先证明底座好用；Phase 4 打开企业自建（Agent 管理控制台 + 开放契约），证明底座通用 |
| 交付形态 | **终局 = 多租户 SaaS、万级并发；首发 = 私有化部署的单体**（DESIGN §1.4/§7.3）。租户不进首发实现；tenant 不落列、由 org 推导；每条扩展缝必须**可反向验证**（能写出测试证明没堵死） |
| Agent 框架 | **不要 agent registry / 插件 SDK**。"Agent 即定义"（AgentDefinition，DESIGN §6.1）：Phase 3 用第二个真实 Agent 把提示词/工具集/策略从硬编码迁出、以两个真实 Agent 校准抽象——此前不提前抽 |
| Agent 也是 Principal | Agent 是被治理的主体：Agent 间调用走同一套认证/策略/配额/审计，归因链穿透（I4），无旁路（DESIGN §3.5） |
| 场景顺序 | 第二 Agent = 知识问答 + 内容创作搭车（Phase 3）→ 数据分析（Phase 5）→ 流程自动化（Phase 6，最重，本质是独立 Agent，此前只存在于路线图） |
| 用户入口 | Web 工作台优先；开放契约（OpenAPI/SSE/Webhook）+ API Key + IM 机器人随 Phase 4 自建工作台一起上（入口跟场景走，提前做是空转） |
| 模型接入 | **以能力为中心，不以厂商为中心**（DESIGN §6.7）：能力档案 + 内部规范表示 + 边缘适配 + 能力协商。**硬约束**（能力不足 = 显式报错，路由与降级都不解决它）与**软约束**（健康/成本降级，允许但必须打点告警）分开；数据分级一票否决看 run 有效分级上界（I9）；embedding/rerank 同等对待，任何子系统不写死单一端点 |
| 运行时复用 | 现有 agent loop / 工具 / policy / 沙箱 / SSE / 记忆就是通用 Agent 运行时，离目标很近，不推倒 |

### 1.3 战略层识别的四个贯穿主题（全流程缺口分析的收敛）

1. **Run 实体与事件日志**（→ #54，最优先）
2. **组织维度**：org_id、RBAC、策略矩阵（角色 × Agent → 策略档）、成本按部门分摊——同一个东西长在身份/审批/工具/计量四处
3. **出口与边界管控**：模型白名单（数据分级→可用端点，且分级随事实传播、只升不降 I9）、沙箱网络出口、检索 ACL、prompt 注入防御——本质都是"Agent 能碰什么数据"
4. **运维成熟度**：初始化向导、升级/备份路径、/metrics、告警、审计保留策略——从"能跑"到"敢交给企业 IT"

### 1.4 顺手可做的便宜件（可插任何间隙，不要单独排期）

密码重置流程（现在忘了密码只能进库）、**单 run 成本/时长熔断**（现在只有月配额，一条失控 run 能烧光整月）、附件上传 API（Phase 3 第一块砖，提前做也行）。

### 1.5 已知架构约束（终局与缝，见 DESIGN §7.3）

终局是多租户 SaaS、万级并发；现在是单体（workspace 本地盘、沙箱本机 docker 池）。**不提前做
分布式改造**，但每个 Phase 收尾要做"缝审查"：新代码有没有把状态埋进进程内存/本地盘、
有没有模块只能整机搬迁、每条缝的反向验证是否仍然成立。run 状态外置到事件日志（#54）
本身就是最大的那条缝——重启进程后任一 run 的状态/事件必须与重启前一致。

---

## 2. 路线图（用户认可的顺序，与 DESIGN §8 对齐）

```
#42/#43  安全运维前置（密钥轮换、云侧暴露面）——用户自己的活，未授权给模型，别碰
记忆系列 #13–#17（另一会话进行中）
   ↓
Phase 1  可靠运行
  #54  Run 地基：run 实体 + 事件日志 + trace_id + 崩溃三分 + 恢复幂等
       + run 分类谱系（kind / parent_run_id）+ 种子 root org   ← 下一个任务，任务书见 §4（实现自定）
  #46  人工审批：waiting_approval 状态迁移 + 审批绑定调用 + reconciliation + I11 零占用
  便宜件（可插间隙）：run 级熔断、密码重置
   ↓
Phase 2  企业治理：Principal（人/Key/Agent）+ org 树 + RBAC + 策略矩阵 + SSO
         + 审计保留执行 + 最小管理后台（用户/部门/角色/用量）
   ↓
Phase 3  第二 Agent：知识问答（附件上传 + 入库管线 + ACL 检索 + 引用 UI + 注入防御）
         + 内容创作搭车——以两个真实 Agent 校准 AgentDefinition 抽象
   ↓
Phase 4  企业自建与开放入口：Agent 管理控制台（定义/版本/发布/Run/审计/用量）
         + 开放契约（OpenAPI/SSE/Webhook）+ API Key + IM 机器人
   ↓
Phase 5  数据分析 Agent：连接器 + 凭据保险箱（凭据即工具）+ artifacts 一等化
Phase 6  流程自动化 Agent：调度器 + 工作流 + 审批链泛化

横切（随 Phase 1–4 持续）：/metrics 与告警、备份恢复、升级演练、缝审查（每 Phase 收尾）
```

排序逻辑（DESIGN §8）：先证明 Agent 能生产运行（Phase 1），再证明企业敢治理（Phase 2），
再证明抽象成立（Phase 3，第二个真实 Agent），最后打开自建（Phase 4）——平台先被自己验证，
再交给企业。数据模型级的事（Run、org）先行不可拖。

任务清单现状（截至 2026-09-04 晚，存于 `/root/.qoder-cn/tasks/`，跨会话持久）：
#42/#43 pending（用户的活）、#44/#45/#51 completed、#46–#53 pending、
#12 completed、#13 in_progress、#14–#17 pending（记忆会话的系列）。
#47/#52/#53 三个老任务在 #54 落地后各塌缩成薄下游，**不要**在 #54 之前单独修。

---

## 3. 环境要点（先读旧 `HANDOFF.md` §1，以下是增量）

### 3.1 ⚠️ 并发会话警告（本次评估亲眼所见）

2026-09-04 晚 21:22–21:28，`src/pi/memory/`、`tests/test_memory.py` 在被另一个会话实时修改
（任务 #13–#17：记忆仲裁模块），完整测试数在几分钟内从 271 涨到 280。
本会话第一次跑套件撞见 12 个 `test_memory.py` 失败，**那是对方改到一半的状态，不是真 bug**
（之后 8 次连跑全绿）。教训：

- 开工前先问用户：记忆系列收尾了吗？
- 看到 `test_memory.py` 失败先怀疑并发，单跑它确认。
- `runner.py`/`app.py` 是两边的公共战场，改动前先 diff 心里有数（没有 git，全靠手工）。

### 3.2 仍然成立的旧坑（详见旧 HANDOFF §1）

- `python` 不在 PATH，用 `/root/pi/pi-python/.venv/bin/python`；Node 要 `export PATH=/usr/local/node/bin:$PATH`
- ~~**不是 git 仓库**~~ → ~~**2026-09-06 17:03:50 已 `git init`（分支 `master`），但零 commit**~~
  → ✅ **2026-09-06 21:10 首个提交 `6eb79d7` 已落地**，分支已改名 `main`，
  remote = `https://github.com/15838328874/pi.git`（接在用户自己的 GitHub `Initial commit`
  `3fd5eba` 之后，非 force-push）。`git diff` / `checkout` / `stash` 现在**真的可用**。
  仓库级身份 `15838328874 <135090639+15838328874@users.noreply.github.com>`（取自用户 GitHub
  提交，能关联头像），**全局 git 配置未动**。提交前扫过索引：133 文件，无 `.env`/`.venv`/
  `node_modules`/`__pycache__`/`dist`/`*.db`/审计日志、无真实云实例域名或公网 IP、无硬编码凭据。
  详见 `deploy/environments.md` L8。`.gitignore:5-6` 忽略 `.env`/`.env.test`
  （`git check-ignore` 与 `git status` 双重确认）。
  ⚠️ **git 给的是回滚能力，不是互斥**——并发会话照样可能同时在改，动手前仍要核对 mtime。
- `.env` 指向**火山引擎云上** RDS/Redis/Milvus，**默认不碰**；验证用临时 SQLite，跑完删
  （"不碰"指不做 #42/#43 那类运维变更；用户明确要求时做**只读**排查是可以的，见 §3.2.1）。
  **例外（2026-09-06 晚，用户明确授权"把运维动作的活干下"）**：追加了 `MYSQL_HOST` /
  `REDIS_HOST`（`docker-compose.cloud.yml` 参数化后必需）和 `PI_METRICS_TOKEN`
  （`PI_METRICS` 默认 `1` 而 token 默认空 → 下次重启 `/metrics` 会在公网 IP 上裸奔）。
  两次都先 `cp -p` 备份到 `/root/pi/local-only-unsanitized/`，权限保持 600，
  全程未把任何值打印出来，改完复验裸跑路径 `ServerSettings.from_env()` 正常。
  **#42 密钥轮换与 #43 云侧暴露面仍未做**，量级不同，需用户单独授权。
- 没有浏览器，前端只能 `vue-tsc` + vitest + SSR 渲染测试

### 3.2.1 ⚠️ 新增（2026-09-06）：先搞清楚你在哪个环境

**`deploy/environments.md` 是新建的环境专档，动手前必读**（14 条已核实的地雷 L1–L14，
外加两个环境的逐键对照、启动流程、账号现状与基线快照）。最容易踩的五条：

- **离线套件曾 flaky，测试侧已修**（L11）：2026-09-06 共 10 次全套件运行有 6 次红在
  `test_server.py::TestDeregister::test_the_cascade_wipes_every_trace_and_the_token`（约 60%）。
  当晚修掉：新增 `_wait_usage_settled()`，在 `_counts()` 快照前等在飞的抽取收尾，
  等**静默**而非等固定行数（照同类里 `_wait_audit_flushed` 的模式）。修后单独跑 30/30、
  全套件连跑 5 次均 `363 passed, 1 skipped`。**只改了测试，没碰产品代码。**
  ⚠️ **根因不只是测试瑕疵，而那一层没修**：注销的 `_counts()` 快照没等在飞的后台记忆抽取，
  而抽取无论成败都写一条 `turns=0` 的 `usage_records` → 生产语义上"注销时正好有抽取在飞"
  会让 `purged` 回执与实际删除数不符。**这与 #54 的"可回溯/可审计"目标同源**，
  做 #54 时**仍然**建议一并考虑注销与在飞任务的互斥——测试现在会等，产品代码不会。
  注：这条 flaky 与 17:03–17:12 那个会话的 web 工具改动无关，改前改后都红在同一条上。

- **当前跑着的实例是测试环境，不是生产**：**PID 1080214**（2026-09-06 20:22:39 启动，
  上一代 PID 809332 已重启）在 **8398** 端口，连 `pi_py_test` / Redis NS `test` /
  `workspaces-test` / Milvus NS `it` / `PI_ENVIRONMENT=test` / `PI_TRACER=otel`。
  `.env.test` 是 **12 个键的覆盖层**（当晚从 6 个涨到 12 个，新增 metrics 与 otel 两组），而且
  `pi/__init__.py` **不会自动加载它**（候选只有 `.pi-py.env` / `.env` / `~/.pi-py/.env`）——
  必须先 `set -a; . ./.env; . ./.env.test; set +a`。裸跑 `pi-py serve` 会连**生产** `pi_py`。
  判断某个活进程连的是哪个库，读 `/proc/<pid>/environ`，**不要**看 `.env`。
- **✅ `.env` 里 `PI_METRICS_TOKEN` 曾被定义两次、空值那份赢了（L12）—— 已修复**：
  原先第 57 行是 `PI_METRICS_TOKEN=`（空），第 173 行才是真 token。加载器语义是
  `if key not in os.environ` —— **先到先得，空值也算"已设置"**，所以真 token 整行被跳过，
  干净进程加载 `.env` 后 `metrics_token == ''` 而 `metrics_enabled == True`，
  即生产 `/metrics` 会无鉴权对外。**2026-09-06 22:00 已删掉那个空赋值**并留了防回归注释；
  验证：重复键扫描无输出、`metrics_token` 长度 64 为真值、`/metrics is open` 告警不再触发。
  ⚠️ **要重启生产服务才生效**（当前跑的测试实例读 `.env.test`，那份一直是好的）。
  **记住这条纪律**：改 `.env` 后用 `grep -E '^[A-Z_]+=' .env | cut -d= -f1 | sort | uniq -d`
  扫重复键 —— 这个加载器会让任何重复键静默失效，且失效方向永远是"看着配了、其实没配"。
- **✅ 生产 schema 曾被 `create_all` 污染（2026-09-06 20:09:21 发生，22:00 已修复）**：
  当时 `pi_py` 的 `alembic_version` 停在 `0002`，但表数已从 5 张变成 9 张 ——
  `agent_runs`/`agent_steps`/`audit_events`/`user_memories` 被 lifespan 的 `db.init()` →
  `create_all` 静默补出来，而 **`sessions.plan` 列仍缺失**（`0003` 是 `ADD COLUMN`，
  `create_all` 只建表不补列）。取证与机理见 `deploy/environments.md` **L1**
  （决定性物证是 `agent_runs` 的**列序**：生产当时是 ORM 声明序、测试是 alembic 追加序）。
  **修复动作**：硬断言目标 schema == `pi_py` 且 8 张数据表合计 0 行后，DROP 那 4 张空表
  （顺序照 `0006` 的 downgrade，先子表 `agent_steps`），schema 精确回到 `0002` 形状，
  再跑 `pi-py migrate`。
  **验证**：`alembic_version` = `0007_trace_fidelity`、9 张表齐全、`sessions.plan` 存在，
  且**逐表逐列比对生产与测试两个 schema 完全一致（0 处差异）**，`agent_runs` 列序已变为
  alembic 追加序。
  ⚠️ **生产仍 0 用户 0 数据**，即"schema 就绪、未播种、未上线"。#54 要动的
  `agent_runs`/`agent_steps` 现在生产也有了，但**#54 若新增迁移请从 `0008` 起编号**，
  并且**永远先 `migrate` 再 `serve`** —— L1 里那条"create_all 只建表不补列、一旦先跑过
  alembic 就升不干净"的教训对以后每一次加迁移都适用。
- **`.env.test` 不覆盖 `PI_JWT_SECRET` 和任何凭据** —— 两个环境共用同一个 JWT 密钥、
  同一个 Redis/Milvus/MySQL 实例、同一个模型网关（测试跑的是**真模型，花真钱**），
  只靠 schema/NS 分隔数据落点。后果之一：在一个环境 `logout`/`revoke` **不会**在另一个环境
  生效（撤销状态存 Redis，按 NS 隔离），但 token 在两边都能通过密码学校验。
  给 `.env.test` 补一个独立 `PI_JWT_SECRET` 是一行的事，属于 #42/#43（用户的运维活，
  未授权自动执行）。详见 L4。

### 3.3 旧 HANDOFF 已过时的部分（别被误导）

- 旧 §0/§6 说"#45 只剩两份文档"——**已完成**：README 已有 "Task planning (Phase 1)" 节和工具清单
  （当时是 "Ten built-ins"；`web_fetch`/`web_search` 后来被删除，现为 "Eight built-ins"），
  ARCHITECTURE 已含 §17.24（MySQL TEXT 默认值夹缝）和 §18 计划卡双形状。#45 状态 completed。

---

## 4. #54 Run 地基——任务书（语义需求 + 验收，不给实现）

> **用户明确要求**：本节只约束语义、不变量与验收标准。表结构、事件命名、API 形状、端点设计、
> 代码组织、迁移拆分、实施顺序——全部由执行模型自行设计，**不要把下面出现的任何名词当成设计定稿**。
> 动手前先读 `DESIGN.platform.md` §5.2（领域不变量 I1–I11）与 §6.2（Run：状态机/事件日志/
> 崩溃恢复——注意 v2.0 里 Run 机制在 §6.2，不再是 §6.1），及附A（OpenHands 事件流先例，
> 只读语义不抄实现）。

### 4.1 目标

把"一次 run"从一次性的 HTTP 请求升格为**持久化实体**，配 **append-only 事件日志**，
让系统第一次能回答：*那次运行到底发生了什么、停在哪儿、花了多少、谁批准的*。

- run 有身份（标识、状态、trace_id、kind、parent_run_id），DB 里有一行代表它
- 事件日志是**真相源**：SSE / 前端 / 审计视图 / 计量全部是它的投影（P3）
- 崩溃/中断后事实不丢失、不自相矛盾（修掉 #52 的"审计说跑了、转录说没跑"）；
  **恢复本身幂等**——双重崩溃不产生重复或歧义记录（I3）
- SSE 重放成为可能（#53 的服务端半边）
- 计量关联到 run（trace_id 贯穿审计/计量/span）
- org_id 从第一张新表起成立：种子 root org，"补 org_id"这个场景永远不许出现（I8）

### 4.2 非目标（本期不做，只留口子）

- 审批流本身（#46）：本期只冻结 `waiting_approval` 状态语义与加态规程，迁移逻辑不做
- 断点续跑（#47）：只冻结 `interrupted` 状态语义（含前置闸门语义——存在未裁决的非幂等
  "结果未知"时不得续跑），恢复逻辑不做
- 前端重连 UI（#53 客户端半边）：本期前端只透传 run 标识
- 保留策略执行与 crypto-shred：只留配置位（默认永不过期）与 key_id 列（DESIGN §6.9/Q9 缝），
  清理/加密逻辑不做
- **记忆抽取收编为 system run**：本期只落 kind/parent_run_id 字段口子——记忆会话（#13–#17）
  正在改同一批文件，收编是它收尾后的薄下游任务，#54 不碰
- 组织模型、/metrics、幂等键：各归各期。但新端点/新表的设计**不要把将来的幂等与 org 口子堵死**

### 4.3 语义需求（必须成立的事）

1. **身份与状态**：每次 run 有唯一标识与 trace_id；状态机六值一次冻结、只加不改：
   running / waiting_approval / interrupted / complete / failed / cancelled。终态封闭（I2）；
   状态迁移本身是被记录的事件。**加态规程**：将来新增状态 = 新事件类型 + 事件 schema 版本号 +
   投影必须忽略未知状态——禁止状态机之外的隐藏状态（如进程内存里的标志位）
2. **run 分类与谱系**：kind = interactive / system 落库（本期交互 run 即 interactive）；
   parent_run_id 口子留好（记忆抽取、知识入库批等后台作业将来收编为 system run，本期不收编）。
   无 run 的消耗在计量上显式标注为无 run，不假装归因
3. **事实落库**（I1）：run 边界、完整消息、工具调用的**全参数与全结果**（截断只允许发生在
   审计投影层，不允许发生在日志；全参数同时是将来"审批绑定调用"的前提——I10 的缝）、
   计划/压缩/错误、状态迁移——append-only、单 run 内有序。流式增量不入日志，重放由完整消息重建
4. **两阶段与崩溃三分**（P10，本任务的灵魂）：工具调用先记录意图再执行再记录结果；崩溃后
   恢复扫描必须能三分：**从未执行** / **执行了但结果未知**（合成显式结果块、**带恢复合成
   标记**，保 I3 配对不变量，绝不自动重跑）/ **事实俱在**。非幂等工具的"结果未知"交给人裁决。
   恢复扫描本身幂等：跑两遍结果一致，重复崩溃不叠加合成块
5. **重放**：以事件序号为游标，能从日志重建除增量外的全部事件流；回放查询按 run 定位
   （run 局部性——DESIGN §7.3 事件日志分区结论的依据）
6. **归因**（I4）：计量行关联到 run；trace_id 出现在审计记录与 span 属性里
7. **组织作用域与扩展缝**：runs / run_events 等新表带 org_id 且非空，同时种子默认 root org
   （最小 org 表或等价机制，实现自定）——含 user_id 不含 org_id 的迁移直接打回（I8）。
   run 状态不进进程内存（应用层无状态化与 SSE 订阅化的前提）；新表评估分级字段口子（I9 缝）
   与 key_id 列（Q9 缝）——只留列，不做任何逻辑

### 4.4 约束与坑（环境事实，不是设计）

- `runner.py` / `app.py` 是记忆会话（#13–#17）的公共战场；`loop.py` 是零硬编码工具名的神圣区
  ——事件截获尽量在 runner 层做，真要动 loop 想清楚再动（旧 HANDOFF §4）
- 崩溃三分的"合成结果块"有现成模式：#45 的 SKIPPED_RESULT（`loop.py:55`）——跳过调用时合成
  结果块保配对不变量的做法已经写好并测过，语义同构，复用思路
- 配对不变量的测试锚点是 `StrictFakeProvider`（conftest）：崩溃三分的测试必须让**重载历史**
  过它，否则测不到真 bug——原生 FakeProvider 完全不检查配对（旧 HANDOFF §7.2 的教训）
- MySQL 拒绝 TEXT 列的字面 DEFAULT（1101）→ 新列 nullable、无 server_default；跨方言 DDL
  有既有守卫测试惯例（`test_mysql_compat.py`），沿用
- SSE wire 契约受三个既有契约测试 + 前端 `KNOWN_EVENTS` 双向约束（旧 HANDOFF §9）：
  新增/改动 wire 事件两边必须同步，契约变更走 regen 流程
- 迁移验证必须用 `pi.cli migrate` 真建库一次：测试 fixture 用 create_all，
  迁移文件永远不会被执行（旧 HANDOFF §7.4 的教训）
- 超时路径别忘了覆盖：现有超时语义在 `asyncio.timeout(self.timeout)`，run 的收尾必须
  正常/超时/异常三路都闭环

### 4.5 验收标准（怎么算做完）

1. 全套件绿；新增测试至少覆盖：状态迁移合法性（非法迁移拒绝）、事件序与保真、**崩溃三分
   模拟**（含重载历史喂 StrictFakeProvider 不炸——这是核心测试）、**恢复幂等**（恢复扫描
   跑两遍结果一致）、重放游标、计量归因、**org 守卫**（种子 root org 存在；含 user_id 不含
   org_id 的迁移被拒）
2. 迁移在临时 SQLite 上 upgrade/downgrade/upgrade 干净 + `pi.cli migrate` 真建库验证
3. 裸机端到端（真 uvicorn + 临时 SQLite + 脚本化 provider，照旧 HANDOFF §7.5；两个坑：登录
   响应字段是 `access_token`、审计文件名有日期后缀）：完整 run 的事件序、崩溃恢复（含二次
   崩溃后的恢复幂等）、重放各验一遍
4. 变异测试至少三处（手工备份还原，不是 git 仓库；候选：去掉意图预写 / 去掉崩溃合成 /
   去掉恢复标记 / 去掉重放游标 / 去掉 org 守卫）——各有测试变红，证明测试钉住了语义
5. README / ARCHITECTURE 按仓库惯例更新（事件日志作为真相源、崩溃语义与恢复幂等、状态机
   与加态规程各一节）；#54 标 completed；#46 描述更新为"基于 run 状态机实现（绑定调用 +
   reconciliation + I11 零占用）"并解锁

### 4.6 诚实声明的成本

写放大（每工具调用 +2 行 DB 写，可接受）；事件 schema 演进要有纪律（只加 type 不改旧 payload
形状——这正是加态规程的前半句）；全保真留痕 = 员工操作全量监控，**合规与隐私两头都要满足**
——保留期限分级（全保真 30–90 天、聚合永久）本期只留配置位与 key_id 列（Q9），落地时和
用户对齐具体天数（PIPL/GDPR，audit.py docstring 自己写过）。

---

## 5. 明确不做的事（本期 + 战略层）

- 不建 agent registry / 插件框架；AgentDefinition 是数据不是框架，Phase 3 才校准抽象（§1.2）
- 不在 #54 收编记忆抽取为 system run——记忆会话（#13–#17）正在改同一批文件，#54 只留
  kind/parent_run_id 字段口子
- 不建租户隔离层、不做多实例水平扩展改造——只按 DESIGN §7.3 留可反向验证的缝
  （tenant 不落列、由 org 推导）
- 不碰云 RDS/Redis/Milvus 的任何运维操作（#42/#43 是用户的活）
- 不把服务切到 compose（用户原话"暂时先用裸跑"）
- 不在 #54 之前单独修 #47/#52/#53（它们是 #54 的下游，单修是打补丁）

---

## 6. 本次评估核实过的事实（2026-09-04 晚，可信任）

- 完整套件 8 次全绿（271→280 passed + 1 skipped，数量漂移来自并发记忆会话）
- `test_memory.py` 单独跑 87 passed（当时点）
- README/ARCHITECTURE 已含 #45 全部内容；#45 任务状态 completed
- 一次 run 的记录现状确实散在 5 处 5 时机（审计 JSONL 截断 400 字、messages turn 末落库、
  usage_records 无 run_id、SSE 不持久化、OTel span 采样可关）——§4 的动机全部来自代码核实，
  关键位置：`runner.py:112/115/167/194/235`、`loop.py:55/131/139/220`、`app.py:355`、
  `audit.py`（行号会漂，以符号为准）
- 路由全景：`/healthz`、auth 3 条、me/sessions 5 条、run 1 条、admin 3 条、usage、memories 3 条
- 部署资产：docker-compose 两份 + Caddyfile 两份 + cloud-deploy.md（拓扑文档质量不错）

### 6.1 增补核实（2026-09-06，覆盖上面几条已过时的数字）

上面是 09-04 晚的快照，**保留不改**（它是当时的真实记录），但以下几条现在已经不同，
以本节为准。全部为 2026-09-06 实测：

- **测试数**：Python **`363 passed, 1 skipped`**（364 collected，25.3s）——不再是 347/348，
  也不再是 271→280。**现在这是稳定基线**：曾约 60% flaky（10 次里 6 次红在
  `TestDeregister::test_the_cascade_wipes_every_trace_and_the_token`），2026-09-06 晚已修
  （新增 `_wait_usage_settled()`，只改测试），修后单独跑 30/30、全套件连跑 5 次结果一致。
  机制与**仍未关闭的生产竞态**见 `deploy/environments.md` **L11** 与 §3.2.1。
  `test_memory.py` 现在 125 例（不再是 87）。前端 `npm test` **56 passed**（1.1s，稳定），
  其中 `render.test.ts` 26 / `transcript.test.ts` 21 / `sse.test.ts` 9；
  `npm run typecheck`（`vue-tsc --noEmit`）亦通过；
  `tests/live/api.live.ts` 是 **11 例**（文档里长期写的 7 已陈旧）。
- **路由全景**：上面那条清单**不完整**。实测 app 上 **30 条路由 = 23 条业务 + 7 条基础设施**
  （2026-09-06 晚新增 `GET /metrics` 后从 29 涨到 30；`/metrics` 归基础设施，因为它
  `include_in_schema=False`、不进 OpenAPI 契约）。漏掉的是：`DELETE /v1/me`（注销）、
  `GET /v1/admin/traces` 与 `/traces/{run_id}`（轨迹，即 `agent_runs`/`agent_steps` 的读侧，
  **与 #54 直接相关**）、**会话附件三条** `POST|GET /v1/sessions/{id}/files` +
  `GET /files/{session_id}/{name}`（后者在 `/v1` 之外、**故意无认证**）、以及 `GET /metrics`。
  admin 也不是 3 条而是 6 条。完整表已补进 README §Multi-user server，
  分析见 `deploy/environments.md` L3/L10。
  ⚠️ **`web/openapi.json` 与 `/docs` 现在都不再是完整清单**（缺 `/metrics`），
  要枚举全部路由只能用 L10 里那个从 app 对象取 `routes` 的脚本。
- **代码规模**（2026-09-06 21:45 实测，比当天下午又涨了一轮）：Python 源码 **10,628** 行
  （ARCHITECTURE 头部原写 8,600，下午我改成 9,672，现已 10,628）、Python 测试 **7,015** 行、
  前端手写 **3,303** 行（另有 codegen 的 `api/schema.d.ts` 2,441 行不计入手写）、
  前端测试 **1,137** 行。
- **新增文档**：`deploy/environments.md`（生产/测试环境专档，**L1–L14** 地雷清单）。
  README 与 ARCHITECTURE 的计数漂移已一并修正（ARCHITECTURE §15 的分文件表现在逐行相加
  正好等于 collected 总数；修正过程中发现 `test_server.py` 原写 92 实际 76、
  `test_model_capabilities.py` 整个不在表里，两者都已补正，随后又随功能扩展涨到 83 / 14）。
- **已变（2026-09-06 22:00 之后）**：`pi_py`（生产）已从 `0002` **修复到 `0007_trace_fidelity`**，
  9 张表、`sessions.plan` 存在、与 `pi_py_test` 逐表逐列完全一致；但仍 **0 用户 0 数据**
  （未播种、未上线）。`pi_py_test` 一直在 head `0007`。
  `pi_py_test.users` 里 **`admin` 的密码已轮换**（不再是 `pi-test-123`，新值不入文档），
  其余 5 个播种账号仍是默认值。
- **已变（2026-09-06 晚）**：`web/dist` 于 **18:48 重建**（原记录是 09-05 19:32），
  且没有比它更新的前端源码，dist 仍是最新的。Docker 里新增
  **`pi-py-prometheus-1`**（`prom/prometheus:v2.54.1`）与 **`pi-py-jaeger-1`**
  （`jaegertracing/all-in-one:1.60`）两个容器，Up 2 小时 —— 这就是 `127.0.0.1:4317`
  有监听的原因，`.env.test` 的 `PI_TRACER=otel` 有真实对端（Jaeger），
  `deploy/prometheus.yml` 对应 Prometheus 抓取。仍无沙箱孤儿容器
  （那个 4 天前 `Exited(0)` 的 `hello-world` 是 docker 装机验证残留）。
  `audit-test-2026-09-06.jsonl` 11 条；依然没有 `audit-2026-09-06.jsonl`（生产没跑）。

## 7. 常用命令

```bash
# Python 测试
cd /root/pi/pi-python && .venv/bin/python -m pytest -q

# 前端
export PATH=/usr/local/node/bin:$PATH
cd /root/pi/pi-python/web && npm run typecheck && npm test && npm run build

# 契约 regen（改了响应模型之后）
cd /root/pi/pi-python && .venv/bin/python tools/dump_openapi.py
cd /root/pi/pi-python/web && npm run gen:api

# 迁移（临时库验证）
cd /root/pi/pi-python && PI_DATABASE_URL="sqlite+aiosqlite:////tmp/x.db" .venv/bin/python -m pi.cli migrate

# ---- 环境相关（2026-09-06 新增，详见 deploy/environments.md）----

# 某个活进程连的是哪个库？读 /proc，别读 .env
PID=$(pgrep -f 'pi-py serve'); tr '\0' '\n' < /proc/$PID/environ | grep -E '^PI_(DATABASE_URL|REDIS_NS|WORKSPACE_ROOT)='

# 起测试环境（顺序不能反；.env.test 不会被自动加载）
cd /root/pi/pi-python && set -a; . ./.env; . ./.env.test; set +a
.venv/bin/pi-py migrate && .venv/bin/pi-py serve --host 0.0.0.0 --port 8398

# 前端对着 8398（代码里所有默认值都是 8300，必须显式覆盖）
cd /root/pi/pi-python/web && PI_API=http://127.0.0.1:8398 npm run dev
cd /root/pi/pi-python/web && PI_LIVE_API=http://127.0.0.1:8398 npm run test:live
```

## 8. 给下一个模型的开场建议

1. 读旧 `HANDOFF.md` §1/§4/§5/§9/§10 + 本文档全文 + `DESIGN.platform.md`（v2.0 两部分结构；
   注意章节号：Run 机制在 §6.2、Agent 定义在 §6.1、模型网关在 §6.7、不变量在 §5.2、缝表在 §7.3）
   + **`deploy/environments.md`**（2026-09-06 新增；生产/测试环境逐键对照、启动流程、
   L1–L11 地雷清单、账号现状、基线快照。**先读它的 §0 和 §1**，否则你可能对着生产库干活
   而不自知）
2. 问用户两件事：记忆系列（#13–#17）是否收尾；要不要先 `git init`（强烈建议，多会话并行 + 无版本控制是当前最大单点风险）
3. 然后开工 #54：**先自行设计实现方案**（对照 §4 语义需求与验收，写下来给自己当检查表），
   再动手。遇到语义冲突以 DESIGN.platform.md 为准；遇到环境/代码事实以本文为准；
   两者都没覆盖的按旧 HANDOFF 的既有架构原则（§11.3 契约、"messages 是记录本体"等）推导

   ⚠️ #54 与环境的交叉点：`agent_runs`/`agent_steps` 由迁移 `0006`/`0007` 建，
   **只存在于 `pi_py_test`**，生产 `pi_py` 停在 `0002` 根本没有这两张表。#54 要动的
   正是这块地基，所以（a）验证一律在测试环境或临时 SQLite 上做；（b）如果 #54 新增迁移，
   编号从 `0008` 起，并且**在文档里记下生产仍停在 `0002`** —— 生产上线前必须先
   `pi-py migrate` 而不是先 `serve`，原因见 `deploy/environments.md` L1（`create_all`
   只建表不补列，先 serve 一次就把 alembic 升级路径彻底弄坏）。
