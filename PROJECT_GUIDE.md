# pi-py 项目全解

> 一份把项目讲透的文档：从"它是什么"讲到"每个设计为什么这样取舍"，再到"踩过的坑和测试体系"。
> 三层读者都能各取所需：**小白**读第一部分看懂全貌；**工程师**读第二、四、五部分拿技术细节；
> **面试官**读第一、二、六部分看价值与亮点。所有内容都来自真实代码、真实测试、真实踩坑记录，
> 无虚构数据。最新状态见 `ROADMAP.md`，逐模块细节见 `ARCHITECTURE.md`。
>
> **文档地图**（四个文档各管一段，知识点不重复）：
>
> | 文档 | 定位 | 什么问题看它 |
> |---|---|---|
> | `README.md` | 门面 | 这是什么、怎么装、怎么跑（快速上手入口） |
> | `PROJECT_GUIDE.md` | 叙事与价值 | 为什么这么设计（取舍）、踩过什么坑（故事版）、测试样例与实测数据 |
> | `ARCHITECTURE.md` | 技术手册 | 每个模块每个函数、配置全表（§13）、坑清单（§17）、差距清单（§19） |
> | `ROADMAP.md` | 状态与路线图 | 什么做完了、什么没做、下一步做什么（含环境区分表） |
| `docs/`（三件） | CubeSandbox 专项 | 沙箱设计笔记 / 生产部署手册 / 生产就绪审计——专项文档，不重复核心四文档内容 |


**目录**

- [第一部分 讲给所有人听](#第一部分-讲给所有人听)
- [第二部分 设计思路与取舍](#第二部分-设计思路与取舍)
- [第三部分 功能全景](#第三部分-功能全景)
- [第四部分 坑与解](#第四部分-坑与解)
- [第五部分 测试体系](#第五部分-测试体系)
- [第六部分 数据与实测](#第六部分-数据与实测)
- [第七部分 待实现与路线图](#第七部分-待实现与路线图)
- [附录A 术语表](#附录a-术语表)
- [附录B 5 分钟跑起来](#附录b-5-分钟跑起来)
- [附录C 目录结构导读](#附录c-目录结构导读10-秒看懂仓库)
- [附录D 环境变量速查](#附录d-环境变量速查关键项)
- [附录E 轨迹 JSON 样例](#附录e-轨迹-json-样例真实落盘的一行)

---

## 第一部分 讲给所有人听

### 1.1 一句话

**pi-py 是一个可自托管、可扩展的 AI 智能体平台**：它给大模型装上"手"（沙箱化的工具执行）、
给团队装上"治理"（多租户配额/审计/隔离）、给研发装上"尺子"（统一轨迹 + 评估闭环）、
给训练装上"原料"（RL 数据飞轮导出 JSONL）。

三个定位支柱：
- **自托管**：数据不出内网，模型、数据库、审计全部自己掌控；
- **评估与数据闭环**：任务集判分、A/B 对比、rollout→reward→JSONL——"好不好"可量化，
  "用得越多"真的能"让模型越强"；
- **协议级扩展**：MCP（外部工具标准协议）+ Skills（可复用技能包）+ ToolProvider 抽象——
  编码是第一个深度打磨的场景，不是能力边界。

为什么不是"编码智能体服务"？因为它已经长出了服务之外的形态：管理员在控制台治理、
评测者跑任务集、训练者消费 JSONL、MCP/Skills 让它能接任何工具——"平台"才装得下这三种角色。
（一句更短的版本：**可自托管、可评估、可沉淀训练数据的 agent 平台**，见 ARCHITECTURE §20。）

### 1.2 一段话：它解决了什么问题

大模型聊天很成熟，但"让模型干活"和"让模型聊天"是两回事：

| 痛点 | 普通聊天产品 | pi-py 的答案 |
|---|---|---|
| 模型只会说不会做 | 网页版 ChatGPT 只能生成代码文本 | 模型调用**工具**（bash/读写文件/搜索），在沙箱里真实执行 |
| 执行有风险 | 没有隔离，模型跑 `rm -rf` 就是真删 | **Docker 沙箱**：断网、限额、白名单路径，乱来也出不了沙箱 |
| 单人玩具 | 插件/客户端每人各玩各的 | **多用户服务**：注册/登录/配额/限流/审计，企业级多租户 |
| 无法判断好坏 | 没有评估标准 | **eval 评测闭环**：任务集 + 自动判分 + A/B 对比 |
| 数据浪费 | 执行记录用完即弃 | **统一轨迹**落库 + **RL 数据飞轮**导出 JSONL 喂 veRL/TRL |

### 1.3 一次完整旅程（不出现代码，看懂全链路）

1. 用户 `zhu` 打开网页，注册并登录，拿到令牌（token）。
2. 他新建一个会话，输入："帮我写个 hello.txt，内容是 hello world"。
3. 服务端检查配额、限流，然后把这轮对话历史 + 系统提示词 + **相关的跨会话记忆**（"用户喜欢简洁输出"）组装好，发给云上的大模型。
4. 模型思考后决定调用工具：`write(path="hello.txt", content="hello world")`。
5. 安全策略先检查：路径必须在用户自己的工作区里 → 放行。
6. 工具在 **Docker 沙箱容器**里执行（这个容器没有网络、有内存/CPU 限额、预热好了等着），写入文件。
7. 模型拿到"写入成功"的结果，回复"已完成"，**流式**逐字推到用户浏览器上。
8. 服务端落库：消息历史、token 用量（计入本月配额）、审计日志（脱敏）、**完整轨迹**（原始事件日志，含每一步耗时）。
9. 用户点"查看轨迹"：一张**时序图**展示"模型想了 1.2 秒 → 工具跑了 45 毫秒"的全过程；哪一步慢了、哪一步失败了，一眼可见。
10. 管理员在控制台看到这个用户的用量、配额，随时可以禁用账号、吊销令牌。
11. 事后，这批执行轨迹可以被评测系统判分（任务是否完成），高质量样本导出为 SFT/RLVR 训练数据——数据飞轮转起来了。

### 1.4 价值主张：横向对比

| 维度 | 网页版 ChatGPT/Claude | Cursor/Codex 插件 | LangChain 教程项目 | **pi-py** |
|---|---|---|---|---|
| 能执行代码吗 | 只生成文本 | 能（单机） | 能（demo 级） | 能，**沙箱隔离**执行 |
| 多用户 | 个人账号 | 个人工具 | 无 | **注册/配额/限流/审计/撤销** |
| 自托管 | 不可 | 不可 | 可 | **可**（数据不出内网） |
| 评估体系 | 无 | 无 | 无 | **任务集+判分+A/B** |
| 训练数据产出 | 不公开 | 不公开 | 无 | **RL 数据飞轮**（SFT/RLVR JSONL） |
| 工具扩展 | 封闭 | 封闭 | 手写 | **MCP 标准协议 + Skills 技能包** |
| 长任务恢复 | 无 | 有限 | 无 | **断点续传**（checkpoint） |
| 跨会话记忆 | 有（封闭） | 有 | 无 | **分层记忆**（压缩摘要 + 向量检索） |
| 可观测 | 黑盒 | 黑盒 | 无 | **轨迹/审计/指标/计量**四件套 |

**每列的补充说明**（面试可能被追问）：

- **对网页版聊天**：它们把"回答"做到极致，但"干活"需要执行环境——而执行环境
  恰恰是模型最容易闯祸的地方（删文件、读密钥、发请求）。pi-py 的答案不是
  "不让模型执行"，而是"让模型在**受限的**环境里执行"（沙箱断网+限额+路径白名单），
  执行能力和安全边界同时成立。
- **对 IDE 插件**：Cursor/Codex 是单机单用户的工具形态，用户体验优秀；
  但它们不可自托管、没有多租户概念（没有配额/审计/强制下线）、没有评估闭环。
  pi-py 是**服务形态**：一个部署，团队共用，管理员可治理，数据可沉淀。
- **对教程项目**：LangChain 教程证明"能跑通"，pi-py 证明"能上线"——
  多租户治理、降级链、沙箱限额、审计脱敏、测试同构这些生产属性才是差距所在。

**组合优势**：单看每一项都有产品做到，但同时拥有"沙箱执行 + 多租户 + 评估闭环 +
训练飞轮 + 标准协议扩展"的**自托管**项目很少——这正是它的定位：
**一个可以自己掌控数据、自己评估、自己训练的可扩展 agent 平台后端**。

### 1.5 纵向演进：这个项目怎么长出来的

**第一阶段：pi（TypeScript 原版）**
上游项目 pi 是一个 TypeScript 的编码智能体（agent loop + 工具 + 模型层）。它是思路源头，但类型系统复杂、生态偏前端。

**第二阶段：pi-py（Python 重写）**
用 Python 从零重写：为什么换语言？
- AI 生态全部在 Python（模型 SDK、评估库、训练框架 veRL/TRL 的对接方都是 Python）；
- 异步模型（asyncio）天然适合 I/O 密集的 agent 场景；
- 开发速度：同样的功能，Python 代码量更少、迭代更快。
代价：放弃上游 TS 代码的复用，但换来与 AI 工具链的无缝对接。

**第三阶段：生产化改造（本仓库的核心工作）**
从"单机脚本"变成"多用户服务"：JWT 认证、配额、限流、审计、Docker 沙箱、Redis 多实例、可观测四件套。
这一阶段的每项都是"看起来简单、上线才见真章"的活：PBKDF2 要移出事件循环（453ms→40ms）、
限流要分布式（Redis）、沙箱要限额（Docker 默认不限额）、审计字段要截断（防攻击者塞满文件）。

**第四阶段：四轮能力爬坡（P1→P4）**
- **P1 统一轨迹**：把散在三条平行流（SSE/审计/tracing）的执行记录统一成一份 canonical 事件日志——评估、回放、调试从此只有一个事实源。
- **P2 durable execution**：断点续传，长任务不怕中断。
- **P3 分层记忆**：episodic（会话内压缩摘要，落库复用）+ semantic（跨会话向量记忆，自动召回）。
- **P4 eval harness**：任务集、判分器、报告、A/B——"好不好"从此可以量化。

**第五阶段：生态扩展**
- **MCP + Skills**：用标准协议接外部工具（MCP），用技能包沉淀可复用能力（Skills）——统一成一个 `ToolProvider` 抽象。
- **RL 数据飞轮**：批量 rollout → 判分得 reward → 过滤 → 导出 SFT/RLVR JSONL，直接对接 veRL/TRL。
- **Web 前端三件套**：用户聊天页、轨迹时序图视图、管理控制台——零构建单文件，无前端工程负担。
- **测试与生产统一**：从"SQLite 测试库"到"测试与生产同构的 MySQL+Redis"，18 个被
  SQLite 掩盖的测试问题现形并修复——测试环境与生产环境的差异本身就是 bug 来源。

---

## 第二部分 设计思路与取舍

### 2.1 架构总览

```
                        ┌─────────────────────────────────────────┐
  浏览器/客户端 ──HTTPS──▶  FastAPI (server/app.py)               │
                        │  ┌─────────┐ ┌─────────┐ ┌──────────┐  │
                        │  │ 认证/配额 │ │ 限流/锁 │ │ 审计/指标 │  │
                        │  └─────────┘ └─────────┘ └──────────┘  │
                        │        ▼ RunManager (server/runner.py) │
                        │  ┌───────────────────────────────┐     │
                        │  │ 组装：记忆注入 + 技能索引 +     │     │
                        │  │ 工具注册表 + 历史 + 系统提示词  │     │
                        │  └───────────────────────────────┘     │
                        └─────────────────────────────────────────┘
                                         ▼
              ┌──────────────── AgentLoop (agent/loop.py) ────────────────┐
              │  调用模型(LLM层) ⇄ 解析工具调用 ⇄ 沙箱执行 ⇄ 记录轨迹        │
              │  (统一 StreamEvent / 降级链 / 退避重试 / 连续拒绝熔断)       │
              └───────────────────────────────────────────────────────────┘
                     │                │                │
              ┌──────▼──────┐  ┌──────▼──────┐  ┌─────▼───────┐
              │ LLM 提供商   │  │ 工具层       │  │ 轨迹 (P1)    │
              │ openai/     │  │ builtin+MCP │  │ 事件日志+ts  │
              │ anthropic/  │  │ +Skills     │  │ jsonl落盘    │
              │ fake        │  │ (registry)  │  └─────────────┘
              └─────────────┘  └──────┬──────┘
                                      ▼
                          Docker 沙箱（预热池+限额+断网）
              ─────────────────────────────────────────────────
              持久化：MySQL(消息/记忆/用量) Redis(锁/限流) Milvus(向量)
              投影：审计(脱敏jsonl) 指标(Prometheus) 计量(成本)
```

### 2.2 五条设计哲学（展开版）

**1. loop 只产出状态/事件，持久化交给调用方。**
AgentLoop 完全不知道 MySQL/Redis 的存在，它只通过三个钩子把状态吐出去：
`on_message`（新消息）、`on_compact`（压缩发生）、`on_checkpoint`（断点）。
服务端决定存哪、怎么存；评测器直接抓轨迹；测试自己决定存哪。**好处**：loop 保持"傻而纯"，
任何新消费者（审计、指标、回放）都是加投影，不动核心循环。**代价**：状态散在调用方，写调用方的人要懂钩子语义。

怎么理解"傻而纯"？看 loop 里一次工具调用做了什么：
```
loop：模型说"调 write(path, content)" → policy 检查 → 沙箱执行 → 把结果喂回上下文
      → trajectory.record(ToolCall(...))          # 只记事件
      → yield ToolCallEndEvent(...)               # 只发事件
      → on_checkpoint(checkpoint)                 # 有钩子就调，没有就算了
```
它不知道结果存进了 MySQL 的哪张表、审计写进了哪个文件、指标加了哪个计数器——
这些全是 server 层在消费事件时决定的。**换掉任何一层存储，loop 一行不改。**

**2. 非破坏优先。**
压缩上下文时，`messages` 表**只增不删**，摘要写独立的 `compactions` 表。为什么：
删除/重写历史会撞上消息下标（idx）、违反"一次 run 的原子性"、丢掉审计底稿。
摘要表是派生数据，随时可重建；原始消息是事实，永远保留。

更深一层的考量：**摘要要不要重复付钱？** 早期实现每轮都重新总结整段历史（每轮多一次 LLM 调用）。
现在 `compactions` 表记录"摘要覆盖到 idx K"，下一轮加载时直接
`[摘要] + [idx > K 的新消息]` 当历史——同一段历史只付一次摘要费。这就是"派生数据落库复用"：
落库不是为了删原始数据，是为了**不重复计算**。

**3. 能不依赖就不依赖，但接口先留好。**
语义记忆第一版用零依赖的词法检索；向量升级时**只改 `MemoryRepo.search()` 一个方法**，
工具和 runner 一行不动。同理：前端零构建单文件；测试用服务替身不花钱。
**原则**：先跑通，后增强，但增强点必须提前设计好"换装口"。

"换装口"的三个实例：
- `MemoryRepo.search(user_id, query, k)` —— 词法/向量/混合检索全在这个方法内切换；
- `CommandRunner` Protocol —— LocalRunner/DockerRunner/CubeSandboxRunner 都实现同一协议，
  上层 AgentLoop 完全无感（`PI_SANDBOX` 只改变 runner 的构造，microVM 与容器同协议）；
- `ToolProvider` —— builtin/MCP/Skills 都是"工具来源"，registry 聚合，加新来源不改上层。

**4. 附属系统永不阻塞主流程（fail-soft）。**
审计写失败？记账失败？轨迹落盘失败？**记日志，绝不让用户的 run 失败**。
用户的工作比任何观测系统都重要。代价：观测数据可能丢，所以失败必须**有日志可查**。
有专门测试验证这一点：把轨迹落盘路径堵死（父目录是个文件），断言 run 仍然 200、
SSE 正常结束、消息照常落库。

**反面对称**：**主流程失败必须响亮**——启动配置错（如非法沙箱模式）直接拒绝启动，
不做静默降级。曾经 `PI_SANDBOX` 拼错会静默退化成"进程内执行"（模型能读应用的环境变量，
包括数据库密码和 JWT 密钥），这个静默降级本身就是安全事故，后来改成启动即失败。

**5. 轨迹是唯一事实源，其余全是投影。**
审计 = 轨迹的脱敏投影；指标 = 轨迹的聚合投影；评估 = 轨迹的判分消费。
边界清晰：**轨迹存原始值（不脱敏），审计存脱敏值**——评估/回放要原始数据，审计要合规。

这条哲学的落地是"四条流合成一条"：早期版本 SSE 事件、审计、tracer span 三条平行流
各自记录 run 的一部分，互相对不上（工具被拒了 SSE 里没有、审计里参数被脱敏、tracer
只包"解析+放行"路径）。现在 loop 内只记一份轨迹，其余全部从轨迹投影：
指标里的 `tool_call` 计数器就是 run 结束时从轨迹事件里逐条投影出来的
（所以它能统计到被拒/未知/无效参数的调用——旧 span 路径做不到）。

### 2.3 关键取舍记录（A/B 对比）

| 决策点 | 备选方案 | 选了谁 | 为什么 |
|---|---|---|---|
| 语言 | TypeScript（沿用上游）| **Python** | AI 生态、异步模型、开发速度（见 1.5） |
| 数据库 | PostgreSQL（最初设想）/ SQLite（早期测试）| **MySQL**（生产实际）+ 测试同库 | 生产实际就是云端托管 MySQL；2026-09 起测试与生产统一，消除"测试 SQLite 与生产 MySQL 行为不一致"的整类问题（SQLite 不强制外键、日期格式等差异真踩过坑） |
| 缓存 | 无/进程内存 | **Redis**（多实例必配）| 锁/限流/撤销要跨实例一致；单实例内存降级可跑 |
| 沙箱 | 进程内直接执行 | **Docker + 预热池 + 限额 + 断网** | 进程内执行 = 模型能读应用环境变量（含密钥）；容器预热把冷启动从秒级压到毫秒级 |
| 前端 | React/Vue 工程 | **零构建单文件 + vanilla JS** | 三个页面不需要工程链；改动即生效；无 node 依赖。代价：无组件生态，靠纪律保持一致性 |
| 工具扩展 | 每种来源写一套 | **统一 ToolProvider + ToolRegistry** | MCP/Skills/内置本质都是"工具来源"；统一后自动获得 policy/审计/沙箱/配额 |
| RL 飞轮 | 自己训模型 | **只做数据侧**（rollout→reward→filter→JSONL） | 训练是 veRL/TRL 的活；项目停在数据生产，接口是 JSONL |
| 记忆检索 | 直接上 Milvus | **词法先行，向量后补** | 零依赖先跑通；`MemoryRepo.search()` 接口不变，升级只动一处 |
| 压缩 | 重写 messages 表 | **独立 compactions 表** | 见哲学 2 |
| 管理端提权 | 页面按钮 | **只能直接写库** | 没有自助提权口子 = 没有提权攻击面；admin 是"人工授予"的审计动作 |

### 2.4 架构巧思（值得讲的设计点，配实现细节）

**1. 钩子三件套**（`agent/loop.py`）：loop 与世界的全部接口就是三个回调。新增消费者 = 新增投影，零侵入。
具体形态：loop 构造函数收 `on_message` / `on_compact` / `on_checkpoint` 三个可选回调，
内部在"状态已定稿"的时刻调用。server 的 `on_message` 就是往 buffer 里追加，
run 结束后一次 `append_many` 批量落库——**不是每条消息单独写库**，一次 run 只有一次批量写，
这在公网数据库场景（每轮约 40 次串行往返）是显著优化。

**2. `message_idx` 并行数组**（P3 最微妙处）：压缩按**列表下标**切（`messages[:-keep_last]`），
但落库按 **DB idx**。所以 loop 维护 `self.message_idx: list[int | None]`，每追加一条消息同步追加下标，
压缩时同步截断——这样"摘要覆盖到第几条"才能报得准。改压缩逻辑漏掉这个数组就会出幽灵 bug
（摘要声称覆盖到 idx 10，实际只到 7，下一轮加载历史会丢三条消息）。交接纪律：
**动压缩逻辑前先读 `message_idx` 的注释**。

**3. 预热池与模型思考并行**：runner 在模型**思考第一轮该调什么工具时**就预热沙箱容器
（`prewarm` 是 best-effort）。等模型决定用 bash 时，容器已经热了——把"模型 1 秒 + 容器冷启动 3 秒"变成"模型 1 秒 + 容器执行 45ms"。
实现：`get_runner(...)` 拿到 runner 后立即 `runner.prewarm(cwd)`（有 prewarm 方法才调），
预热是后台任务、失败只记日志。池的形态：**每工作区一个 `sleep infinity` 常驻容器**，
调用 = `docker exec`，空闲回收；预热容器内存仅 ~26 MiB/个（实测）。

**4. 连续拒绝熔断**：实测过模型被安全策略拒绝后**死磕**：25 次变着花样尝试越权路径，
每次 600 秒超时。现在连续 N 次被拒就**大声终止**并告诉模型"重新读拒绝原因"——
省 token，也避免把拒绝当偶发错误无限重试。细节：只有 `denied=True`（策略拒绝）计数，
工具自身报错（`is_error`）**不**计数——报错是正常学习信号，策略拒绝是死路。

**5. 降级链 + 退避重试**：`PI_FALLBACK_CHAIN="openai/qwen3.8-max,openai/qwen3.8-flash,..."`
让主模型挂了自动切备选。精确规则：**只对瞬时错误**（连接错误、超时、429/5xx）
重试和降级——参数/协议错误直接上抛（重试也没用）。每模型最多重试 2 次，
退避基值 0.5s 指数增长；**流式进行中失败绝不重放**（可能已输出一半，重放=重复）。
每次降级都打一个计数器：沉默降级 = 事故（"降级必须发出噪音"）。

**6. 向量记忆的优雅换装**：`MemoryRepo.search(user_id, query, k)` 一个方法封装了
"向量优先、词法兜底、embedding 失败回退、命中按用户过滤、缺失行跳过"全部逻辑；
工具层和 runner 层完全不知道后端是词法还是 Milvus。
词法检索怎么做的（零依赖版）：query 拆词，对候选记忆按**词重叠度打分**取 top-k——
朴素但可工作。向量版：query → 云 embedding → Milvus 相似度检索 → 拿 id 回 MySQL 取原文；
Milvus 挂了自动回词法。**连 embedding 的 token 用量都计入用户配额**
（`embedding/<model>` 记进 usage_records），和 LLM 用量一样有月度账单。

**7. policy 只加不减**（`server_policy`）：服务器模式的策略文件**只能收紧不能放松**——
一份只写了 deny 规则的策略文件如果被允许关掉 `path_sandbox`/`redact`，等于越配置越不安全。
所以这两个开关在 server 模式强制打开，文件只能往上加限制。
路径沙箱的实现细节：`_extract_paths` 对已知文件工具取专门的 `path` 参数，
对未知工具（MCP/Skill）取所有"path 类键"的字符串值——**新工具来源不写策略适配代码
也能被路径沙箱管住**（这正是 MCP/Skills 设计红利之一）。

**8. 原始与投影分离**：轨迹存原始参数（评估要还原现场），审计存脱敏参数（合规要最小暴露），
同一条事件两个视角，谁都不妥协。脱敏器有实测过的模式集：API key、阿里云/AWS/GitHub/Slack
token、中国手机号等。审计还做了**防伪造/防轰炸**：认证记录带客户端 IP/UA，字段全部截断——
失败登录的用户名是攻击者可控的，不限长会让人一条请求塞满审计文件。

**9. 测试与生产同构**：单测直连本地 MySQL/Redis（不再是 SQLite 玩具库），
每个测试从"20 个空用户 + 9 个空会话"的干净库开始——MySQL 强制外键，SQLite 不查，
这个差异曾让 18 个测试在真实约束下现形（详见 4.5）。

**10. 零构建前端的可靠性防线**：静态 HTML 全部 `no-cache`；任何未捕获 JS 错误显示成页面横幅；
Playwright 无头浏览器**程序化断言布局**（气泡间距、对齐方式、宽度一致性），
因为"协议层 curl 全通 ≠ 页面没问题"。流式接收用 `fetch` + `ReadableStream` 手动解析
SSE 帧协议（浏览器 EventSource 只支持 GET，run 端点是 POST）。

**11. 会话锁的双层语义**：`run_turn` 先拿**分布式会话锁**（Redis SET NX EX，TTL=超时+60s），
再进**全局并发信号量**（`PI_MAX_CONCURRENT_RUNS`）。锁保证同一会话不会并发跑两轮
（输家收到的是 **HTTP 200 流内的 `event: error`**，不是 4xx——SSE 的语义细节），
信号量保证全局资源（沙箱、内存）不被无界并发打爆。

**12. 令牌撤销的两种粒度**：登出只黑名单**这一个 token 的 jti**（保留到自然过期）；
管理员踢人用**用户 epoch**——比"该用户所有 token 的签发时间戳"新的 token 全部失效，
一次写入全部踢掉。禁用账号则每次请求查库（`is_active`），立即生效。

---

## 第三部分 功能全景

### 3.1 能力矩阵（全部已实现、已验证）

| 层 | 功能 | 位置 | 亮点 |
|---|---|---|---|
| LLM 层 | openai/anthropic 提供商 + 统一流事件（TextDelta/ToolCallDelta/StreamEnd） | `src/pi/llm/` | 厂商差异被抽象在适配层，loop 只认一种流 |
| LLM 层 | 降级链 + 指数退避 + 流式不重放 | `llm/fallback.py` | 生产级韧性 |
| LLM 层 | think_filter（过滤思考 token） | `llm/think_filter.py` | 长思考模型友好 |
| Agent | 工具调用循环 + 错误回喂自纠正 | `agent/loop.py` | 工具报错自动进上下文让模型改 |
| Agent | 上下文压缩（摘要+保留尾部） | `agent/compaction.py` | 非破坏，落库复用 |
| Agent | 断点续传（checkpoint/resume） | `agent/loop.py` | P2 |
| Agent | 连续拒绝熔断 | `agent/loop.py` | 防死磕 |
| 轨迹 | canonical 事件日志（6 类事件 + ts 墙钟） | `agent/trajectory.py` | P1，评估/回放唯一事实源 |
| 轨迹 | **双写**：runs 表（结构化）+ jsonl 按天（合规底稿） | `server/db.py` `server/trajectory_store.py` | 落盘失败不影响 run |
| 工具 | 10 内置（bash/read/write/edit/grep/find/ls/remember/recall/subagent） | `src/pi/tools/` | 全走 policy 路径沙箱 |
| 工具 | 子代理（递归委派 + max_depth） | `tools/subagent.py` | Multi-Agent |
| 工具 | MCP 工具源（stdio + fail-soft + 生命周期） | `tools/mcp.py` | 标准协议 |
| 工具 | Skills 技能包（SKILL.md + 索引注入 + 脚本走沙箱） | `tools/skill.py` | 渐进披露 |
| 工具 | ToolRegistry（聚合/去重/预热缓存） | `tools/registry.py` | 一个抽象管所有来源 |
| 沙箱 | Docker（预热池+限额+断网）+ **CubeSandbox microVM**（GNU timeout/退出码透传/10MB 上限） | `tools/sandbox.py` | 52 exec/s 实测 + 真机故障注入探针 |
| 归档 | 会话工作区 tar.gz + 差异元数据 + MinIO 惰性上传 | `server/archive.py` | 9 turns 实测 |
| 记忆 | episodic（compactions 表复用摘要） | `server/db.py` | 不重复花钱总结 |
| 记忆 | semantic（remember/recall 工具 + 自动召回注入） | `tools/memory.py` | 跨会话 |
| 记忆 | 向量检索（Milvus + 云 embedding，词法兜底） | `server/vectorstore.py` `llm/embedding.py` | 换后端只改一处 |
| 服务端 | 注册/登录/登出（JWT+PBKDF2） | `server/app.py` `server/auth.py` | PBKDF2 40ms |
| 服务端 | 会话管理（建/列/删） | `server/app.py` | 删除级联清理 |
| 服务端 | SSE 流式 run（8 类事件） | `server/runner.py` | 锁/超时/配额/限流 |
| 服务端 | 配额（月度 tokens，402） | `observability/metering.py` | 含 embedding 计量 |
| 服务端 | 限流 + 会话锁（Redis） | `server/ratelimit.py` `server/cache.py` | 多实例一致 |
| 服务端 | 审计（按天滚动 jsonl + 过滤查询） | `security/audit.py` | 脱敏投影 |
| 服务端 | 令牌撤销（jti 黑名单 + 用户 epoch） | `server/app.py` | 踢人两种粒度 |
| 服务端 | 管理 API（用户/配额/禁用/审计/stats/usage） | `server/app.py` | 控制台后端 |
| 可观测 | Prometheus 指标（run/llm/tool/记忆/HTTP RED） | `observability/metrics.py` | 标签有界 |
| 可观测 | tracing（jsonl/otel） | `observability/tracing.py` | 全链路 span |
| 可观测 | 成本计量（按模型价格表） | `observability/metering.py` | 每月按模型账单 |
| 评估 | 任务集 + 4 种判分（file/command/tests/judge） | `evals/` | P4 |
| 评估 | A/B 对比 + 报告 | `evals/report.py` | 量化"好不好" |
| 飞轮 | rollout → reward → filter → SFT/RLVR JSONL | `evals/rollout.py` 等 | 数据生产者 |
| 前端 | 用户聊天页（气泡/流式/工具折叠/用量面板） | `server/static/app.html` | 零构建 |
| 前端 | 轨迹视图（时序图三模式/车道/联动） | `server/static/trajectory.html` | 面试演示 |
| 前端 | 管理控制台（概览/用户/审计） | `server/static/admin.html` | 零构建 |
| 部署 | 本地/生产双 compose + 文档 | `deploy/` | 环境严格区分 |
| SDK | 官方异步客户端（SSE 流式解析 + PiError 语义） | `src/pi/client.py` | 单测 + 真实模型实测 |

### 3.2 核心功能精讲

#### （1）Agent 循环：一个"轮"的完整解剖

一个"轮"（turn）的完整流程：

```
turns += 1
t0 = perf_counter(); ts0 = time.time()          # 计时 + 墙钟（轨迹 time 模式用）
outbound = redact_messages(messages)            # 若 policy.redact，出站前脱敏
for ev in provider.stream(system, outbound, tool_specs):   # 流式调用模型
    TextDelta     → 累积文本，yield TextDeltaEvent（SSE 逐字推送）
    ToolCallDelta → 按 id 增量拼装参数（模型参数是分片流的），yield ToolCallStartEvent
    StreamEnd     → 记 stop_reason 和 usage
record(LlmCall(...))                            # 轨迹事件（含 ts=ts0，起始时刻）
if not calls or stop_reason != "tool_use": break   # 没有工具调用 = 结束
for call in calls:
    outcome = await _run_tool(call)             # policy 检查 → 沙箱执行 → 结果
    record(ToolCall(...))                       # 轨迹事件（denied/is_error/latency）
    yield ToolCallEndEvent(...)
    if outcome.denied: consecutive_denials += 1 # 只有策略拒绝计数
    if consecutive_denials >= MAX: 终止并大声说明
把工具结果作为 user 消息喂回上下文 → 下一轮
```

三个关键设计：
- **错误回喂自纠正**：工具失败不是致命错误——结果（含报错信息）作为 `ToolResultBlock`
  进入上下文，模型下一轮看到"这个命令报错了：xxx"就会自己换方案。工具错误是**学习信号**。
- **流式参数增量拼装**：模型的 tool_calls 参数是分片流式到达的，loop 用
  `by_id: dict[id, {name, arguments}]` 按调用 id 增量拼装——这是 OpenAI 流式协议的要求，
  拼错了参数就是损坏的 JSON。
- **两种终止**：`stop_reason != "tool_use"` 正常收尾；`max_turns` 或连续拒绝熔断是保护性终止。

#### （2）沙箱执行：从"进程内"到"容器+池"的进化

- **为什么必须隔离**：进程内执行 bash = 模型能读应用进程的环境变量（数据库密码、JWT 密钥）、
  能读写别的用户的目录。这不是功能问题，是安全事故。
- **CommandRunner 协议**：`LocalRunner` / `DockerRunner` 实现同一接口
  （`run(command, cwd, timeout) -> CommandResult`、`prewarm(cwd)`），
  上层（AgentLoop/工具）通过 `get_runner(PI_SANDBOX, ...)` 拿到实例，**对实现无感**。
  将来接 CubeSandbox（microVM）只需要再写一个 runner。
- **限额必须显式**：Docker 默认**不限额**（Memory=0、NanoCpus=0、无 PidsLimit），
  开放注册的服务里一个 `while true` 就能吃光宿主机。所以
  `PI_SANDBOX_MEMORY/PIDS/CPUS` 在**全部四条建容器路径**（冷 CLI / 冷 API / 热 CLI / 热 API）
  都强制带上，且 `--memory-swap = --memory`（否则 Docker 默认允许 2 倍交换）。
- **断网是默认**：`--network none`。联网是显式开关（`PI_SANDBOX_NET=host`），
  且进程内抓取工具（web_fetch/web_search）已因 SSRF 风险整体移除（见 4.12）——沙箱内 bash 抓取替代。
- **预热池**：每工作区一个 `sleep infinity` 常驻容器，调用 = `docker exec`
  （比 `docker run` 少一次容器创建）；空闲自动回收；预热是后台任务，
  与模型思考并行（见巧思 3）。实测每预热容器仅 ~26 MiB。
- **fail-closed**：`PI_SANDBOX` 非法值**启动即拒绝**（曾静默降级为进程内执行 = 事故）。
- **第二形态 CubeSandbox**（`PI_SANDBOX=cubesandbox`，方案 B）：**每回合新建独立 microVM
  （71ms 冷启）、用完即销毁**——零常驻、崩溃天然隔离，内存模型 = 并发回合数 × 256Mi
  而非会话数（E2B 兼容 SDK，宿主需 KVM）；工作区每回合进出 VM、结束回传归档；命令用 GNU `timeout` 包装（退出码 124 → `timed_out`，
  沙箱内收尸零残留）；非零退出码透传（模型能区分"exit 1 失败"和"超时"）；
  工作区 >10MB 装载拒绝并给可操作提示；close 有总超时（默认 90s），三层 VM 泄漏防线。
  生产化审计（7 个真实缺口 + 故障注入探针）见 `docs/cube-sandbox-design-notes.md` /
  `docs/production-readiness.md`。

#### （3）分层记忆：两个时间尺度的记忆

**episodic（会话内，短期）**：
- 触发：会话消息估算超过 `compact_threshold`（默认 80,000 字符）。
- 动作：LLM 把旧消息总结成摘要，只保留最近 `compact_keep`（默认 8）条；
  摘要落 `compactions` 表（`covered_upto_idx` 标明覆盖到哪条），原始消息不动。
- 复用：下一轮开始时 server 查 `latest_compaction`，加载 `[摘要] + [idx 之后的新消息]`
  作为历史——**同一段历史只付一次摘要的 LLM 费用**。
- 微妙点：摘要覆盖边界依赖 `message_idx` 并行数组（巧思 2），改压缩必须懂它。

**semantic（跨会话，长期）**：
- 写入：模型主动调 `remember(text)` 工具（在判断"这条信息值得长期记住"时）；
  文本落 `memories` 表（事实源），同时异步写向量索引（Milvus，best-effort）。
- 读取：每轮开始自动 `search(user_id, prompt, k=3)`，命中内容注入系统提示词
  （`<relevant memories>` 块）——**模型不用被提醒就"记得"用户**。
- 检索：向量优先（云 embedding → Milvus 相似度），失败/未配置自动词法兜底；
  命中结果按 user_id 过滤（绝不跨用户召回）；embedding 用量计入配额。

#### （4）统一轨迹：为什么值得做，以及怎么做

**为什么**：早期三条平行流（SSE 事件、审计、tracer span）各记一部分，互相对不上。
统一成一份 canonical 事件日志后：评估=判分消费、回放=重放消费、调试=可视化消费、
计量=求和消费、指标=投影消费——**一个事实源，N 个视角**。

**事件模型**（6 类，每条带 `ts` 墙钟）：
`RunStarted`（run_id/model/cwd/tools/prompt）→ `LlmCall`（每轮：tokens/stop_reason/latency/text/tool_calls）
→ `ToolCall`（每个：name/arguments/result/is_error/denied/latency）→ `Compaction`（可选）
→ `RunError`（可选）→ `RunFinished`（汇总）。

**ts 的语义细节**：LlmCall/ToolCall 的 `ts` 是**调用起始时刻**（loop 在调用前抓的墙钟），
不是记录时刻（记录在调用结束后）——错位的话，时序图的 time 模式会把事件画错位置。
`latency_ms` 用单调时钟（perf_counter），`ts` 用墙钟，两者分工不同：一个量时长，一个定位置。

**落盘与查询**：run 结束后轨迹序列化成一行 JSON 追加到按天滚动的 jsonl
（`PI_TRAJECTORY_PATH`，`""`=关）；**落盘失败只记日志**（fail-soft，有测试验证）；
查询端点 `GET /v1/sessions/{id}/trajectory` 先做属主校验（跨用户 404 不泄漏存在性），
落盘与查询：run 结束后轨迹序列化成一行 JSON **双写**——按天滚动的 jsonl（`PI_TRAJECTORY_PATH`，
`""`=关，追加式合规底稿，fail-soft）+ `runs` 数据库表（结构化查询索引，迁移 0005）。
查询三个层级：会话级端点（DB 优先、jsonl 兜底旧数据）、`/v1/trajectory/{run_id}`（属主校验）、
`/v1/admin/trajectory/{run_id}`（管理员跨用户回放）。

**可视化**：时序图三种模式——`sequence`（等宽按序，看流程）、`duration`（按耗时累加，
看时间花哪）、`time`（真实墙钟，看空闲 gap）；模型/工具两条车道 + 轮次分隔线；
点击双向联动（时序条 ↔ 列表行）。**面试演示：跑一次 → 打开视图 → "时间花在模型还是工具、
哪一步失败"一眼看清。**

#### （5）RL 数据飞轮：训练的数据生产侧

**定位**：本项目**不训练模型**，只做数据生产：批量 rollout → 判分得 reward → 过滤 → 导出 JSONL。
沙箱 = rollout 环境；P1 轨迹 = 采集；P4 判分器 = 可验证奖励（RLVR 的核心：reward 必须
纯由环境给出，不要奖励模型）；JSONL = 与 veRL/TRL 的接口。

**流水线各环节**：
- `rollout`：每任务跑 `n_samples=8` 次（GRPO 组采样需要一组回答做组内比较，
  采样太少方差大），并发受信号量（默认 16）限制；每次 = 沙箱跑 AgentLoop → 抓轨迹 → 判分。
- `reward`：tests/command/file 判分（可验证）→ 0/1，可选部分分（过几条/共几条）；
  judge 判分 → 0~1 连续分，标 `source` 区分（可验证的优先用于 RLVR）。
- `filter`：① 按 (task_id, 轨迹哈希) 去重；② 剔噪声（工具错误率高的、没有工具调用的）；
  ③ rejection sampling：每任务留 top-k + **少量"从错误中恢复成功"的样本**（这类最值钱）；
  ④ 每任务上限防简单任务淹没难任务。
- `export`：关键转换——**block 模型 → OpenAI chat 格式**：
  assistant TextBlock → `{"role":"assistant","content":...}`；
  assistant ToolCallBlock → 同消息加 `tool_calls` 数组；
  user ToolResultBlock → `{"role":"tool","tool_call_id":...}`。
  `sft.jsonl` 只导 reward==1.0 的样本（冷启动教格式），`rlvr.jsonl` 导全部样本+reward。
- **v1 离线 reward**：reward 预先算好存进 JSONL，训练框架直接读，最解耦；
  v2 在线验证器服务（HTTP `POST /verify`）留给后续。

#### （6）MCP + Skills：把"外部能力"统一成"工具来源"

**统一抽象**：`ToolProvider.tools() -> list[Tool]`，`ToolRegistry` 聚合 + 按名字去重
（先注册优先，冲突记 warning）+ **fail-soft**（单个 provider 失败只记日志，
MCP server 不可达是常态，绝不能炸启动）。预热时取一次并缓存（MCP 连接跨轮复用，
stdio 子进程只 spawn 一次，shutdown 时统一回收）。

**MCP 链路**：`PI_MCP_SERVERS` 配置 JSON 数组（stdio 给 command，HTTP 给 url）→
`initialize` 握手 → `tools/list` → 每个 schema 包装成 `McpTool` →
模型调用时 `tools/call` 转发 → 结果（含 isError）转成 `ToolResult`。
**红利**：McpTool 就是 Tool，自动穿过 policy 黑名单、审计、沙箱、配额——不为新来源重做安全。
**安全边界**：MCP stdio = spawn 第三方进程，理想形态应在沙箱里启动（v1 记为 TODO）。

**Skills 链路**（两处 seam，别只做工具）：
- seam A（工具）：一个 `use_skill(name)` 工具返回 SKILL.md 正文（手动兜底）；
  **技能脚本不注册成工具**——技能一多就是工具爆炸，正文教模型用 bash 在沙箱里跑脚本。
- seam B（指令注入）：技能索引（name + 一行描述）启动时注入系统提示词
  （`<available skills>` 块），模型先知道"有哪些技能"，命中后用 use_skill 取正文。
- 三级渐进披露：① 索引常驻 prompt（最便宜）→ ② 正文命中后注入 → ③ 脚本执行时跑（不进上下文）。

#### （7）Web 前端三件套：零构建的取舍与底线

**取舍**：不引 React/Vue = 无 node 依赖、无构建链、改动即生效；代价是没有组件生态，
一致性靠**共享 CSS 变量**（一套颜色/字体 token，三页共用）+ 纪律维持。

**三页分工**：
- `app.html` 用户端：注册/登录（token 存 localStorage 的 `pi_token` 键，三页共享）、
  会话列表（建/切/删）、气泡式聊天（用户右/助手左、着色气泡贴头像、短文本气泡就短）、
  **工具卡默认折叠**（单行摘要 = 工具名 + 参数预览 + 完成/失败徽章，展开看参数和结果）、
  SSE 流式渲染（text_delta 打字机 / toolcall 状态 / compaction 标记 / turn_end 本轮 tokens）、
  顶栏用量面板（配额进度条 + 按模型明细）、"查看轨迹"入口（带会话参数跳转）。
- `trajectory.html` 轨迹视图：三种时间模式、两车道、轮次分隔线、双向联动、搜索过滤、
  "← 返回聊天"（回到同一会话）。
- `admin.html` 管理台：概览（readyz 状态卡 + 今日聚合 + 规模）、用户（配额内联编辑/
  禁用/强制下线——不能禁自己，后端和 UI 双重挡）、审计（按天/按用户/按工具过滤）。

**可靠性底线**（都是踩坑后加的）：HTML `no-cache`；未捕获 JS 错误显示为页面横幅；
重拉消息失败可见（不静默）；流式结束后**以服务端落库为准重拉历史**（流式只是预览，
不信任流式重建）；Playwright 布局断言（气泡间距 10px、对齐方向、内容列宽度集合唯一）。

---

## 第四部分 坑与解

> 每条都是本仓库真实发生过的，格式统一：**现象 → 排查 → 根因 → 解法 → 预防**。

### 4.1 "流式有回复，流一结束就消失"（三天悬案）

- **现象**：聊天页面里模型的回复逐字流式显示，流一结束整个对话消失。
- **排查**：curl 全链路正常；数据库里消息都在；前端无报错。
- **根因**：`GET /messages` 返回的 `blocks` 字段是**整个 Message 对象**（`{"role":..,"blocks":[..]}`）
  而不是数组；前端 `for (const b of m.blocks)` 直接抛异常；异常又被 `.catch(()=>{})` **静默吞掉**。
  渲染函数先清空页面再遍历 → 页面空了，错误没声。
- **解法**：服务端返回真正的数组（契约修正 + 回归测试钉死）；前端防御性兼容两种形状；
  **所有异步错误改为页面可见横幅，禁止静默 catch**。
- **预防**：API 返回形状必须写进测试（契约测试）；前端错误必须可见。

### 4.2 登录成功但所有请求 401（"Not enough segments"）

- **现象**：页面登录成功，随后一切请求报 `invalid token: Not enough segments`；curl/Swagger 全部正常。
- **根因**：一行 JS 少了 `await`：`token = r.json().access_token` 拿到的是 Promise 上的
  `undefined`，localStorage 存进字符串 "undefined" → 每次请求 `Bearer undefined`。
- **解法**：补 `await`。
- **预防**：**协议层 curl 全通 ≠ 页面没问题**。页面必须有真实浏览器验证（Playwright），
  并加上"未捕获错误可见化"的兜底。

### 4.3 修好了但用户看不到（浏览器缓存）

- **现象**：改了页面，用户反复说"按钮没有/页面没改"。
- **根因**：静态 HTML 被浏览器缓存（304）。
- **解法**：`/ui/*.html` 统一响应头 `Cache-Control: no-cache`——此后普通刷新即最新。

### 4.4 生产 `.env` 污染本地

- **现象**：本地裸跑 `pi-py serve`：要么带鉴权端点全部 500（生产 Redis 域名本地解析不了），
  要么启动即崩（`.env` 里 `PI_POLICY` 是服务器路径，本地不存在）。
- **根因**：`.env` 是从服务器拷来的生产配置，自动加载。
- **解法**：本地一律用 `.env.local`（shell 环境变量优先于 .env 文件）；
  完整本地栈文档化（`deploy/local-dev.md`）。
- **预防**：环境配置严格区分生产/本地；测试 conftest 把生产敏感变量全部钉死。

### 4.5 SQLite 不查外键，MySQL 查（18 个测试现原形）

- **现象**：把测试库从 SQLite 统一到 MySQL 后，18 个测试挂在外键约束上。
- **根因**：repo 直测用例直接用任意 `user_id`/`session_id` 写 memories/messages，
  SQLite 默认不强制外键所以一直"通过"，MySQL 一上全暴露。
- **解法**：conftest 每测试清表后**播种 20 个空用户 + 9 个空会话**作为 fixture 行；
  两个断言按新现实修正（用户列表断言改为子集）。
- **预防**：**测试与生产同库**（这正是统一 MySQL 的价值——SQLite 会掩盖真实约束）。

### 4.6 LLM 对模糊输入乱调工具

- **现象**：用户发了个"en ?"，模型先跑 `pwd; ls -la; git status` 探查工作区再回答。
- **根因**：系统提示词鼓励"能用工具查就别问用户"，模型把模糊输入理解为"先收集信息"。
- **解法**：提示词加硬约束：**闲聊/模糊短输入直接回答，不跑任何命令**。
- **预防**：真实模型回归验证（同 prompt 复测，确认工具调用数为 0）。

### 4.7 浏览器 EventSource 不支持 POST

- **现象**：流式聊天必须用 POST（run 端点是 POST）。
- **解法**：用 `fetch` + `ReadableStream` 手动解析 SSE 帧（`event:`/`data:` 行协议），
  处理跨 chunk 的帧切分与 UTF-8 多字节字符。
- **预防**：解析逻辑单独在 node 里原样复制验证过，与浏览器行为一致。

### 4.8 WSL 下 Windows 浏览器访问不到服务

- **现象**：WSL 里起了服务，Windows 浏览器打不开。
- **根因**：WSL2 `networkingMode=mirrored` 下 localhost 是共享的——服务没起或绑错地址才是主因；
  跨设备访问需 `--host 0.0.0.0` + 局域网 IP。
- **解法**：本地默认 `localhost:8300` 即可；文档写明两种场景。

### 4.9 页面布局"丑"的度量问题

- **现象**：用户反馈气泡长短不一、贴边不对——口头描述改了三轮还没对准。
- **根因**：视觉问题靠肉眼+文字反馈迭代，效率极低。
- **解法**：Playwright 无头浏览器**程序化断言**：气泡与头像间距（10px）、
  对齐方式（justify-content）、内容列宽度集合（必须是单一值）——每项可测、可回归。
- **预防**：布局改动后跑一次布局断言脚本。

### 4.10 PBKDF2 阻塞优化（453ms → 40ms）

- **现象**：登录时健康检查停顿 453ms，CPU 上不去。
- **根因**：PBKDF2 哈希在 asyncio 事件循环里同步执行。
- **解法**：`asyncio.to_thread` 移出事件循环（pbkdf2_hmac 在 C 层释放 GIL）。
- **结果**：453ms → 40ms，CPU 利用率 52.7% → 99.7%（纪律条目见 ARCHITECTURE §17.15）。

### 4.11 附属系统失败绝不能挂主流程

- **场景**：审计/计量/轨迹/指标任一步失败。
- **纪律**：全部 try/except + 日志，run 照常完成（有单测专门验证"轨迹落盘失败时 run 仍 200 且消息正常落库"）。
- **反面**：**主流程失败必须响亮**——启动配置错（如非法沙箱模式）直接拒绝启动，不做静默降级。

### 4.12 Web 工具的 SSRF 隐患（已解决：移除方案）

- **现象/风险**：`web_fetch`/`web_search` 在**应用进程内**用 httpx 直连目标 URL、
  不做任何地址校验、自动跟随重定向——等于给模型一个内网探测口子（云元数据服务
  169.254.169.254/100.96.0.96、内网服务都能被扫）。
- **解法（2026-09 已落地）**：**移除而非修补**——`web.py` 整体删除，工具集里不再存在
  进程内抓取工具（回归测试断言 `web_fetch/web_search not in all_tools()`）；沙箱内 bash
  抓取完全替代（Docker 断网时抓取走 `PI_SANDBOX_NET=host` 显式放行）。这是"漏洞关闭"的
  一种合法形态：修不好就删掉，而不是留着缓解措施自我安慰。
- **预防**：凡"应用进程对外发起请求"的代码，默认按"有 SSRF"审查。

### 4.13 指标标签爆炸

- **现象**：指标系统若把"原始路径/用户名/会话号"当标签，标签基数无界增长，
  Prometheus 内存会被打爆（每新用户/新会话都产生新时间序列）。
- **解法**：路由用 **pattern**（`/v1/sessions/{id}/runs`）不是 raw path；标签只有
  model/tool/status/route 四类，**永远不含 user/session/prompt**。未匹配路由归入
  固定标签 "unmatched"（防 404 扫描也刷标签）。

---

## 第五部分 测试体系

### 5.1 四层测试金字塔

| 层 | 内容 | 规模 | 依赖 | 命令 |
|---|---|---|---|---|
| **单测** | 逻辑/协议/安全/降级/契约，共 254 例 | 20 秒 | 本地 MySQL（pi_py_test）+ Redis；LLM/embedding/Milvus 用替身 | `.venv/bin/python -m pytest -q` |
| **真实栈集成** | 真 MySQL+Redis+Milvus+云 embedding | 2 例 | 完整本地栈 + 云 API | `PI_INTEGRATION=1 pytest integration/ -q` |
| **浏览器** | Playwright 无头 Chromium：登录/流式/布局/联动/缓存头 | 脚本 | 运行中的服务 | `/tmp/*.py` 脚本或未来 `tests/browser/` |
| **压测** | 沙箱容量、并发锁 | 2 工具 | Docker 沙箱 | `tools/sandbox_bench.py` `tools/loadtest.py` |

**测试纪律**（写进 conftest 与 ROADMAP）：
- 测试与生产**同库同栈**：基础设施没起 = 测试直接报错，不静默跳过（跳过的测试是死测试）；
- 每个测试从干净库开始（清表 + 播种 fixture 行）；环境变量全部钉死，防生产 `.env` 渗入；
- 服务替身分层：FakeProvider（脚本化模型）/FakeEmbedder/FakeVectorStore/FakeMcpServer
  是**测试替身**，不是 demo 环境——真实链路由 integration 层负责。

### 5.2 代表性测试样例（全部真实代码，可直接读 tests/）

**样例 1：契约测试（防 §4.1 复发）** —— `tests/test_server.py`
```python
def test_messages_blocks_contract_is_an_array(self, server):
    """API 契约："blocks" 是块数组，不是存储的整个 Message 对象。
    聊天页曾因对象形状在每次 run 后崩溃——流式文本闪现后消失。"""
    _register(server, "alice", "password123")
    token = _login(server, "alice", "password123")
    h = {"Authorization": f"Bearer {token}"}
    sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
    with server.stream(
        "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=h
    ) as resp:
        list(resp.iter_lines())
    msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
    assert msgs, "run should persist messages"
    for m in msgs:
        assert isinstance(m["blocks"], list), m
        for b in m["blocks"]:
            assert isinstance(b, dict) and "type" in b, b
```
要点：**把接口形状钉进测试**——这是三天悬案留下的纪律。

**样例 2：降级与兜底（向量记忆三档回退）** —— `tests/test_memory_vector.py`
```python
def test_search_falls_back_lexical_on_store_error(tmp_path):
    store = FakeVectorStore(scripted=RuntimeError("milvus down"))
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")
        hits = await repo.search(1, "api naming", k=2)
        assert hits and "snake_case" in hits[0].text   # Milvus 挂了用户无感
        await db.dispose()

    asyncio.run(main())
```
同类还有：embedding 失败回退、空命中回退、跨用户命中过滤、DB 缺失行跳过——
"依赖挂了，功能降级不消失"是这一组测试的主题。

**样例 3：安全回归（对仓库真实 policy.json）** —— `tests/test_security.py`
```python
MUST_DENY = [
    "rm -rf /", "rm -rf --no-preserve-root /", "sudo systemctl stop docker",
    "git push && sudo reboot", ":(){ :|:& };:", "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/vdb", "reboot", "init 0", "chmod -R 0777 /",
    "curl --unix-socket /var/run/docker.sock http://localhost/containers/json",
    "cat /etc/shadow", "cat /root/.ssh/id_rsa", "mount -o remount,rw /",
]
MUST_ALLOW = [
    "ls -la", "rm -rf ./build", "grep -rn 'shutdown' src/", "echo reboot later",
    "dd if=input.bin of=output.bin", "python -m pytest -q", "chmod 0777 ./tmp",
    "kill 4242", "npm run build && npm test", "grep -rn 'sudo' src/",
]

class TestShippedPolicy:
    def test_destructive_commands_are_denied(self):
        policy = server_policy(str(SHIPPED_POLICY))   # 仓库根的真实 policy.json
        leaked = [cmd for cmd in MUST_DENY
                  if check(policy, "bash", {"command": cmd}, REPO_ROOT).allowed]
        assert not leaked, f"policy.json lets these through: {leaked}"

    def test_ordinary_commands_are_still_allowed(self):
        policy = server_policy(str(SHIPPED_POLICY))
        blocked = [(cmd, check(policy, "bash", {"command": cmd}, REPO_ROOT).reason)
                   for cmd in MUST_ALLOW
                   if not check(policy, "bash", {"command": cmd}, REPO_ROOT).allowed]
        assert not blocked, f"policy.json false-positives on: {blocked}"
```
要点：**误杀比漏杀更伤**（一条日常命令被拦 = agent 干活被静默打断），所以两个方向都测，
而且测的是**实际生效的那份文件**，不是测试里的副本。

**样例 4：附属系统失败不挂主流程** —— `tests/test_trajectory_view.py`
```python
def test_persistence_failure_does_not_fail_run(self, tmp_path, monkeypatch):
    # "logs" 是个文件，mkdir(parents=True) 必然失败：落轨迹被堵死
    (tmp_path / "logs").write_text("blocking", encoding="utf-8")
    with _make_app(monkeypatch, tmp_path, str(tmp_path / "logs" / "traj.jsonl")) as client:
        h, sid = _register_and_session(client, "alice")
        with client.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h
        ) as resp:
            assert resp.status_code == 200
            lines = list(resp.iter_lines())
        assert "event: done" in lines          # SSE 正常结束
        msgs = client.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert len(msgs) >= 2                  # 消息照常落库
```
要点：用**真实的可失败路径**（父目录是文件）而不是 mock，验证 fail-soft 哲学。

**样例 5：轨迹时序增强的语义** —— `tests/test_trajectory_view.py`
```python
def test_llm_and_tool_ts_are_start_times(self, tmp_path):
    """ts 必须是调用起始时刻（时序图 time 模式依赖），不是记录时刻。"""
    provider = FakeProvider(model="demo", responses=[
        [ToolCallBlock(id="t1", name="write", arguments=json.dumps({"path": "a", "content": "x"}))],
        [TextBlock(text="done")],
    ])
    agent = AgentLoop(provider=provider, tools=all_tools(), system_prompt="sys", cwd=tmp_path)
    async def main():
        async for _ in agent.run("write a file"):
            pass
    asyncio.run(main())
    events = agent.trajectory.to_dict()["events"]
    llm1 = next(e for e in events if e["type"] == "LlmCall")
    tool = next(e for e in events if e["type"] == "ToolCall")
    llm2 = [e for e in events if e["type"] == "LlmCall"][1]
    assert llm1["ts"] <= tool["ts"] <= llm2["ts"]   # 单调：第1轮模型 → 工具 → 第2轮模型
    assert llm2["ts"] <= time.time() + 5            # 墙钟 sane
```
要点：把"微妙语义"（起始时刻 vs 记录时刻）变成可回归的断言。

**样例 6：连续拒绝熔断** —— `tests/test_trajectory.py`
```python
def test_five_consecutive_denials_abort_the_run(self, tmp_path):
    # 脚本 2 倍于熔断阈值：熔断必须发生在脚本耗尽之前
    responses = [[ToolCallBlock(id=f"c{i}", name="bash", arguments="{}")]
                 for i in range(MAX_CONSECUTIVE_DENIALS * 2)]
    agent = AgentLoop(
        provider=FakeProvider(responses=responses),
        tools=[BashTool()],                      # 工具必须存在才能到达策略门
        policy=Policy(deny_tools={"bash"}),      # 未知工具在策略前就报错，不算 denied
        max_turns=40,
    )
    ...
    tool_calls = [e for e in agent.trajectory.to_dict()["events"] if e["type"] == "ToolCall"]
    assert len(tool_calls) == MAX_CONSECUTIVE_DENIALS   # 恰好在阈值处停
    assert all(e["denied"] for e in tool_calls)
```
要点：**只有策略拒绝计数**（工具报错是学习信号，策略拒绝是死路）——这条语义也钉在测试里。

### 5.3 如何加一个新测试

1. 明确测的是**契约**（接口形状）还是**行为**（逻辑结果）还是**回归**（已知坑不再犯）；
2. 单测放 `tests/`，用服务替身；需要真实链路的放 `integration/`（PI_INTEGRATION=1）；
3. 涉及 app 的测试用 `TestClient(create_app(...))` fixture 模式，参考 `tests/test_server.py`；
4. 涉及 MySQL 数据断言：每个测试开始前库是干净的（conftest 自动清表 + 播种 u1..u20/s1..s9）；
5. 前端改动：跑 node 语法检查 + Playwright 脚本（布局断言）；
6. 全套 `.venv/bin/python -m pytest -q` 必须全绿——254 例是底线不是上限。

---

## 第六部分 数据与实测

| 指标 | 数值 | 来源/条件 |
|---|---|---|
| 测试规模 | **254 单测 + 2 真实栈集成**，20 秒跑完 | 本地 MySQL+Redis 统一栈 |
| 沙箱吞吐 | **~52 exec/s 饱和、零失败**（p50 44ms@N=1 → 1162ms@N=64） | `tools/sandbox_bench.py`，4 vCPU/16GiB，Docker 预热池 |
| 沙箱内存 | 每预热容器 ~26 MiB；64 容器冷启动 3.0s | 同上 |
| 登录哈希 | **PBKDF2 453ms → 40ms**（CPU 52.7% → 99.7%） | `asyncio.to_thread` 优化 |
| 真实模型一轮 | ~2,103 tokens in / 147 out（qwen3.8-max，一次短任务） | 本地 E2E 实测落库 |
| 记忆向量 | 一次召回 embedding 15~28 tokens（qwen3.7-text-embedding） | 计量记录实测 |
| 轨迹落盘 | 每 run 一行 JSON（含全部事件 + ts），按天滚动 | 本地实测 |
| 端到端延迟 | 服务端 0.7s 出首个 token（fake）；真实模型由云 API 决定 | 实测 |
| CubeSandbox 超时 | `sleep 30` timeout=3 → 3.2s 返回 `timed_out=True`，`pgrep -c sleep`=0（零残留） | 真机故障注入探针 |
| 企业 eval | 真实模型端到端冒烟 29s / 4 工具 / 断言全过；eval 5/5 | docs/production-readiness.md |

**成本提示**：真实模型每次调用花钱（token 计费）。UI 调试期不要用真实模型反复回归——
布局/流式验证优先用 fake 模型脚本 + Playwright，验收时用真实模型跑一遍即可。

---

## 第七部分 待实现与路线图

详见 `ROADMAP.md` §3/§4，此处列概览：

| 优先级 | 事项 | 一句话 |
|---|---|---|
| 中 | 管理员会话浏览器 | 管理员查看任意用户会话/轨迹（需 admin_* 端点） |
| 中 | eval 补全 | 自动抽任务、regress、badcase 归因 |
| 低 | checkpoint 接 server | 超时后从断点恢复（loop 钩子已就绪） |
| 后续 | **RAG 企业知识库** | 铁律：先评测再调检索；v1=解析+切块+混合检索(BM25)+rerank+引用+ACL；与记忆共用 Milvus/embedding |
| 后续 | 文件上传 + MinIO | 与 RAG 解析层共用 parser；配额+解压炸弹防护 |
| 后续 | SSO/RBAC | 现在只有 JWT + admin 开关 |
| ~~后续~~ 已落地 | CubeSandbox microVM（`PI_SANDBOX=cubesandbox`，KVM 前置 + 云 runbook 见 docs/） |

---

## 附录C 目录结构导读（10 秒看懂仓库）

```
src/pi/
├── llm/           模型接入层：openai/anthropic 提供商、统一流事件、降级链、think_filter、embedding
├── agent/         智能体核心：loop（循环/压缩/断点）、trajectory（P1 事件日志）、compaction
├── tools/         工具层：10 内置 + subagent + memory + mcp + skill + registry + sandbox
├── server/        FastAPI 服务：app（路由）、runner（RunManager）、db（ORM+repo）、
│                  cache（Redis/内存）、auth、config、ratelimit、trajectory_store、
│                  vectorstore（Milvus）、static（三个前端页面）、archive.py（会话归档）
├── security/      policy（策略/路径沙箱）、audit（审计）、redact（脱敏）
├── observability/ tracing、metrics（Prometheus）、metering（用量/配额/成本）、prices
├── evals/         评估与飞轮：schema/load/runner/scorers/report（P4）
│                  + rollout/reward/filter/export（RL 数据飞轮）
├── cli.py         入口：serve / migrate / eval run|diff|rollout
└── prompt.py      系统提示词
tests/             254 个单测（连本地 MySQL/Redis）
integration/       真实栈集成测试（PI_INTEGRATION=1）
tools/             压测/播种脚本（sandbox_bench、loadtest、seed_testdb）
deploy/            本地/生产 compose、环境模板、部署文档
docs/              CubeSandbox 设计笔记、生产部署手册、生产就绪审计
migrations/        Alembic 迁移（0001~0006）
```

## 附录D 环境变量速查（关键项，完整清单见 ARCHITECTURE §13）

| 变量 | 作用 | 备注 |
|---|---|---|
| `PI_DATABASE_URL` | 数据库连接（mysql+aiomysql / postgresql+asyncpg） | 必填，服务硬依赖 |
| `PI_REDIS_URL` | Redis（锁/限流/撤销） | 多实例必配；单实例可内存降级 |
| `PI_MODEL` | 默认模型 | 本地用 openai/qwen3.8-flash |
| `PI_FALLBACK_CHAIN` | 降级链（逗号分隔） | 主模型挂了自动切 |
| `PI_SANDBOX` | 沙箱模式（docker / 空=进程内） | 非法值启动即拒绝 |
| `PI_SANDBOX_MEMORY/PIDS/CPUS` | 容器限额 | 1g / 256 / 1.0 |
| `PI_SANDBOX_NET` | `host` 才开网络 | 默认断网 |
| `PI_POLICY` | 策略文件路径 | server 模式只加不减 |
| `PI_TRAJECTORY_PATH` | 轨迹 jsonl 路径 | `""` = 关闭落盘 |
| `PI_EMBEDDING_*` ×3 + `PI_MILVUS_URI` | 向量记忆四件套 | 缺一即词法检索 |
| `PI_MCP_SERVERS` | MCP 服务器 JSON 数组 | 格式错只告警不炸启动 |
| `PI_SKILLS_DIR` | 技能包目录 | 空 = 不加载 |
| `PI_JWT_SECRET` | 令牌签名密钥 | 多实例必须一致，轮换=全员登出 |
| `PI_MAX_CONCURRENT_RUNS` | 全局并发信号量 | 默认 8 |
| `PI_METRICS_TOKEN` | /metrics 访问令牌 | 错误令牌答 404（不暴露存在性） |
| `PI_CUBE_API_KEY` | CubeSandbox（E2B 兼容 API）密钥 | `PI_SANDBOX=cubesandbox` 时必配 |
| `PI_SANDBOX_CLOSE_TIMEOUT_SECONDS` | 沙箱 close/save 总超时 | 默认 90s，超时 turn 先走、清理线程收尾 |
| `PI_ARCHIVE` / `PI_ARCHIVE_DIR` / `PI_ARCHIVE_S3_*` | 会话归档开关/目录/MinIO 上传 | 默认开启，落 `~/.pi-py/archives` |

## 附录E 轨迹 JSON 样例（真实落盘的一行）

```json
{"run_id": "0d260d973ba1", "started_at": 1790517200.8,
 "session_id": "e3d4046d61a4", "user_id": 1,
 "events": [
   {"type": "RunStarted", "run_id": "0d260d973ba1", "session_id": "e3d4046d61a4",
    "user_id": "zhu", "model": "openai/qwen3.8-max", "cwd": ".../workspaces/zhu",
    "tools": ["bash","edit","find","grep","ls","read","recall","remember",
              "spawn_subagents","write"],
    "prompt": "只回复：布局测试完成", "ts": 1790517200.8},
   {"type": "LlmCall", "turn": 1, "model": "openai/qwen3.8-max",
    "input_tokens": 2103, "output_tokens": 147, "stop_reason": "end_turn",
    "latency_ms": 4312, "text": "布局测试完成", "tool_calls": [],
    "ts": 1790517200.9},
   {"type": "RunFinished", "input_tokens": 2103, "output_tokens": 147,
    "turns": 1, "latency_ms": 4420, "ts": 1790517205.2}
 ]}
```

读法：run 开头记录上下文（谁、什么模型、什么提示词、有哪些工具）；
每个 LlmCall 记录模型一轮的完整输入输出和耗时；ToolCall 记录每个工具的参数/结果/成败；
结尾汇总。这一行 JSON 就是前端时序图、评估判分、RL 数据导出的**全部原料**。

---

---

## 附录A 术语表

| 术语 | 白话解释 |
|---|---|
| Agent | 能自己决定"下一步做什么"的 AI 程序（想→做→看结果→再想） |
| 工具（Tool） | 给模型用的"手"：bash/读写文件/搜索等，有名字、参数、返回结果 |
| 沙箱（Sandbox） | 隔离的执行环境（Docker 容器），模型在里面随便折腾，出不了边界 |
| 轨迹（Trajectory） | 一次运行的完整事件日志：模型每轮想了什么、每个工具怎么调、耗时多少 |
| 上下文压缩（Compaction） | 对话太长时，把旧历史总结成一段摘要，省 token |
| 向量检索 | 把文字变成数字向量，按"语义相近"找内容（能搜到"意思相近但用词不同"的） |
| MCP | 模型上下文协议：给 AI 接外部工具的标准接口（像 USB-C） |
| Skills | 技能包：一份指令文档教模型"怎么干某类活"（可复用、可积累） |
| eval | 评测：用任务集+判分器量化模型/系统"干得好不好" |
| RL / RLVR | 强化学习 / 可验证奖励：用"任务过没过"当奖励信号训模型 |
| SFT | 监督微调：拿高质量问答对教模型格式和基础能力 |
| rollout | 批量跑 N 遍任务，采集轨迹和得分（训练数据的原料） |
| SSE | 服务器推送事件：一条连接上持续推送流式输出 |
| 多租户 | 一套系统服务多个用户，互相隔离（配额/限流/审计） |
| fail-soft | 附属功能失败不拖垮主功能（记账失败 ≠ 任务失败） |
| 投影 | 同一份数据的不同视角：审计=脱敏视角，指标=聚合视角，轨迹=原始视角 |

## 附录B 5 分钟跑起来（完整步骤与排障见 `deploy/local-dev.md`）

```bash
# 1) 基础设施（MySQL + Redis + Milvus，本机已有 Redis/Milvus 时只起 mysql）
docker compose -f deploy/docker-compose.local.yml up -d

# 2) 环境变量（模板 deploy/env.local.example，云 API key 从生产 .env 复制）
cp deploy/env.local.example .env.local && vi .env.local

# 3) 建表 + 起服务
set -a; source .env.local; set +a
pi-py migrate
pi-py serve                        # http://localhost:8300

# 4) 浏览器打开 /ui/app.html 注册即用；管理台 /ui/admin.html
#    （admin 写库授予：UPDATE users SET is_admin=1 WHERE username='...'）

# 5) 测试（本地 MySQL/Redis 必须在跑）
.venv/bin/python -m pytest -q              # 254 例
PI_INTEGRATION=1 pytest integration/ -q    # 真实栈 2 例
```

---

*本文档与 `README.md`（门面）、`ARCHITECTURE.md`（技术手册）、`ROADMAP.md`（路线图）配套阅读；
实现状态以代码为准，文档可能滞后。最后更新：2026-09-27。*

