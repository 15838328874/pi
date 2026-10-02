# pi-py 开发者手册（ARCHITECTURE.md）

> 面向后来人的完整说明：项目是什么、怎么设计的、每个模块每个函数干什么、
> 如何启动和使用、有哪些坑。读完本文 + `README.md`，你应该能独立维护和扩展这个项目。
>
> 最后更新：2026-10-02 · 代码规模约 10,000 行源码 + 267 个测试
>
> **文档地图**（四个文档各管一段，知识点不重复）：
>
> | 文档 | 定位 | 什么问题看它 |
|---|---|---|
| `README.md` | 门面 | 这是什么、怎么装、怎么跑（快速上手入口） |
| `PROJECT_GUIDE.md` | 叙事与价值 | 为什么这么设计（取舍）、踩过什么坑（故事版）、测试样例与实测数据 |
| `ARCHITECTURE.md` | 技术手册 | 每个模块每个函数、配置全表（§13）、坑清单（§17）、差距清单（§19） |
| `ROADMAP.md` | 状态与路线图 | 什么做完了、什么没做、下一步做什么（含环境区分表） |
| `docs/`（三件） | CubeSandbox 专项 | 沙箱设计笔记 / 生产部署手册 / 生产就绪审计——专项文档，不重复核心四文档内容 |

>
> 本包已收敛为**纯服务端形态**：本地单人 CLI/TUI、本地 SQLite 会话存储、
> Windows/WSL 支持均已移除（见 §12）。

---

## 1. 项目定位

pi-py 是 **earendol-works/pi**（TypeScript 版编码智能体外壳）的 **Python 实现**。
定位（与 §20 战略定位一致）：**可自托管、可扩展工具、可评估、可沉淀训练数据的 AI 智能体平台**——
编码智能体是第一个深度打磨的场景，MCP/Skills/ToolProvider 让工具能力不受场景限制。

它只有**一个运行形态**：多用户服务（`pi-py serve` + HTTP/SSE API）。内核
（`pi.agent` + `pi.llm` + `pi.tools`）与运行形态无关，可以单独 import 使用，
但仓库里不再有本地单人入口。

| 形态 | 入口 | 场景 |
|---|---|---|
| **多用户服务** | `pi-py serve` + HTTP/SSE API | 企业级服务：JWT 登录、配额、限流、审计、沙箱（CubeSandbox microVM / Docker） |

> 历史上还有"本地单人"形态（`pi-py chat / tui / run`，SQLite 存会话）。它已连同
> `pi/tui/`、`pi/session/`、`pi/env.py` 一起删除；`pi.cli` 只剩 `serve` / `migrate`。

设计上的北极星原则（理解所有代码的钥匙）：

1. **用户见过的必落库，用户没见过的绝不落库**：write-ahead 先存用户消息、工具轮边界增量续存，一次事务一段，崩溃最多丢当前轮（细节与取舍见 `docs/run-durability-design.md`）。
2. **每个 session 同一时刻只有一个 run**：分布式锁保证，跨实例也成立（Redis 后端）。
3. **安全策略是闸门，不是装饰**：每次工具调用都过 `policy.check()`，拒绝即审计、即报错给模型。
4. **附属系统永不阻塞主流程**：计量、审计、回调失败只记日志，绝不让 run 失败。
5. **能不依赖就不依赖**：不配 Redis 退化为进程内实现（单实例语义不变）；
   不配 API key 可用 `fake/demo` 跑通全流程。**但数据库是硬依赖**——
   `PI_DATABASE_URL` 缺失时服务直接拒绝启动，不再有 SQLite 兜底。

---

## 2. 快速开始

### 2.1 环境要求

- **Linux**（Windows/WSL 分支已删除；容器化部署亦为 Linux）
- Python ≥ 3.11（代码用了 `X | Y` 类型语法、`asyncio.timeout`）
- 一个 MySQL 或 PostgreSQL 实例——**这是硬依赖**，没有本地文件兜底
- Redis 可选（多实例部署时才必需）；模型 API key 可选（`fake/demo` 免 key）

### 2.2 安装

```bash
cd pi-python
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[production]"  # 可部署的服务：asyncpg + aiomysql + redis + alembic + otel
pip install -e ".[dev]"         # 只跑测试：pytest + aiosqlite（兜底）；测试统一连本地 MySQL/Redis（见 §15）
```

**本地/生产环境搭建**（MySQL + Redis + Milvus 三件套、环境变量区分、账号与排障）：
`deploy/local-dev.md`（本地测试）与 `deploy/cloud-deploy.md`（生产 runbook）。

裸 `pip install -e .` 只装内核与 FastAPI/SQLAlchemy，**不带任何数据库驱动**，
起不了服务——要么 `[production]`，要么至少 `[mysql]` / `[postgres]` 之一。

### 2.3 三十秒体验（无需任何 API key）

```bash
export PI_DATABASE_URL="mysql+aiomysql://user:pass@host:3306/pi_py"
export PI_JWT_SECRET="$(openssl rand -hex 32)"
export PI_MODEL=fake/demo          # 脚本化 provider，返回固定演示文本
pi-py migrate
pi-py serve --host 127.0.0.1 --port 8300
```

另开一个终端走一遍完整链路：

```bash
curl -X POST :8300/v1/auth/register -d '{"username":"alice","password":"password123"}'
TOKEN=$(curl -s -X POST :8300/v1/auth/login -d '{"username":"alice","password":"password123"}' | jq -r .access_token)
SID=$(curl -s -X POST :8300/v1/sessions -H "Authorization: Bearer $TOKEN" -d '{"title":"t"}' | jq -r .id)
curl -N -X POST :8300/v1/sessions/$SID/runs -H "Authorization: Bearer $TOKEN" -d '{"prompt":"你好"}'
```

`fake/demo` 下所有 HTTP 语义（鉴权、隔离、SSE、限流、配额、锁）都是真的，
只有模型回复是罐头文本——这正是测试套件零成本的原因（§15）。

### 2.4 接真实模型

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://your-compatible-endpoint   # 任何 OpenAI 兼容端点（阿里云/DeepSeek 等）
export PI_MODEL=openai/qwen3.8-max
export PI_FALLBACK_CHAIN="openai/qwen3.8-flash"           # 可选：降级链
```

模型只能经环境变量配置——`--model` 参数随本地 CLI 一起删除了。

### 2.5 启动多用户服务

```bash
export PI_JWT_SECRET=<openssl rand -hex 32 生成>
export PI_DATABASE_URL=mysql+aiomysql://user:pass@host:3306/pi_py   # 必填，缺失即启动失败
export PI_REDIS_URL=redis://user:pass@host:6379/0                   # 多实例必填
pi-py migrate
pi-py serve --host 0.0.0.0 --port 8300
```

配置项全表见 §13；云端内网部署的完整 runbook 见 `deploy/cloud-deploy.md`。

注册对所有人开放，注册即普通用户；管理员身份只能由运维直接改库授予
（`UPDATE users SET is_admin=1 WHERE username='alice';`），代码里没有任何提权路由。
详见 §11.3。

---

## 3. 整体架构

### 3.1 分层图

```
┌──────────────────────────────────────────────────────────────┐
│  入口层                                                       │
│  cli.py (argparse: serve / migrate)   server/app.py (FastAPI)│
├──────────────────────────────────────────────────────────────┤
│  编排层                                                       │
│  server/runner.py (RunManager：锁 / 背压 / 超时 / SSE 序列化) │
├──────────────────────────────────────────────────────────────┤
│  智能体核心（与运行形态无关）                                  │
│  agent/loop.py (AgentLoop)  agent/compaction.py  events.py   │
├──────────────────────────────────────────────────────────────┤
│  能力层                                                       │
│  tools/* (9 个工具 + sandbox)   llm/* (provider 适配 + 降级链)│
├──────────────────────────────────────────────────────────────┤
│  横切层（安全 / 可观测 / 存储）                                │
│  security/*   observability/*   server/db + server/cache     │
└──────────────────────────────────────────────────────────────┘
```

**依赖方向是单向的**：上层依赖下层，`agent/` 不知道 `server/` 的存在。
这是刻意的——智能体核心可以脱离 HTTP 服务独立测试和使用。

### 3.2 一次对话回合（turn）的完整数据流

以多用户服务为例，这是理解全系统的主线：

```
客户端 POST /v1/sessions/{id}/runs  (prompt, Bearer token)
  │
  ├─ current_user 依赖：解码 JWT → 查 jti 黑名单 → 查用户撤销纪元 → 查库确认激活
  ├─ 限流检查（每用户固定窗口，超限 429）
  ├─ 配额检查（当月已用 token 超限 402）
  │
  ▼ 返回 SSE 流（StreamingResponse）
RunManager.run_turn
  ├─ cache.acquire_lock("session:{id}")      ← 失败则 SSE 内发 ErrorEvent 并结束
  ├─ semaphore.acquire()                     ← 全局并发上限（背压）
  ├─ 加载历史消息（MessageRepo）
  ├─ 装配 AgentLoop（工具 + 策略 + 审计 + tracer）
  ├─ sandbox 预热（docker 池模式下与 LLM 首响应并行）
  ├─ 用户消息 write-ahead 落库（首个 SSE 事件发出前，流内触发）
  ├─ async with asyncio.timeout(600s):
  │    AgentLoop.run(prompt):
  │      追加用户消息 → 调 provider.stream() 流式收事件
  │        → 模型要工具？→ policy.check() → 执行 → 结果回喂 → 再来一轮
  │        → 不要工具？→ 结束
  │      （历史超阈值先压缩；每步写审计和 trace span；
  │        每个 toolcall_start 前把已定稿消息 append_many 增量落库）
  ├─ 收尾：剩余消息 MessageRepo.append_many（一次事务，含失败场景）
  ├─ 成功：UsageTracker.record（失败只记日志，不回滚对话）
  └─ finally: 释放 session 锁
```

### 3.3 目录结构

```
pi-python/
├── src/pi/
│   ├── models.py            消息数据模型（全部代码的通用语言）
│   ├── prompt.py            系统提示词
│   ├── cli.py               argparse 入口：serve / migrate
│   ├── llm/                 LLM 适配层
│   │   ├── base.py          流式协议（StreamEvent）
│   │   ├── registry.py      "provider/model" 字符串 → 实例
│   │   ├── openai_provider.py / anthropic_provider.py / fake.py
│   │   ├── fallback.py      重试 + 模型降级链
│   │   └── think_filter.py  剥离 <think> 推理段的流式状态机
│   ├── agent/
│   │   ├── loop.py          AgentLoop：对话-工具循环
│   │   ├── events.py        事件类型（序列化只在 server/runner.py 的 SSE 侧）
│   │   └── compaction.py    上下文压缩（LLM 摘要 + 保留尾部）
│   ├── tools/
│   │   ├── base.py          Tool 抽象 + ToolContext + 公共工具函数
│   │   ├── bash.py read.py write.py edit.py grep.py find.py ls.py web.py
│   │   └── sandbox.py       命令执行隔离：LocalRunner / Docker 冷路径/预热池 / CubeSandboxRunner（microVM）；
│   │                        SandboxLimits（内存/pids/cpu/user）在四处建容器路径统一生效
│   ├── security/
│   │   ├── policy.py        策略引擎（拒绝清单/命令模式/路径沙箱）
│   │   ├── redact.py        出站脱敏（发给模型前遮蔽密钥）
│   │   └── audit.py         审计（jsonl 按天滚动 + audit_events 表双写）
│   ├── observability/
│   │   ├── tracing.py       noop/jsonl/otel 三后端 span
│   │   ├── metering.py      用量记录 + 月度汇总 + 配额检查
│   │   └── prices.py        模型单价表 + 成本估算
│   └── server/              多用户服务（FastAPI）；`__init__.py` 惰性导出 create_app，
│       │                    只 import db.py 不会连带拉起整个 app（缘由见 §17）
│       ├── config.py        ServerSettings.from_env（所有环境变量）
│       ├── app.py           路由 + 认证依赖 + 启动引导
│       ├── runner.py        RunManager + SSE 序列化
│       ├── db.py            SQLAlchemy ORM + 仓储（MySQL / PG 通用）
│       ├── cache.py         缓存/锁后端（内存 / Redis）
│       ├── auth.py          PBKDF2 哈希 + JWT
│       ├── ratelimit.py     每用户固定窗口限流
│       ├── archive.py        会话工作区归档（tar.gz + 差异元数据 + MinIO 惰性上传）
│       ├── storage.py         MinIO/S3 文件管线（预签名直连 + sha256 去重）
│       └── client.py         SDK（异步 HTTP 客户端，SSE 流式解析）
├── tests/                   267 个测试（连本地 MySQL/Redis，服务替身分层）
├── migrations/              Alembic 迁移（0001 建表 ~ 0007 files）
├── docs/                    CubeSandbox 设计笔记 / 生产部署手册 / 生产就绪审计（专项文档）
├── tools/loadtest.py        SSE 压测工具
├── tools/seed_testdb.py     给 *_test 库灌可复用的测试数据（幂等，拒绝跑在生产库上）
├── tools/sandbox_bench.py   docker 预热池容量压测（直打 sandbox 层，扫并发用户数）
├── deploy/                  Caddyfile（SSE 友好 TLS）、云端部署手册、.env 模板
├── policy.json              服务端安全策略（`PI_POLICY` 指向它；两个 compose 也挂这一份）
├── Dockerfile               多阶段镜像（含 alembic，支持 `pi-py migrate`）
├── docker-compose.local.yml 本地测试基础设施（MySQL+Redis+Milvus）
└── docker-compose.cloud.yml 云端变体（指向托管 MySQL/Redis 内网）
```

---

## 4. 数据模型 `src/pi/models.py`

全系统的通用语言，所有层都围绕它转。

| 类型 | 作用 | 设计说明 |
|---|---|---|
| `Role` | system/user/assistant 枚举 | 只有三种角色；工具结果以 **user 角色**回喂（Anthropic 风格），各 provider 自己翻译成平台线格式 |
| `TextBlock` | 纯文本内容 | `type: "text"` |
| `ToolCallBlock` | 模型发起的工具调用 | `arguments` 存 **JSON 字符串**而非对象——保持与线格式一致，解析推迟到执行时（§7.3），坏 JSON 能被优雅拒绝 |
| `ToolResultBlock` | 工具执行结果 | `tool_use_id` 关联调用，`is_error` 让模型知道失败并自我纠正 |
| `Block` | 三者的判别联合 | `Field(discriminator="type")`：Pydantic 按 `type` 字段精准分发，比逐个尝试更快、报错更明确 |
| `Message` | role + blocks 列表 | 一条消息可混合文本与多个工具块（如"解释一下 + 调用工具"） |
| `Usage` | 输入/输出 token 数 | `add()` 用于跨轮累计 |
| `ToolSpec` | 工具声明（name+description+JSON Schema） | 直接喂给模型的 tools 参数 |

**为什么用 block 模型而不是纯字符串？** 因为工具调用是结构化往返：模型发调用 →
系统执行 → 结果必须按 `tool_use_id` 精确回喂。block 模型是 OpenAI/Anthropic
两家线格式的公约数，`llm/` 层只做翻译，核心逻辑对厂商无感知。

---

## 5. LLM 层 `src/pi/llm/`

### 5.1 `base.py` — 流式协议

```
LLMProvider.stream(system, messages, tools) -> AsyncIterator[StreamEvent]
```

契约：**先**产出 0+ 个 `TextDelta`，**再**产出 0+ 个完整的 `ToolCallDelta`
（id+name+完整 arguments），**最后**恰好一个 `StreamEnd(stop_reason, usage)`。

- 为什么 ToolCallDelta 是"完整的"而不是增量片段？——各厂商的流式工具参数分片方式
  不同（OpenAI 按 index 累积、Anthropic 按 content_block 累积），把累积逻辑关在
  provider 内部，上层（AgentLoop）拿到的永远是完整调用，简化了循环逻辑。
- `stop_reason` 归一为 `"end_turn" | "tool_use"`，屏蔽厂商差异。

### 5.2 `registry.py` — 模型解析

| 函数 | 作用 |
|---|---|
| `resolve(model)` | `"openai/gpt-4o"` → `OpenAIProvider` 实例。按前缀分发，API key/base_url 从 kwargs 或环境变量取 |
| `resolve_chain(model, chain)` | 主模型 + `PI_FALLBACK_CHAIN` 降级链 → 包成 `FallbackProvider`；链为空则裸返回主模型 |
| `DEFAULT_MODEL` | 读 `PI_MODEL` 环境变量，默认 `openai/gpt-4o` |

### 5.3 `openai_provider.py` / `anthropic_provider.py` — 厂商适配器

两者结构对称：`_to_wire()` 把 block 消息翻译成平台格式，`stream()` 消费平台流、
归一成 §5.1 的三种事件。

**OpenAIProvider 要点**：
- `stream_options={"include_usage": True}`：否则流式拿不到 token 用量；
- 工具参数按 `index` 分槽累积（OpenAI 的 tool_calls 是分片下发的）；
- 累积完对 arguments 做 `json.loads` 健全性检查，坏 JSON 替换为 `{}`（执行层还有一道防线）；
- 内容流经 `ThinkFilter`（§5.5）；
- `finish_reason == "tool_calls"` 归一为 `"tool_use"`。
- 兼容任何 OpenAI 协议端点（`OPENAI_BASE_URL`），这是接国产模型的方式。
  生产部署推荐前置**聚合网关**（cloud compose 内置 `llm-gateway` = new-api）：
  pi-py 只指向网关一个端点，网关按渠道把流量分给多家厂商、QPS 打满自动切换
  （配置与演练见 `deploy/cloud-deploy.md` §2.5）。

**AnthropicProvider 要点**：
- `MAX_TOKENS = 8192`（Anthropic 必填参数）；
- 用 `client.messages.stream()` 上下文，`content_block_start/delta` 事件驱动；
- 结尾 `get_final_message()` 拿 stop_reason 和 usage。

### 5.4 `fallback.py` — 重试与降级

`FallbackProvider(primary, fallbacks)`：对链上每个模型最多试
`1 + _MAX_RETRIES_PER_MODEL`(2) 次，指数退避（0.5s、1s），全灭才抛错。

关键设计：
- `_is_transient()` 只认瞬时故障（连接/超时/429/5xx 类）。**参数错误、内容错误直接抛出**
  ——换模型重试解决不了坏请求，只会浪费钱。
- `got_event` 守卫：**流已经吐出事件后发生的错误不重试**——异步生成器无法回滚已
  yield 的内容，重试会让下游看到重复片段。宁可失败。
- 降级时触发 `on_fallback` 回调（通知/埋点用），回调异常被吞掉——通知不能打断主流程（原则 4）。

### 5.5 `fake.py` 与 `think_filter.py`

- `FakeProvider`：脚本化应答（构造时传 `responses` 队列），按 16 字符分片模拟流式。
  测试与无 key 演示的基石——**整个测试套件不花一分钱 API 费用全靠它**。
- `ThinkFilter`：剥离 qwen/deepseek 风格模型在正文里内联的 `<think>...</think>` 推理。
  难点是**标签可能跨 chunk 边界**：`_longest_suffix_prefix()` 计算缓冲区末尾是否正
  在拼凑标签的前几个字符，是则扣留不发，等下个 chunk。`feed()` 喂入、`flush()` 收尾。

---

## 6. 智能体核心 `src/pi/agent/`

### 6.1 `loop.py` — AgentLoop（全项目的心脏）

构造参数（都有合理默认值，服务层会全量传入）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `provider` | 必填 | §5 的流式提供者 |
| `tools` | 必填 | `Tool` 实例列表，内部建成 name→tool 字典 |
| `messages` | `[]` | 历史（服务层从库里反序列化后注入） |
| `cwd` | 当前目录 | 工具的工作目录（= 会话的 workspace） |
| `on_message` | None | 每条新消息的回调（服务层 write-ahead + 工具轮边界增量落库 + 收尾合批，见 §11.5） |
| `max_turns` | 40 | 防死循环：模型反复调工具的硬上限 |
| `compact_threshold` / `compact_keep` | 80,000 / 8 | 压缩触发阈值（字符数）/ 压缩后保留的尾部消息数 |
| `policy` / `audit` / `tracer` | None | 安全策略 / 审计 / 追踪（§9、§10） |
| `session_id` / `user_id` | — | 只用于审计和 trace 打标 |

方法逐个说：

| 方法 | 作用 |
|---|---|
| `run(user_text, resume_from=None)` | 公开入口：追加用户消息 → 包一层 `agent.run` trace span → 委托 `_run_inner`。传 `resume_from`（Checkpoint）时改为续跑：恢复历史/用量/轮数，不追加新用户消息，并进入**重放模式**（completed_tools 账本生效，见下） |
| `_run_inner` | 主循环：① 超阈值先压缩；② while 循环：调 `provider.stream`（出站消息先过 `redact_messages` 脱敏）、收 TextDelta 边收边 yield、收 ToolCallDelta 按 id 攒参数；③ 有工具调用且 `stop_reason=="tool_use"` 就逐个 `_run_tool` 执行、结果作为一条 user 消息回喂、继续循环；否则跳出；④ 任何异常转成 `ErrorEvent`（UI 永远能收到结构化结果）；⑤ 最后必发 `TurnEndEvent(usage, turns)` |
| `_run_tool(call)` | 单工具执行的完整管线：未知工具→错误块；解析 arguments（必须为 JSON 对象）→失败→错误块；`policy.check()`（含能力授权）拒绝→错误块+审计拒绝记录；**重放模式命中 completed_tools 账本→直接回填记录的结果，不重执行**；执行崩溃→错误块+审计；成功→结果块+审计+写入账本。错误块（`is_error=True`）会回喂给模型，让它知道失败并可自我纠正。账本只记成功返回的结果——被拒/未知/崩溃的调用 resume 时重新执行 |
| `_maybe_compact` | 估算大小超阈值则调 `compact()`，替换 `self.messages`，发 `CompactionEvent` |
| `_audit` | 审计写入封装：出站参数也先脱敏再记 |

**Checkpoint 与断点重放**（P2 durable execution）

`Checkpoint` 字段：`messages`（完整历史）、`input_tokens` / `output_tokens`（续计）、`turns`、
`completed_tools`（幂等账本：key → `{"content", "is_error"}`）。loop 在每轮结束经
`on_checkpoint` 钩子把它吐给调用方落库；`run(resume_from=...)` 时整体恢复。

`completed_tools` 是 **P0-2 幂等重放**的载体：key = 工具名 + `sort_keys` 规范化 JSON 参数。
resume 时命中的工具**重放记录的结果**，不再执行副作用——只有正常返回的结果才记账，
被拒/未知/崩溃的调用 resume 时重新执行；重放只在 resume 模式生效，正常 run 里同参数
重复调用不会被误判（`tests/test_durable.py` 钉住两条边界）。

**为什么 `run` 是异步生成器（yield 事件）而不是返回最终结果？**
因为消费端（HTTP SSE 客户端）需要在过程中实时渲染——模型每吐一个字、每调一个工具
都要即时可见。事件流是唯一不需要缓冲整轮的方案。

**多工具调用的并行 / 串行（当前实现）**

模型在一次回复里**可以**同时吐多个 tool call（`calls` 列表，一个 assistant 消息带多个
工具块）；但 `_run_inner` 的执行是**严格串行**的：

```python
for call in calls:                        # 模型发多少个，都排队
    outcome = await self._run_tool(call)  # 逐个 await，无 asyncio.gather
```

要点：

- **谁决定并行/串行？模型自己**，通过"批量粒度"表达——一次发多个 = 它认为这些调用
  相互独立（可并行）；一次只发一个、等结果再发下一个 = 有依赖、必须串行。
- **当前代码没有兑现"并行"**：批量也被降级为串行执行（结果按 `calls` 原序回填，
  打包成一条 user 消息一起回喂）。所以独立子任务不会真的并发提速；有依赖的调用
  必须在**跨回合**完成——同一批里 A 的结果对 B 不可见。
- **串行是当前的合理默认**：工具大多共享同一 workspace（文件系统有状态），并发写
  同一路径会竞争；串行顺序确定、无竞态。若要真并行，只适合对**只读、无副作用**的
  工具（read/grep/find/ls）开 `gather`，写类工具保持串行——这是待办优化项。

### 6.2 `events.py` — 事件词汇表

`TextDeltaEvent / ToolCallStartEvent / ToolCallEndEvent(ok, result 预览) /
CompactionEvent / TurnEndEvent / ErrorEvent`。
事件对象本身不含任何传输格式；唯一的序列化点是服务层的
`runner.event_to_sse()`——把事件拍平成 SSE 帧（`event:` 名 + `data:` JSON）。

**事件是纯数据**：7 个都是 `@dataclass`，字段只有 str/int/Usage，**没有方法、没有依赖**。
`AgentEvent` 声明为 **union 而非基类**（`AgentEvent = TextDeltaEvent | ThinkingEvent | ...`），
所以消费者用 `isinstance`/`match` 穷举时，新增事件类型不会被静默漏处理——这是刻意的类型选择。

#### 6.2.1 解耦与投影：loop 只发信号，消费者各自投影

**一句话判据**：想加一个新消费者时，要不要动 `loop.py`？不用 → 解耦成立。

**① 「loop 无状态」= 不保有「跨 run 的、外部依赖的」状态**（不是不持有数据）

§6.1 的构造参数**全是协议或回调，没有一个具体后端**——它拿到 `LLMProvider` 却不知道是
哪家厂商/走不走网关/降级链多长，拿到 `runner: Any` 却不知道背后是本机执行还是
CubeSandbox microVM。**缺席清单**才是重点：loop 里搜不到 `pi.server.*` / `db` / `redis` /
`milvus` 的 import，没有 `open()` 写文件，没有 SQL。

它确实持有 `self.messages`（会话历史），但那是**每次 run 由外层构造时注入、run 结束即弃**的；
它不持有连接池、不持有单例、不缓存后端。这正是它能被单测直接构造（不起 DB、不起沙箱）的原因。

**② 只发信号：一个事件流 + 三个钩子**

`run()` 返回 `AsyncIterator[AgentEvent]`——**不 return 结果、不写副作用、不等待消费者**。
除事件流外只有三个同步回调钩子：`on_message` / `on_compact` / `on_checkpoint`。

**为什么 Checkpoint 走钩子而不是事件**：事件流是「给消费者的输出」（一次 run 一条流，
可被多个消费者投影），而 Checkpoint 是**状态快照**——它不是流的一部分，是生命周期通知，
用回调表达更贴切（`loop.py:363-364`）。

一个体现纪律的细节：`ToolCallStartEvent` 是**执行之前**发的（`loop.py:289`），
外层正是靠这个时机落库（见 ④）。**loop 只负责在对的时机发信号，不负责信号被用来干什么。**

**③ 装配 / 存储 / 记忆 / 技能，全在外层**

| 关注点 | 在哪 | 怎么进 loop |
|---|---|---|
| 装配 | `RunManager`（`server/runner.py:236`） | 构造 `AgentLoop(...)` 时注入 provider/tools/policy/audit/tracer |
| 存储 | 同上，事件循环里 | loop **一行写库代码都没有**；落库靠事件时机驱动（见 ④） |
| 记忆 | 两个**普通 Tool**（`recall`/`remember`） | 经 `ToolContext` 拿到 provider/policy/audit/tracer/session/user（`loop.py:173-178`） |
| 技能 | `use_skill` 工具 + 外层拼好的 system_prompt | 同上传通道 |

**一致性是这里的关键**：记忆、技能、RAG、子智能体**全部走同一条「工具」通道**，
loop 不为任何一类特殊能力开洞。所以加一个新能力（如 `rag_search` 声明
`knowledge.retrieve`）一个字都不用改 loop。

**④ 投影：同一份事件流，多个消费者同时在消费**

`server/runner.py:339-384` 就是一个 `async for` 里**五件事同时发生**：首个事件 → write-ahead
落库（时机投影）；`TurnEndEvent` → 取 usage/turns（状态投影）；`ErrorEvent` → 判定
`run_status`（归因投影）；`ToolCallStartEvent` → flush 缓冲（持久化投影）；`yield ev` →
透传到 SSE（前端投影）。

**加消费者的成本**：加一行 `isinstance` 分支，`loop.py` 的 534 行不动。这正是「审计、指标、
回放、评测都是从轨迹/事件**投影**出来的，加投影不动核心循环」的含义。

**⑤ ⚠️ 三个投影源要分清**（这里最容易讲错成一个源）

| 消费者 | 真实来源 |
|---|---|
| SSE 透传 / 落库时机 / in-flight gauge | **事件流**（实时投影） |
| 评测（`evals/` 整包）/ 回放（`/v1/.../trajectory`）/ `tool.call` 指标 | **轨迹**（jsonl + DB 双写） |
| **审计** | **独立链路**：loop 直接调注入的 `AuditLogger`（`loop.py:521` `_audit`），**不经过轨迹** |

`evals/__init__.py` 原文即「eval package is a **CONSUMER of the runtime's trajectory**」，
这条成立；但**审计不是轨迹的投影**，它必须独立，理由有三：

1. **审计要覆盖「被拒绝的」调用**——拒绝发生在 `policy.check()`，走的是另一条控制流，
   **压根不在 run 的轨迹里**，而它恰恰是最该审计的一类事件。
2. **审计是合规底稿，不能依赖业务日志的完整性**——轨迹可因磁盘满、或
   `PI_TRAJECTORY_PATH=""`（可关闭）而缺失；审计走独立双写，且明确「mirror 失败不许影响
   jsonl 副本」（`security/audit.py:30`）。
3. **脱敏发生在审计入口**（`_audit` 里 `redact_text`），是审计自身职责，不该等投影时再补。

口径不同会反映到数上：§17 第 24 条记录了实测——`pi_tool_calls_total{ok=False}` 报 12 个
「失败」，而审计日志里 0 个失败（那 12 个是 policy 拒绝，审计记的是 `allowed=False, ok=None`）。
查数要 join 审计的 `(allowed, ok)` 两字段，别直接下「工具坏了」的结论。

> 因此准确的总括是：**轨迹**是「运行时视图」的投影源（评测/回放/导出都吃它）；**事件流**是
> 「实时视图」的投影源（SSE/落库时机/in-flight）；**审计**是一条独立的合规链路。三者都因
> 「loop 只管发信号」而互不干扰——但这不等于「一个源投影出所有东西」。

### 6.3 `compaction.py` — 上下文压缩

| 函数 | 作用 |
|---|---|
| `estimate_size(messages)` | 粗略字符数（文本+工具参数+工具结果），作为阈值判据 |
| `render_messages(messages)` | 把消息拍成人类可读文本喂给摘要模型，单块截断 2000 字符 |
| `compact(provider, messages, keep_last)` | 取 `messages[:-keep_last]` 让 LLM 总结成纪要，返回 `[摘要标记消息] + 尾部原文`；摘要为空则原样返回（dropped=0） |

摘要提示词（`COMPACTION_PROMPT`）强制保留：目标与约束、已做决定及理由、
文件变更、命令结果、未完成事项。

**压缩结果已落库（P3 episodic 记忆）**：`RunManager` 接线了 `on_compact`
（runner.py:162-170、231），摘要写独立的 `compactions` 表——`save_compaction` 在
finally 里 fail-soft（runner.py:344-353），**原始消息表只增不减**（非破坏优先，见 §8）。
`covered_upto_idx` 标明摘要覆盖到第几条；下一轮开始时 `run_turn` 查
`latest_compaction`，只加载 `[摘要] + [idx 之后的新消息]` 作为历史
（runner.py:172-187）——**同一段历史只付一次摘要 LLM 费用**。摘要表是派生数据，
随时可重建；新消息的 `idx` 由 `count_for_session` 递增，与前缀压缩互不影响。

**为什么保留尾部 8 条原文？** 最近上下文是模型当前任务的工作记忆，摘要必有损，
混合方案（纪要+原文）在成本与连贯性之间取平衡。

---

## 7. 工具层 `src/pi/tools/`

### 7.1 `base.py` — 工具契约

```python
class Tool(ABC):
    name: str            # 模型看到的工具名
    description: str     # 给模型读的使用说明（写得越准，模型用得越对）
    input_schema: dict   # JSON Schema，模型按它生成参数
    capabilities: frozenset[str] = frozenset()   # 能力声明（§9.1 能力授权用；空 = 未声明）
    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult
```

- `ToolContext`：`cwd`（工作目录）、`max_output`（30,000 字符，防撑爆上下文）、
  `runner`（命令执行器，None=本机直跑，沙箱模式下注入，见 §7.3）。
- `resolve_path(ctx, raw)`：相对路径按 `ctx.cwd` 解析——**所有文件工具的寻址基准**。
- `SKIP_DIRS`：grep/find 自动跳过的目录（.git、node_modules、venv…）。
- `truncate()`：统一截断并附 `[truncated, N more chars]` 提示。
- `capabilities`：能力词汇表 `filesystem.read` / `filesystem.write` / `process.execute` /
  `memory.read` / `memory.write` / `agent.delegate`（§7.2 有各工具声明表）。12 个内置工具
  全部声明；MCP/skill 工具不声明（空集）——allow-list 策略下因此被 fail-closed 拒绝。

**为什么工具结果有 `is_error`？** 失败不是异常——把错误作为正常结果回喂给模型，
让它读到报错并自我纠正（改参数重试），这是编码智能体可用性的关键。真正的异常
（工具崩溃）由 `AgentLoop._run_tool` 兜住转成错误块。

### 7.2 内置工具一览

| 工具 | 参数 | 行为与关键限制 |
|---|---|---|
| `bash` | command, timeout(≤600, 默认120) | 走 `ctx.runner`（本机或沙箱）；输出合并、返回退出码；非零退出且输出以 `Error:` 开头 → `is_error` |
| `read` | path, offset, limit | 带 6 宽行号输出（模型做 edit 时按行号定位）；默认 2000 行、单行 2000 字符；NUL 字节判二进制拒读 |
| `write` | path, content | 自动建父目录、整体覆盖写 |
| `edit` | path, old_string, new_string, replace_all | **精确字符串替换+唯一性守卫**：匹配 0 次报"未找到"，多次且未开 replace_all 报"不唯一"；成功返回 unified diff（≤60 行）。这是防模型"凭印象改文件"的核心护栏 |
| `grep` | pattern, path, include | Python `re` 正则全树搜索，返回 `path:line: text`；≤200 条、单文件 ≤1MB；跳 SKIP_DIRS 和二进制 |
| `find` | pattern, path | fnmatch 相对路径 glob，≤500 条 |
| `ls` | path | 目录在前（带 `/`）、文件带大小，≤500 项 |

`all_tools()`（`__init__.py`）返回全部 12 个工具的实例列表（含 `list_files`/`fetch_file`），是唯一的工具注册点。
（`web_fetch`/`web_search` 2026-09 已整体移除：进程内抓取有 SSRF 风险，沙箱内 bash 抓取替代。）
**加工具就在这里注册**（扩展指南见 §16）。

**能力声明（capability-based 授权用，见 §9.1）**：

| 能力 | 声明它的工具 |
|---|---|
| `filesystem.read` | read、ls、grep、find、list_files、fetch_file、bash |
| `filesystem.write` | write、edit、fetch_file、bash |
| `process.execute` | bash |
| `memory.read` | recall |
| `memory.write` | remember |
| `agent.delegate` | spawn_subagents |
| `knowledge.retrieve` | rag_search（只读检索本用户已入库文档，无副作用） |

MCP/skill 工具不声明任何能力（空集）——allow-list 策略下 fail-closed 拒绝，所以配置了
能力白名单后外部工具默认不可用，需要明确放行。

### 7.3 `sandbox.py` — 命令执行隔离（本层最复杂的文件，~1300 行）

四种执行器，实现同一个 `CommandRunner` 协议（`run(command, cwd, timeout)` +
`prewarm(cwd)` 预热钩子）；**生产主线是 ④ CubeSandbox microVM**，Docker 为本地开发形态：

**① `LocalRunner`**：`asyncio.create_subprocess_shell` 直跑——未配 `PI_SANDBOX` 时的默认。
超时杀进程；输出统一 utf-8 解码（`errors="replace"`）。此时唯一的护栏是策略引擎的
路径沙箱与命令黑名单，多租户场景应当开 docker。

**② `DockerRunner`（冷路径）**：每次调用一个全新 `docker run --rm` 容器。
- 双传输：有 `docker` CLI 走 CLI；设了 `PI_DOCKER_HOST=tcp://...` 走 Engine REST API
  （`_run_via_api`，连远程 daemon 用）。
- 容器配置：挂载 `-v <workspace>:/ws`、工作目录 `/ws`、默认 `--network none` 断网。
- **fail-closed**：配了沙箱但 docker 不可用 → 命令直接报错，绝不退回裸跑。

**③ `DockerPool`（预热池，docker 模式默认）**：解决冷路径"每次调用都付一遍
create→start→销毁"的延迟。设计：

| 机制 | 实现 |
|---|---|
| 预热 | `prewarm(cwd)` 在 turn 开始时被调用（与 LLM 首响应并行），提前建好该 workspace 的温容器 |
| 分配 | `acquire()` 等容器就绪；并发请求同一容器只建一次（task 去重） |
| 执行 | 每次命令 = `docker exec`（CLI）或 `POST /exec`（API），无容器生命周期开销 |
| 即时回收 | 命令超时 → 容器判脏立即销毁重建；容器意外消失（daemon 重启等）→ 透明重建一次，两次都失败才报错 |
| 闲置回收 | 后台清扫任务按 `PI_SANDBOX_IDLE_TTL`（默认 600s）销毁闲置容器 |
| 扩容上限 | `PI_SANDBOX_POOL_MAX`（默认 16）：满了按 LRU 驱逐最闲的；全忙时允许临时超额（不阻塞用户） |
| 防孤儿 | 温容器启动命令是 `timeout <PI_SANDBOX_WARM_LIFETIME(2h)> sleep infinity`——应用崩了容器也会自毁；`shutdown()` 在 FastAPI lifespan 收尾时全量清理 |
| 创建限流 | `PI_SANDBOX_CREATE_CONCURRENCY`（默认 4）信号量，防止突发大量建容器打爆 daemon |

`get_runner(mode)` 是选择入口：`""/"local"` → LocalRunner；`"docker"` →
默认预热池（`PI_SANDBOX_POOL=0` 退回冷路径）。池是进程级单例，跨 turn 复用。

**语义变化须知**：预热池下同 workspace 的多次调用共享进程态（pip 装的包、env 变量
会保留），冷路径每次清零。文件不受影响（本来就在挂载卷里）。

### 7.3 `base.py` — 工具契约与 WorkspaceFS（2026-09 沙箱生产化后）

`ToolContext` 收敛为 `cwd / max_output / runner / fs` 四个字段；`provider/policy/audit/
tracer/session_id/user_id/memory/user_db_id` 由 loop/runner **运行时动态赋值**——
工具侧一律 `getattr(ctx, "policy", None)` 防御（裸 context 场景如 eval 没有这些属性，
直接访问会 AttributeError；这是合流修复的教训，见 ROADMAP 教训 9）。

文件工具通过 `WorkspaceFS` 协议读写（`LocalFS` / `SandboxFS` 双实现）：Docker 模式
LocalFS 直接操作宿主文件；CubeSandbox 模式 SandboxFS 走 VM 内路径。**数据通路与
policy 声明式路径沙箱构成两层越界防护**（详见 `docs/cube-sandbox-design-notes.md` §3）。

### 7.4 `registry.py` / `mcp.py` / `skill.py` — 工具来源聚合（MCP + Skills）

`ToolProvider`（`tools() -> list[Tool]`，可抛异常）+ `ToolRegistry` 聚合：

- `BuiltinToolProvider` = 现有 12 个内置；`McpToolProvider` = `PI_MCP_SERVERS` 配的
  外部 server（官方 mcp SDK，stdio spawn 或 HTTP）；`SkillToolProvider` = `PI_SKILLS_DIR`
  下的 `SKILL.md` 技能包。
- **fail-soft**：单个 provider 失败只记日志跳过，不炸启动；**按 name 去重**，先注册者
  优先（builtin 永远第一，同名冲突 MCP/Skill 让位）；工具列表**预热时取一次并缓存**
  （lifespan 里 `registry.warmup()`——`create_app` 是同步函数不能 await），MCP 连接跨
  turn 复用，shutdown 时 `registry.close()` 收掉子进程。
- Skills 两个 seam：`use_skill` 工具渐进加载正文 + runner 把紧凑索引（name+一句话描述）
  注入 system prompt（照抄记忆注入写法）；`scripts/*` 每个脚本一个
  `skill_<name>_<script>` 工具，**经 ctx.runner 沙箱执行**——技能目录在 workspace 挂载
  之外，执行前先把脚本 stage 到 `<workspace>/.pi-skills/<skill>/`。
- 安全:新来源工具自动穿过 deny_tools 黑名单/审计/追踪/配额;路径沙箱对它们**泛化**:
  任何工具 args 里的 `path`/`file`/`dir` 字符串值都做工作区越界检查(§9.1),不再只看
  工具名——MCP filesystem 类工具无法借未知工具名逃出 workspace。
- 坑(实测):mcp SDK 2.x 的 `Tool` 模型字段是 **snake_case**(`input_schema`),
  访问 camelCase 别名 `inputSchema` 会在无 anyio portal 的 asyncio 上下文里**死锁**;
  跨事件循环 close 同理(SDK 的 cancel scope 绑第一个 loop),连接与关闭必须在同一
  loop(测试里别用两个 `asyncio.run`)。
- 取舍:v1 无自动重连(server 死了=工具调用报错);子代理不拿 MCP/Skill 工具(递归仍
  builtin);HTTP transport 用 streamable HTTP,SSE 未单独验证。

### 7.5 `evals/` — 评估与 RL 数据飞轮（rollout / reward / filter / export）

`evals/`(P4)是 eval harness:声明式任务(`schema.py`:env.files 播种 + setup 命令 +
file/command/tests/judge 判分器)→ `runner.py` 跑 AgentLoop 抓 P1 轨迹 → `scorers.py`
判分 → 报告/AB diff。**eval 是轨迹的消费者,不是 loop 的功能**。

RL 数据飞轮(数据侧,训练交给 veRL/TRL,本项目停在 JSONL):

| 模块 | 作用 |
|---|---|
| `rollout.py` | 每任务 n_samples 条(GRPO 组内比较需要多样本)、并发受信号量;每条**独立临时 workspace**(组内互不污染,跑完删除);`RolloutSample` = 轨迹 + messages(从轨迹重建的 OpenAI chat 格式)+ reward + usage/latency |
| `reward.py` | verdict → reward:可验证判分 0/1,judge 分标 `source="judge"`;partial credit 时 tests 判分 = `passed_count/total_count`(Verdict 的**加性字段**,`score` 语义不变) |
| `filter.py` | 去重(规范化轨迹哈希——**必须剥掉 run_id/started_at/latency**,否则永不重复)→ 噪声剔除(无工具且 reward<1;工具全错且 reward<1)→ 每任务 rejection sampling:recovery 样本(reward=1 且中途有工具报错)无条件保留 + top_k + 每任务上限 |
| `export.py` | `sft.jsonl`(reward==1 高质量轨迹,带 system prompt)、`rlvr.jsonl`(**只含可验证样本**)、`rlvr_judge.jsonl`(judge 样本分文件,防 RLVR 奖励纯度被污染) |

**沙箱接线(加性缝)**:`run_task(task, *, runner=None)` 与 `score(task, result, *, runner=None)`
——默认 None = 宿主执行(开发工具原行为);rollout 传 `get_runner("docker")` 后,bash 工具、
env.setup、command/tests 判分器的 shell 全部进容器(判分跑 pytest 时容器镜像需自带 pytest,
file 判分器只读文件不受影响)。**执行环境在 eval 层可注入**——开发工具(零依赖)与数据管道
(生产沙箱)两个信任模型显式共存。

CLI:`pi-py eval rollout --tasks DIR --model X --n 8 --concurrency 16 --sandbox docker --out data/rl`。

---

## 8. 会话与消息持久化 `src/pi/server/db.py`

> 本节原来描述的是 `src/pi/session/store.py`：本地 SQLite（`~/.pi-py/sessions.db`）的
> `SessionStore`，只服务已删除的本地 CLI。**该文件已随 CLI/TUI 一起删除**，
> 持久化只剩下面这一条路径。引擎、方言与仓储写法的工程细节见 §11.6。

`SessionRepo`：

| 方法 | 作用 |
|---|---|
| `create(user_id, title, model, cwd)` | 建会话，id = `uuid4().hex[:12]`，title 截断 128 字符，`cwd` 存为该会话的 workspace 路径 |
| `for_user(user_id, session_id)` | 按 (id, user_id) 双条件取——**属主校验就在查询里**，别人的会话返回 None，上层转 404（不泄漏存在性） |
| `list_for_user(user_id, limit=50)` | 按创建时间倒序列出本人会话 |

`MessageRepo`：

| 方法 | 作用 |
|---|---|
| `append_many(session_id, entries)` | 批量追加，`entries` 为 `[{'idx', 'role', 'blocks'}, ...]`；`blocks` 是 `Message.model_dump_json()` 的字符串。一次事务写完——这就是原则 1（run 原子性）的落地点；用户消息 write-ahead 先行落库、每工具轮边界增量 flush（2026-10-02 起，见 §11.5） |
| `list_for_session(session_id, after_idx=-1)` | 按 `idx` 升序读回，`after_idx >= 0` 时只取 `idx > after_idx`（摘要复用加载用）；调用方用 `Message.model_validate_json` 反序列化 |
| `count_for_session(session_id)` | 现有消息数，`RunManager` 用它算新消息的起始 `idx` |
| `save_compaction(session_id, covered_upto_idx, summary, model)` | 摘要落 `compactions` 表（fail-soft，绝不挂 run） |
| `latest_compaction(session_id)` | 最近一份摘要；`RunManager` 加载历史时用它拼 `[摘要] + [后续新消息]`，同一段历史只付一次摘要费 |

消息表**只增不减**——没有"重写整个会话"的方法（原 `SessionStore.replace_messages`
的对应物刻意不复刻）。压缩落独立 `compactions` 表而非重写历史（非破坏优先），
完整机制见 §6.3。

`MemoryRepo`（语义记忆，P3b，跨会话长期记忆）：

| 方法 | 作用 |
|---|---|
| `add(user_id, text)` | 插 `memories` 行（源事实）；配置向量后端时顺手 embedding → Milvus upsert（失败只记日志、不抛——记忆绝不能挂 run） |
| `search(user_id, query, k)` | 配置向量后端时先向量检索（按 `user_id` 过滤）再按命中序回查 DB；任何失败/空结果**回退词法**（token 重叠打分，即原实现） |
| `list_for_user(user_id)` | 倒序全量 |

向量路径由四个环境变量整体开关（`PI_EMBEDDING_URL` / `PI_EMBEDDING_API_KEY` /
`PI_EMBEDDING_MODEL` + `PI_MILVUS_URI`，**四者全配才启用**，见 §13）：embedding 走
`pi/llm/embedding.py`（httpx，零新依赖），Milvus 走 `pi/server/vectorstore.py`（懒导入
pymilvus、懒建集合 `pi_memories`，主键=Postgres 主键，upsert 幂等）。`memories` 表是唯一
事实来源，Milvus 集合是可重建索引——删集合后从表全量重灌即可。

---

## 9. 安全层 `src/pi/security/`

### 9.1 `policy.py` — 策略引擎

所有工具执行前的闸门（`AgentLoop._run_tool` 里调用）。

| 组件 | 作用 |
|---|---|
| `Policy` | 六个开关：`deny_tools`（工具黑名单）、`deny_command_patterns`（bash 命令正则黑名单，大小写不敏感）、`path_sandbox`（路径沙箱）、`redact`（出站脱敏）、`allow_capabilities`（能力白名单，子集语义）、`deny_capabilities`（能力黑名单，交集语义） |
| `load_policy(path)` | 从 JSON 文件加载；路径为空 → 返回 None（没有策略对象 = 全放行） |
| `check(policy, tool_name, args, cwd, capabilities=frozenset())` | 返回 `PolicyDecision(allowed, reason)`。顺序：工具黑名单 → 能力黑名单 → 能力白名单 → bash 命令模式 → 路径沙箱（把 `read/write/edit/ls/grep/find` 的路径参数解析为绝对路径，必须落在 `cwd` 之内，防 `../../` 逃逸）。`capabilities` 由 `AgentLoop` 从 `Tool.capabilities` 传入 |

**能力授权语义（P0-1，2026-09-29）**：

- `deny_capabilities`：工具声明的能力与黑名单**有交集**即拒绝（在一切检查前，白名单之前）。
- `allow_capabilities`：非空时，工具必须**声明了能力**且声明的集合**全部落在**白名单内
  才放行——是子集而非交集，所以 bash（`process.execute` + 两个 filesystem 能力）不会因为
  `filesystem.read` 在白名单里就被放进一个纯读环境。
- **未声明能力 = fail-closed**：MCP/skill 工具的空能力集在 allow-list 策略下被拒绝
  （reason 里写明 "declares no capabilities"）。没有配置白名单时它们照常放行。
- 能力词汇表与各工具声明见 §7.2；`tests/test_security.py::TestCapabilities` 7 例钉住
  黑/白名单、子集语义、fail-closed 与 from_dict 解析。

服务端策略（`runner.server_policy`）：没给 `PI_POLICY` 文件时 = `path_sandbox + redact`；
**给了文件也一样强制这两位**——`Policy.from_dict` 把它们默认成 `False`，否则一份只列
拒绝规则的文件会顺手关掉工作区沙箱和脱敏（§17.17）。也就是说 `PI_POLICY` 只能加规则。

仓库根 `policy.json` 是**当前生效的生产策略**（不是示例）：裸跑由 `.env` 的 `PI_POLICY`
直接指向它，两个 compose 把同一个文件挂到 `/etc/pi-py/policy.json`。它拦得住什么、
拦不住什么，以及钉住它的回归测试，见 §17.18。

### 9.2 `redact.py` — 出站脱敏

发给模型**之前**把敏感信息替换成 `[REDACTED:...]`。覆盖：常见云/LLM API key、
key=value 形式的密钥、中国身份证号、手机号、内网 IPv4 段。
`redact_messages()` 返回脱敏副本，**本地存档保留原文**——只遮出站方向。

### 9.3 `audit.py` — 审计日志

`AuditLogger`：append-only JSONL，按天滚动（`audit-2026-09-03.jsonl`）。
线程锁保护写；每条记录含时间戳、会话、用户、工具、参数、决定、结果预览（≤400 字符）。
`tool_call()`、`compaction()`、`auth()` 三个记录类型。管理员通过 `GET /v1/admin/audit` 查询。

`auth()` 记注册和登录的**每一次尝试**（`action` / `ok` / `reason`），带客户端 IP 和
User-Agent，**不记密码**。三点注意：

- **所有字段都截断**（username ≤64、ip ≤45、ua ≤200、reason ≤32）。登录失败路径是攻击者
  控制的，而 `LoginIn.username` 没有长度限制——不截断的话，一个请求就能把审计盘写满。
  JSONL 本身会转义换行，所以伪造不出第二条记录（`tests/test_security.py` 钉了这条）。
- **`ip` 只有在 `PI_FORWARDED_ALLOW_IPS` 覆盖到反向代理时才是真客户端**，否则记的是
  Caddy 容器 IP。详见 §17 第 16 条。
- 这个文件从此**含个人数据**（IP + UA）。该给它定保留期限，不是无限期留着。

---

## 10. 可观测层 `src/pi/observability/`

沙箱健康指标（2026-09 起，§10.4）：`pi_sandbox_create_failures_total` /
`pi_sandbox_command_timeouts_total` / `pi_sandbox_close_failures_total` /
`pi_sandbox_create_duration_seconds` 四系列，runner 装配时注入 `runner.metrics`
（鸭子类型，standalone 无 metrics 时全 no-op，零耦合）。

### 10.1 `tracing.py` — 三种 tracer 后端

统一接口：`tracer.track(name, attrs)` 上下文管理器产出 span，异常自动记
`set_status(False)`，结束记耗时。

- `NoOpTracer`：默认，零开销；
- `JsonlTracer`：零依赖，每个 span 一行 JSON 写 `~/.pi-py/traces-日期.jsonl`；
- `OtelTracer`：桥接 OpenTelemetry SDK（没装就自动回落 jsonl）。

埋点位置：`agent.run` → 内含 `llm.call`（每轮）和 `tool.call`（每次）。
`get_tracer(backend)` 按 `PI_TRACER` 选择。

### 10.2 `metering.py` — 计量与配额

`UsageTracker`（用服务的 SQLAlchemy 引擎，写 `usage_records` 表）：

| 方法 | 作用 |
|---|---|
| `record(...)` | 每次 run 结束记一行（模型、输入/输出 token、估算成本、轮数） |
| `monthly_summary(username)` | 当月按模型分组汇总（token、成本、次数） |
| `quota_check(user_id, quota)` | 当月已用（输入+输出）< 配额？返回 `QuotaCheck` |

配额语义：`quota_tokens <= 0` 时用全局默认值（`PI_DEFAULT_QUOTA_TOKENS`）。
改某用户配额 = `PATCH /v1/admin/users/{u}` 或直接改库。

### 10.3 `prices.py` — 成本估算

内置各模型每百万 token 的输入/输出美元单价表，`estimate_cost()` 估算单次成本；
支持用 `~/.pi-py/prices.json` 覆盖/补充单价。仅估算（不同供应商计费有差异）。

### 10.4 `metrics.py` — Prometheus 指标（旁观者架构:单一记录点投影)

`prometheus-client`(可选依赖,observability extra),pull 模型暴露于 `GET /metrics`
(不在 OpenAPI schema 里;`PI_METRICS_TOKEN` 门控,**错 token 回 404 不是 403**——403
等于告诉扫描器端点存在;`PI_METRICS=0` → 503 带原因)。

**每族指标一个既有 choke point,不新增埋点**:

| 指标族 | 来源 | 为什么 |
|---|---|---|
| llm.call(counter/histogram,model/ok) | `Tracer.track()` finally 钩子(tracing.py,基类唯一出口) | 唯一覆盖流中断失败的记录点(LlmCall 轨迹事件只在成功后记录) |
| tool.call(tool/ok,含 denied/unknown/无效参数) | run 结束时从 trajectory 投影(runner.py) | tool.call span 只包"解析+放行"路径,trajectory 覆盖全部尝试 |
| run 级(runs/status、duration、turns、tokens、in_flight) | RunManager:in_flight 用 async context manager 包运行段(gauge 异常路径回落);status 判定同时看 ErrorEvent 消息里的 TimeoutError 与 trajectory RunError | TurnEndEvent 自带 usage/turns |
| memory 检索(outcome/duration/词法回退) | MemoryRepo 的 `on_retrieval` 回调缝(仿 on_embed_usage);outcome:vector_hit/lexical_fallback/no_hits/embed_failed | 调用方拿不到 vector/lexical 出处 |
| 模型降级 | FallbackProvider 既有 `on_fallback` → `pi_llm_fallbacks_total(from,to)` | "静默降级必须出声" |
| HTTP(method/route/status/TTFB) | 既有请求中间件;route 用**路由模板**(有界),未匹配统一 "unmatched" | SSE 在响应后流式——HTTP duration 是 TTFB 语义,不是 run 时长 |

**标签纪律**:仅 model/tool/status/outcome/method/route;username/session/prompt 进标签
= 基数随用户数增长 = 自造事故。降级计数器(词法回退、模型降级)是本设计的核心价值:
让静默故障在仪表盘上出声。采集:deploy/prometheus.yml(内网直连,多副本=每副本一个 target)。

---

## 11. 多用户服务 `src/pi/server/`

### 11.1 `config.py` — ServerSettings.from_env

所有服务端配置的集中读取点。关键项：`database_url`（PI_DATABASE_URL，**必填**——
为空直接抛 `RuntimeError`，服务拒绝启动，没有本地文件兜底）、`jwt_secret`
（PI_JWT_SECRET，缺省自动生成并持久化到
`~/.pi-py/jwt.secret`——**生产必须显式设置**）、`max_concurrent_runs`(8)、
`run_timeout_seconds`(600)、`rate_limit_runs_per_min`(20)、
`default_quota_tokens`(100万)、`redis_url`、`sandbox*`、`audit_path`、`policy_path`、
`forwarded_allow_ips`（PI_FORWARDED_ALLOW_IPS，默认 `127.0.0.1`——**反向代理部署必须改**，
理由见 §17 第 16 条）。

### 11.2 `auth.py` — 密码与令牌

- `hash_password / verify_password`：PBKDF2-HMAC-SHA256，20 万次迭代、随机 16 字节盐，
  存为 `pbkdf2$次数$盐$摘要` 自描述格式；验证用 `secrets.compare_digest` 防时序攻击。
  这两个是**同步阻塞**函数（约 50ms），异步化的责任在调用方：`app.py` 用
  `asyncio.to_thread` 包起来，新增路由别直接调（缘由与实测见 §17 第 15 条）。
- `create_token / decode_token`：JWT HS256，载荷含 `sub/iat/exp/jti`。
  **jti 是注销和踢人的钩子**（见 11.4）。

### 11.3 `app.py` — FastAPI 应用（路由全表）

| 端点 | 鉴权 | 作用 |
|---|---|---|
| `GET /healthz` | 无 | 存活探针（Docker HEALTHCHECK 用） |
| `GET /readyz` | 无 | 就绪探针：检查 DB/缓存 + **沙箱后端硬检查**（cubesandbox=控制面 `/health` + 数据面 `*.PI_CUBE_DOMAIN` DNS 解析，缺一不可；docker=连 docker.sock，不起子进程；local=无外部依赖直接 ok）。沙箱不可用翻 503（Milvus 仍只记 `degraded` 不翻 503，见 §13 `PI_MILVUS_URI` 行） |
| `POST /v1/auth/register` | 无（刻意免鉴权） | 注册（`PI_ALLOW_REGISTER=0` 时 403），一律普通用户；重名 409 |
| `POST /v1/auth/login` | 无 | 验密 → 发 JWT |
| `POST /v1/auth/logout` | 用户 | 把当前 token 的 jti 拉黑至过期 |
| `GET /v1/me` | 用户 | 当前用户名 |
| `GET/POST /v1/sessions` | 用户 | 会话列表 / 创建（workspace = `workspace_root/用户名/`） |
| `GET /v1/sessions/{id}` · `/messages` | 用户 | 会话详情 / 消息历史（仅限本人，`_owned_session` 强制属主校验） |
| `POST /v1/sessions/{id}/runs` | 用户 | **核心**：提交 prompt，返回 SSE 流（限流→配额→RunManager） |
| `GET /v1/admin/users` | 管理员 | 用户列表 |
| `PATCH /v1/admin/users/{u}` | 管理员 | 改配额 / 启停账号（禁用时顺带踢掉其所有在线 token；不能禁用自己） |
| `POST /v1/admin/users/{u}/revoke` | 管理员 | 只踢令牌不禁账号 |
| `GET /v1/admin/audit` | 管理员 | 查当日审计（可按 user/tool 过滤） |
| `GET /v1/usage` | 用户 | 本人当月用量 + 配额余量 |

**依赖链**：`current_user`（解码 JWT → jti 黑名单 → 用户撤销纪元 → 查库确认激活）；
`require_admin` 在其上再加管理员校验。所有需要登录的路由都挂这两个依赖。

**权限模型：注册完全开放，管理员只能改库授予。** 注册接口没有任何门禁——无注册码、
无审批队列、无匿名限流，注册者一律是普通用户（`is_admin=False`）。提权唯一路径是运维
直连数据库：`UPDATE users SET is_admin=1 WHERE username='alice';`。
`UserRepo.set_admin` 存在，但**没有任何路由调用它**，只供测试和内部脚本用。

这套设计的前提是网络边界：8300 只在私网暴露、公网不可达。前提一旦破了，代价要认——

- 注册是全服务唯一免鉴权又能触发 PBKDF2（20 万次迭代，实测约 50ms/次）的入口，
  单核约 20 次注册/秒，几十个并发就能吃掉多核 CPU；
- 账号可被无限刷，每个账号都自带 `PI_DEFAULT_QUOTA_TOKENS` 的月配额，
  并在首次建会话时 `mkdir` 一个 workspace 目录。

并发重名的正确性：`by_username` 查重与插入不是原子的，两个同名请求可能都通过检查。
兜底是 `username` 上的 UNIQUE 约束——`IntegrityError` 被翻译成 409（旧实现不接这个
异常，同名并发会返回 500）。前置查重仍然保留，作用只是让常见的重名请求不必白付一次
50ms 的哈希。

### 11.4 撤销机制（两级）

- **注销（登出）**：`revoked:{jti}` 键，TTL = token 剩余寿命；
- **管理员踢人**：`epoch:{username}` 键 = 踢人时刻；凡是 `iat <= epoch` 的令牌一律拒绝。
  保守窗口：禁用账号时 epoch 的 TTL 设为令牌最大寿命 + 60 秒。

### 11.5 `runner.py` — RunManager

见 §3.2 数据流。补充要点：

- **锁拒绝的语义**：会话锁被占时不是 HTTP 错误码，而是正常 200 的 SSE 流里发一条
  `event: error`（"another turn is already running"）——因为连接已经建立为流，
  客户端只处理一种错误通道。
- `event_to_sse(ev)`：AgentEvent → SSE 帧（`event:` 名 + `data:` JSON），
  text_delta / toolcall_start / toolcall_end / compaction / turn_end / error。
- run 超时（`PI_RUN_TIMEOUT_SECONDS`）用 `asyncio.timeout` 包住整个循环，超了发
  ErrorEvent 但**已产生的消息仍会持久化**（buffer 非空就写）——这是刻意的，
  部分结果比全丢有用。
- **write-ahead + 逐轮落库（2026-10-02 起）**：非 resume run 的第一条消息必是用户
  prompt（loop 在任何流式事件前 append 它），`on_message` 单独摘出、在**首个 SSE 事件
  发出前**落库（失败 = 发 ErrorEvent 终止 run，数据安全优先）；run 未走到首事件
  就结束时由 finally 兜底补落。此后每个 `ToolCallStartEvent` 前再 flush buffer——
  模型请求执行工具前，客户端已见过的全部消息先落库。硬崩溃丢失窗口 ≤ 当前轮，
  用户回来看到截断历史、说"继续"即可接着跑（主流产品形态，无需显式 resume 端点）。
  完整方案与剩余可选增强见 `docs/run-durability-design.md`。

### 11.6 `db.py` — ORM 与仓储

四张表：`users` / `sessions` / `messages` / `usage_records`（SQLAlchemy 2.0 Mapped 风格）。

- `engine_kwargs(url)`：MySQL 方言追加 `charset=utf8mb4`（防实例默认 latin1 乱码）
  + `pool_recycle=280`（低于常见 wait_timeout，池内连接永不失效）。
  应用启动和 alembic 共用此函数，保证两边引擎配置一致。
- `Database`：异步引擎 + `pool_pre_ping=True`（生产保留：防 RDS HA 切换后的死连接）。
- 三个仓储（`UserRepo/SessionRepo/MessageRepo`）：每个方法独立 `AsyncSession`，
  写操作各自提交。这种"每方法一会话"的写法简单但有隐藏开销（每次隐式 BEGIN/
  ROLLBACK 一个往返）——公网部署时是延迟大头之一；同 VPC 部署后 <1ms，不值得优化。
  若将来要做请求级会话共享，从这三个仓储入手。
- `UserRepo` 有 `set_active / set_quota / set_admin` 三个改属性的方法，但只有前两个
  挂着管理员路由；`set_admin` **刻意不暴露**（见 §11.3 的权限模型）。

### 11.7 `cache.py` — 缓存/锁后端

协议 `CacheBackend`：`get/setex/delete/incr_window/acquire_lock/release_lock/ping`。
- `MemoryBackend`：进程内字典（无 Redis 时的退化实现，单实例可用）；
- `RedisBackend`：键带命名空间前缀（`{ns}:kv:` / `{ns}:lock:`），`PI_REDIS_NS` 隔离环境。
  锁 = `SET key 1 NX EX`，释放 = `DEL`。
- `get_backend(url, namespace)`：空 URL → 内存；`redis://...` → Redis。

### 11.8 `ratelimit.py` — 限流

`RateLimiter`：每用户固定窗口（`incr_window` 计数，超 `PI_RATE_LIMIT_RUNS_PER_MIN`
拒绝 429）。固定窗口实现简单，缺点是窗口边界处可能短时 2 倍突发——对内部工具够用；
要滑动窗口就换 `cache.incr_window` 的实现。

---

## 12. 入口：`cli.py` 与 `.env` 加载

### 12.1 `cli.py` — 子命令（只剩两个）

| 子命令 | 作用 |
|---|---|
| `serve` | 启动 FastAPI 服务（`--host`（默认 127.0.0.1）/`--port`（默认 8300）/`--db`）；内部就是 `ServerSettings.from_env()` → `create_app(settings)` → `uvicorn.run` |
| `migrate` | 跑 `alembic upgrade head`（支持 `--db`；容器内靠 `PI_ALEMBIC_DIR` 定位 `alembic.ini`，否则按仓库布局往上找两级） |

`--db` 的实现方式值得注意：它不是把 URL 传给下游，而是**写回
`os.environ["PI_DATABASE_URL"]`**，这样 `ServerSettings.from_env()` 和 alembic 子进程
（`env=os.environ.copy()`）看到的是同一个值，不存在两条配置通路。

无参数运行 `pi-py` 打印帮助；`--version` 打印版本。

> 原先这里还有 `chat` / `tui` / `run` / `sessions` 四个本地子命令，以及把它们和
> `pi/env.py`（`AgentEnv`）串起来的装配层——"解析模型 + 打开会话 + 决定 cwd +
> 读策略 + 挂审计与 tracer + 注入沙箱 runner"。**这些都已删除**。
> 服务端从来不走 `AgentEnv`：同样的装配工作由 `RunManager.run_turn` 在每个 turn
> 里就地完成（见 §3.2 数据流），装配点因此从两处收敛成一处。

### 12.2 自动加载 `.env`（`pi/__init__.py`）

包导入时按顺序找 `.env` / `~/.pi-py/.env`，读 `KEY=VALUE` 行注入
环境变量（**已存在的环境变量优先**，即显式设置永远赢）。找到第一个存在的文件就停。
值两侧的引号会被剥掉；空行与 `#` 开头的行忽略。

> **变更留痕（2026-10-01）**：候选曾有三个（`.pi-py.env` / `.env` / `~/.pi-py/.env`）。
> 项目根的 `.pi-py.env` 与 `.env` 是同一槽位的两个名字，且本仓库两个 compose 文件
> 都没有 `${}` 变量替换（不存在"compose 抢读 .env"的冲突），防冲突理由不成立，
> 因此移除 `.pi-py.env`，收敛为两个：**项目内 `.env`（默认）+ 机器级 `~/.pi-py/.env`
> （跨 checkout 共用密钥，与 jwt.secret/audit 同目录）**。本地若还在用
> `.pi-py.env`，把文件改名为 `.env` 即可，无需其他迁移。

**这条机制有个容易踩的后果**：仓库根放着生产 `.env` 时，**任何 import pi 的进程
都会继承它**——包括 `pytest`。测试自己会 monkeypatch `PI_DATABASE_URL`，但历史上
没有任何测试设置 `PI_REDIS_URL`，于是测试会把锁 / 限流 / 撤销键写进生产 Redis
（只是命名空间被随机化成 `test-xxxx`，不与 `prod:*` 冲突，但确实在污染生产实例），
`PI_TRACER=jsonl` 也会让测试往 `~/.pi-py/` 写 trace 文件。

因此 `tests/conftest.py` 在 **import pi 之前**钉死变量（"已存在的环境变量优先"让
`.env` 里的对应项失效）：

```python
# DB/Redis 是"推导"不是"钉死"（2026-10-02 起）：从 .env.local 的
# PI_DATABASE_URL / PI_REDIS_URL 换出 pi_py_test 库 / Redis db1——端口单一来源、
# 测试自动跟随（PI_TEST_DB_URL / PI_TEST_REDIS_URL 可显式覆盖）。曾自带一份默认
# 端口，与 .env.local 上移后的端口错位：清表连到别的库且不报错，用例随机失败。
os.environ["PI_DATABASE_URL"] = TEST_DB_URL     # 见上，推导结果
os.environ["PI_REDIS_URL"] = TEST_REDIS_URL     # 同上
os.environ["PI_SANDBOX"]   = ""   # → LocalRunner，绝不起真容器
os.environ["PI_POLICY"]    = ""
os.environ["PI_TRACER"]    = "noop"
# 其余 PI_EMBEDDING_* / PI_MILVUS_URI / PI_MCP_SERVERS / PI_SKILLS_DIR 全空
```

因为"已存在的环境变量优先"，这几行赋值就让 `.env` 里的对应项失效。
**新增会触达外部服务的配置项时，记得同步加进这个列表。**

### 12.3 测试库：`.env.test` 与 `tools/seed_testdb.py`

生产库 `pi_py` 之外，同一实例上还有 `pi_py_test`（charset/collation 与生产一致：
utf8mb4 / utf8mb4_unicode_ci），用于手工 e2e 和压测，里面常驻一批可复用的测试数据。

切换全靠环境变量，不需要任何代码改动——因为"已存在的变量优先"：

```bash
set -a; . ./.env; . ./.env.test; set +a   # 后 source 的覆盖先 source 的
pi-py migrate                             # 只对 pi_py_test 建表/升级
python tools/seed_testdb.py               # 幂等灌数据；--reset 先清空再灌
pi-py serve --port 8398                   # 别占用生产的 8300
```

`.env.test` 只覆盖四项，其余（JWT 密钥、Redis 地址、模型）沿用 `.env`：
`PI_DATABASE_URL`（换成 `pi_py_test`）、`PI_REDIS_NS=test`（键前缀隔离）、
`PI_WORKSPACE_ROOT` / `PI_AUDIT_PATH`（各带 `-test` 后缀，不与生产混）。
它**不会**被 `pi/__init__.py` 自动加载（加载器只认 `.env` /
`~/.pi-py/.env`），必须显式 source——这正是想要的：忘了 source 就还在生产配置上，
而种子脚本的护栏会因此直接拒绝运行。文件含密码，权限 600，已进 `.gitignore`。

种子数据（密码默认 `pi-test-123`，`--password` 可改）：

| 账号 | 状态 | 用来验什么 |
|---|---|---|
| `admin` | `is_admin=1` | 管理员端点；提权走 `UserRepo.set_admin`，就是"改库"那条路 |
| `alice` / `bob` / `carol` | 普通用户，各 2 个会话（一个 4 条消息、一个空） | 正常链路、跨用户 404、utf8mb4（消息里带中文和 4 字节 emoji） |
| `overquota` | 配额 1000、当月已用 1540 | `POST /runs` 立刻 402 |
| `disabled` | `is_active=0` | 登录 401 |

护栏：脚本开头检查 `PI_DATABASE_URL` 的库名，不以 `_test` 结尾就拒绝退出，
免得对着生产库灌出一堆账号。另外用量记录写的是**当天**日期，而 `/v1/usage` 和
配额检查只统计当月——跨月之后要重灌一次才有配额数据。

**pytest 用 `pi_py_test` 库**（2026-09 起测试与生产统一 MySQL/Redis）：conftest
每测试清表 + 播种 fixture 行（u1..u20 用户、s1..s9 会话），保证每个测试从干净库开始、
结果可复现；手工验证/压测用 `pi_py` 库。这条边界别混——同一张表若被 pytest 与手工
操作同时读写，测试之间就会互相看见数据。

---

## 13. 配置速查表

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PI_MODEL` | `openai/gpt-4o` | 默认模型（`provider/model` 格式） |
| `PI_FALLBACK_CHAIN` | 空 | 逗号分隔降级链 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | — | OpenAI 兼容端点凭据；cloud compose 默认把 base_url 指向内置聚合网关 `llm-gateway`（显式设置则直连厂商，见 §14.2） |
| `ANTHROPIC_API_KEY` | — | Anthropic 凭据 |
| `PI_DATABASE_URL` | **无（必填）** | `mysql+aiomysql://...` 或 `postgresql+asyncpg://...`；缺失则服务拒绝启动 |
| `PI_REDIS_URL` / `PI_REDIS_NS` | 空 / `pi` | Redis（锁/限流/撤销）；空=进程内退化 |
| `PI_JWT_SECRET` | 自动生成 | **生产必须显式设置**；改了=所有已发 token 作废 |
| `PI_TOKEN_TTL_MIN` | 720 | JWT 寿命（分钟） |
| `PI_MAX_CONCURRENT_RUNS` | 8 | 全局并发 run 上限（信号量） |
| `PI_RUN_TIMEOUT_SECONDS` | 600 | 单次 run 超时 |
| `PI_RATE_LIMIT_RUNS_PER_MIN` | 20 | 每用户每分钟 run 上限 |
| `PI_DEFAULT_QUOTA_TOKENS` | 1,000,000 | 新用户默认月配额 |
| `PI_TRACER` | jsonl | noop / jsonl / otel |
| `PI_POLICY` | 空 | 策略 JSON 文件路径；**只能加规则**，`path_sandbox`/`redact` 被 `server_policy` 强制打开 |
| `PI_SANDBOX` | 空 | 空/`local`=本机执行（启动时打 `warning`）；`docker`=容器隔离；`cubesandbox`=CubeSandbox microVM（生产方案）；**其它值启动即报错**，不再静默降级 |
| `PI_SANDBOX_IMAGE` | python:3.12-slim | 沙箱镜像 |
| `PI_SANDBOX_NET` | 关 | `host` 才允许容器联网 |
| `PI_SANDBOX_MEMORY` | 1g | 单容器内存上限；同时设等值 `--memory-swap` 关掉 swap（docker 默认允许 2 倍） |
| `PI_SANDBOX_PIDS` | 256 | 单容器进程数上限（挡 fork bomb）；0=不限 |
| `PI_SANDBOX_CPUS` | 1.0 | 单容器 CPU 上限；空=不限 |
| `PI_SANDBOX_USER` | 空 | 容器内 `uid:gid`；空=**跟随应用自身**（裸跑 root → `0:0`，compose uid 10001 → `10001:10001`），无需为换部署方式而改 |
| `PI_SANDBOX_POOL` | 1 | docker 模式下 0=退回冷路径（每调用一容器） |
| `PI_SANDBOX_POOL_MAX` | 16 | 温容器上限（软上限，全忙可超额） |
| `PI_SANDBOX_IDLE_TTL` | 600 | 闲置回收秒数 |
| `PI_SANDBOX_WARM_LIFETIME` | 7200 | 单容器最长寿命（防孤儿） |
| `PI_DOCKER_HOST` | 空 | `tcp://host:2375` 走 REST API 而非 CLI |
| `PI_WORKSPACE_ROOT` | `~/.pi-py/workspaces` | 服务端各用户工作区根目录 |
| `PI_AUDIT_PATH` | `~/.pi-py/audit.jsonl` | 审计文件基址（实际按天滚动） |
| `PI_ALEMBIC_DIR` | 仓库根 | 容器内迁移脚本位置（Dockerfile 已设） |
| `PI_EMBEDDING_URL` / `PI_EMBEDDING_API_KEY` / `PI_EMBEDDING_MODEL` | 空 | 云 embedding 端点/凭据/模型名；与 `PI_MILVUS_URI` **四者全配**才启用向量语义记忆，缺一即纯词法检索 |
| `PI_MILVUS_URI` | 空 | Milvus 连接串（如 `http://127.0.0.1:19531`）；懒连、挂了 readyz 只记 `degraded` 不翻 503（词法兜底，检索质量降级而非正确性） |
| `PI_MCP_SERVERS` | 空 | JSON 数组：`[{"name","command":[...]}]`（stdio）或 `[{"name","url"}]`（HTTP）；空=不启用。**管理员级配置**——stdio server 作为应用子进程运行、继承应用环境 |
| `PI_SKILLS_DIR` | 空 | 技能根目录（`<skill>/SKILL.md` + `scripts/`）；空=不加载技能 |
| `PI_METRICS` | 1 | Prometheus 指标开关；0=关（`/metrics` 回 503） |
| `PI_METRICS_TOKEN` | 空 | `/metrics` 门控 token；空=开放（启动打 warning），错 token 回 404 |
| `PI_CUBE_API_URL` | `http://127.0.0.1:3000` | CubeSandbox 控制面（E2B 兼容 API）；`PI_SANDBOX=cubesandbox` 时使用 |
| `PI_CUBE_API_KEY` | 空 | CubeSandbox（E2B 兼容 API）密钥；`PI_SANDBOX=cubesandbox` 必配 |
| `PI_CUBE_DOMAIN` | `cube.app` | 沙箱数据面域名后缀（`{port}-{id}.cube.app`，解析到 `10.0.0.8`）；`/readyz` 硬检查探它——DNS 解析失败 = 503（见 §11.3） |
| `PI_SANDBOX_TEMPLATE` | 空 | CubeSandbox 模板 id；`PI_SANDBOX=cubesandbox` 必配（如 `cube-lite-py`） |
| `PI_SANDBOX_CA_FILE` | `~/.pi-py/cube-ca-bundle.pem` | 平台自签 CA + 系统 CA 的合并包（设为 `SSL_CERT_FILE`）；单放平台 CA 会覆盖系统信任链 |
| `PI_SANDBOX_CLOSE_TIMEOUT_SECONDS` | 90 | 沙箱 close/save 总超时；超时 turn 先走、清理线程收尾（VM 必死） |
| `PI_SANDBOX_POOL_SIZE` | 4 | 常驻 VM 池上限，超出 LRU 淘汰 |
| `PI_SANDBOX_POOL_TTL` / `PI_SANDBOX_POOL_TTL_TIGHT` | 900 / 300 | 空闲 VM 回收阈值（内存宽裕 / 紧张两档） |
| `PI_SANDBOX_POOL_PRESSURE_HIGH` / `_LOW` | 1.5GiB / 512MiB | 宿主可用内存双阈值，低于则收紧/激进回收 |
| `PI_ALLOW_REGISTER` | 1 | 开放注册开关；0 = 注册接口 403、前端隐藏注册 tab（公网演示用） |
| `PI_RAG_ENABLED` / `PI_RAG_MEMORY_VECTOR` | 1 / 0 | RAG 知识库总开关（关=rag_search 工具整个消失）/ 进程内向量兜底（多 worker 下默认关） |
| `PI_RAG_MAX_UPLOAD_BYTES` | 200 MiB | `/v1/rag/ingest` 单文档上限，超出 413——每 MiB 都变成 OCR/embedding 配额消耗，上限收窄单次误传的爆炸半径 |
| `PI_RAG_MAX_CONCURRENT_INGESTS` | 8 | ingest 后台任务并发上限（I/O 密集，保护外部 OCR/embedding API 不被突发打满）；超出 429，稍后重试 |
| 其余 `PI_RAG_*`（内核） | — | chunking/embedding/rerank/heavy-parser/Milvus 参数由 `RagConfig.from_env()` 读，完整表见 §21 |
| `PI_ARCHIVE` | 1 | 会话归档开关（0=关）；`PI_ARCHIVE_DIR`（默认 ~/.pi-py/archives）、`PI_ARCHIVE_S3_*`（MinIO 惰性上传） |
| `PI_S3_ENDPOINT` / `PI_S3_ACCESS_KEY` / `PI_S3_SECRET_KEY` / `PI_S3_BUCKET_FILES` / `PI_S3_BUCKET_ARTIFACTS` | 空=关 | MinIO/S3 文件管线（预签名直连、sha256 用户级去重、files 表索引） |

---

## 14. 部署

### 14.1 数据库迁移

- 开发：`pi-py migrate`（对 `PI_DATABASE_URL` 执行）。
- 生产：compose 里 `migrate` 是一次性服务，先于 `app` 运行，成功才放行
  （`service_completed_successfully`）。**每次发版都会自动跑**，幂等。
- 迁移文件在 `migrations/versions/`；schema 的"真相源"是 `server/db.py` 的 ORM
  定义，新增字段要同时写迁移。

### 14.2 Docker 部署

- `Dockerfile`：多阶段（构建层编译依赖不污染运行层）；运行时带
  `asyncpg/aiomysql/redis/uvicorn/alembic`；非特权用户（uid 10001）；
  内置 HEALTHCHECK 打 `/healthz`；迁移脚本打进 `/opt/pi-py`。
- `docker-compose.local.yml`：**本地测试环境基础设施**（MySQL 8 + Redis 7 + Milvus standalone），
  app 跑在宿主机 python；环境变量模板 `deploy/env.local.example`，见 `deploy/local-dev.md`。
- `docker-compose.cloud.yml`：云变体——不含数据库容器，指向火山引擎托管
  MySQL/Redis 的**内网**域名；密钥全部来自 `.env`（`deploy/env.cloud.example` 模板）；
  内置 `llm-gateway`（new-api）聚合网关服务：`OPENAI_BASE_URL` 留空即默认走网关
  （compose 内 `http://llm-gateway:3000/v1`），管理 UI 只绑 `127.0.0.1:3000`、
  经 SSH 隧道访问；可选 `--profile tls` 加 Caddy（`deploy/Caddyfile.cloud`，`{$DOMAIN}` 注入，
  `flush_interval -1` + `encode off` 保证 SSE 不被缓冲）。
- **完整云端 runbook**（安全组核对、上传、验证清单、调优、故障排查）：
  `deploy/cloud-deploy.md`。

### 14.3 多实例与横向扩展

锁、限流、撤销都在 Redis → 多实例语义正确（已双实例验证）。
**但** `workspaces`/`audits` 是本机 named volume：跨主机扩容前必须先解决工作区
共享存储（NFS/云盘）或会话粘连路由，否则副本间看不到彼此的文件。单机多副本
（`PI_REPLICAS>1`）共享同一 volume，无此问题。

---

## 15. 测试

```bash
pip install -e ".[dev]"
python -m pytest -q     # 测试统一连本地 MySQL（pi_py_test 库）+ Redis（db1）；外部服务用测试替身
```

**2026-09-27 起测试与生产同构**（MySQL + Redis + Milvus，不再有 SQLite 测试库和 demo 环境）：
- 单元测试：DB 走本地 MySQL `pi_py_test`（每测试清表隔离），缓存走本地 Redis（随机 namespace）。
  LLM/embedding/Milvus 用测试替身（FakeProvider/FakeEmbedder/FakeVectorStore）——这是测试分层，
  不是 demo；真实链路由 `integration/` 验证。
- 真实栈集成测试：`PI_INTEGRATION=1 pytest integration/`（真实 MySQL/Redis/Milvus/云 embedding，
  配置变量 `PI_ITEST_*`，见 `integration/conftest.py`）。
- 基础设施没起时单测会失败，先 `docker compose -f deploy/docker-compose.local.yml up -d`。

测试组织（都在 `tests/`，共 267 例，2026-10-02 按 `--collect-only` 实测）：

| 文件 | 例数 | 覆盖 |
|---|---|---|
| `test_security.py` | 47 | 策略拒绝、路径逃逸、脱敏、审计（含认证记录的截断与防伪造行）、JWT；`server_policy` 只加不减（策略文件无法关掉 `path_sandbox`/`redact`）；能力授权 7 例（deny 交集、allow 子集语义、未声明能力 fail-closed、`from_dict` 解析、12 内置工具全声明能力）；对**仓库根那份生效的** `policy.json` 做回归：22 条危险命令必须拦、15 条日常命令必须放行（见 §17.18） |
| `test_sandbox_pool.py` | 41 | 预热池（假传输，无需真 docker）：复用/预热/并发去重/回收/重建/驱逐/关闭；容器资源限额（`_parse_size`、`SandboxLimits` 校验与两种渲染、CLI/Engine API 两条建容器路径都真的带上了限额）；`PI_SANDBOX` 非法值必须报错而不是静默降级 |
| `test_server.py` | 30 | 全 HTTP API：开放注册（含并发重名）、登录、会话、run SSE、跨用户隔离、限流（`TestClient` 进程内驱动 + 临时 SQLite）；另有认证事件审计（每个出口都落一条、不落密码）与 `X-Forwarded-For` 取真实 IP（可信 CIDR / 默认只信本机 / 伪造前缀 / `*` 反例，见 §17.16）；持久化 4 例（write-ahead：中途崩溃/无输出崩溃后用户消息仍在、失败后 idx 连续；逐轮落库：完成轮消息在下一轮崩溃后仍在） |
| `test_trajectory_view.py` | 16 | 轨迹持久化 + 查看端点：会话级/run 级查询、DB 优先 jsonl 兜底、属主校验（跨用户 404 不泄漏存在性） |
| `test_rollout.py` | 15 | RL 数据飞轮：rollout、reward 抽取、过滤、导出 JSONL |
| `test_memory_vector.py` | 15 | 向量语义记忆：Milvus/embedding 路径 + 失败/未配置时优雅词法兜底 |
| `test_observability.py` | 14 | 计量、配额、价格、tracer、降级链 |
| `test_metrics.py` | 14 | 指标：span 钩子、run/工具事件投影、gauge、gate、标签纪律（缺 metrics 类时 skip） |
| `test_deployment.py` | 12 | 缓存后端、本地沙箱执行器、迁移可达性；其中 1 例（真 Redis 限流）在 localhost:6379 无服务时 **skip** |
| `test_registry.py` | 8 | ToolRegistry：聚合、去重、fail-soft、缓存、技能索引 |
| `test_launch.py` | 7 | 注销/撤销、管理员端点、审计按天滚动 |
| `test_admin_console.py` | 7 | 管理台端点：/v1/admin/stats、usage、审计日期参数 |
| `test_trajectory.py` | 6 | canonical 轨迹（P1）：6 类事件、ts 墙钟语义 |
| `test_mcp.py` | 5 | MCP 工具源：对假 stdio server（tests/fake_mcp_server.py） |
| `test_evals.py` | 5 | eval harness（P4）：runner、判分器、报告、加载器 |
| `test_durable.py` | 4 | durable execution（P2）：checkpoint 往返 + resume 幂等重放 + 正常 run 同参数重复不误去重 |
| `test_skill.py` | 4 | Skills：加载、索引、use_skill、脚本工具、提示注入 |
| `test_subagent.py` | 4 | 递归子代理委派（spawn_subagents） |
| `test_client.py` | 3 | SDK：pi.client 用 ASGITransport 打真实 app（不开 socket） |
| `test_mysql_compat.py` | 3 | `engine_kwargs` 的方言分支 + 布尔默认值在 MySQL/PG/SQLite 三方言下的 DDL 兼容 |
| `test_smoke.py` | 2 | 端到端：fake 模型驱动完整 agent 循环（write→read→edit→grep 四次工具调用）+ `on_message` 回调 |
| `test_memory.py` | 2 | 语义记忆（P3）：跨会话长期记忆 |
| `test_episodic.py` | 2 | episodic 记忆（P3）：压缩摘要落库复用 |
| `test_compaction.py` | 1 | 压缩：摘要替换旧历史、保留尾部 |

> 原有 `test_cli_polish.py`（7 例，覆盖 chat REPL 的斜杠命令、自动标题、`--json`
> 事件流）随本地 CLI 一起删除；`test_deployment.py` 里的 GBK 解码例随 Windows 支持删除。
> 101 → 93 的差额（8 例）全部来自这两处，没有覆盖率损失。（93 是**那次删除之后**的
> 数量，不是当前总数；之后陆续补了认证审计、`X-Forwarded-For`、沙箱资源限额与
> 策略回归，现在见上表 267 例。）

**测试约定**：

- 一律用 `FakeProvider` + 临时目录/一次性 SQLite 文件，绝不依赖真实模型或外部服务；
- 异步测试用 `asyncio.run(main())` 包裹（未引入 pytest-asyncio 依赖）；
- `conftest.py` 除了把 `src/` 加进 `sys.path`，还在 import pi **之前**钉死
  `PI_SANDBOX` / `PI_POLICY` / `PI_TRACER` 等，防止仓库根的生产 `.env`
  被自动加载后把测试引到真 docker / 写盘（原理见 §12.2）；DB/Redis 连接则从
  `.env.local` **推导**（换 `pi_py_test` 库 / Redis db1，端口单一来源）；
- `aiosqlite` 只是**测试依赖**（`[dev]` extra），生产路径不含 SQLite。

---

## 16. 常见扩展怎么做

| 需求 | 做法 |
|---|---|
| 加一个新工具 | `tools/` 下建类（`name/description/input_schema/execute`），在 `tools/__init__.py` 的 `all_tools()` 注册；若带路径参数，记得让 `policy._extract_path` 认识它（路径沙箱才管得住） |
| 接新的模型厂商 | `llm/` 下实现 `LLMProvider`（把厂商流翻译成三种 StreamEvent），在 `registry.resolve` 加分支 |
| 加服务端新路由 | 在 `app.py` 的 `create_app` 内定义，挂 `current_user`/`require_admin` 依赖；需要新表就加 ORM + 迁移 |
| 改系统提示词 | `prompt.py` 的 `SYSTEM_PROMPT`（由 `runner.py` 传给 `AgentLoop`） |
| 调整压缩策略 | `compaction.py` 的提示词；阈值是 `AgentLoop` 的构造参数 `compact_threshold` / `compact_keep`（默认 80,000 字符 / 保留 8 条）。**未暴露成环境变量**，`RunManager` 也没传，要改就在 `runner.py` 构造 `AgentLoop` 处加参数 |

## 17. 注意事项与坑（前人踩过的）

1. **`PI_JWT_SECRET` 是多实例的命根**：不设的话每个实例各自生成，跨实例登录互认不了；
   重启后换了=全员登出。生产一定显式注入且不要轮换（除非有意踢人）。
2. **注册默认开放，可一键关闭（`PI_ALLOW_REGISTER=0`）**：注册接口 403、前端藏注册 tab；
   管理员仍然只能改库授予（`UPDATE users SET is_admin=1 WHERE username='...'`），
   没有自助提权口子。公网演示先关注册（代价见 §11.3）。想清库重来：按外键顺序清
   `usage_records → messages → sessions → users`（TRUNCATE 会因外键失败）。
3. **公网部署延迟大头是网络往返不是代码**：一次 run 约 40 次串行往返
   （SQL+pre-ping+隐式事务+Redis）。同 VPC 部署即根治；不要为此去关
   `pool_pre_ping`——它防 RDS HA 切换后的死连接，韧性价值 > 开销。
4. **httpx 客户端注意代理**：本机环境若有 `HTTP_PROXY`，httpx 默认 `trust_env=True`
   会把绝对 URL 编码进代理路径导致 404；本地压测/脚本里显式 `trust_env=False`。
5. **MySQL 方言细节**：布尔列 `server_default` 只能是 `"1"` 不能是 `"true"`；
   非参数化 SQL 里的 `%` 不要写 `%%`（那是给参数化语句的）；连接必须带
   `charset=utf8mb4`（`engine_kwargs` 已处理，别绕过它自建引擎）。
6. **SSE 锁拒绝是 200 流内错误**：并发抢同一会话，输家收到的是
   `event: error`（HTTP 仍 200）——客户端别只盯状态码。
7. **沙箱启用前提**：仓库 `.env` 已按裸跑（`pi-py serve`）打开 `PI_SANDBOX=docker`。
   两个 compose **仍然没开**——app 容器里要能访问宿主 Docker（挂 socket = 等同宿主机
   root 权限，或配 `PI_DOCKER_HOST` TCP+TLS），开启前读 `deploy/cloud-deploy.md`。
8. **预热池改变进程态语义**：同 workspace 连续调用共享容器内状态（装的包还在）；
   需要干净环境就用 `PI_SANDBOX_POOL=0`。
9. **计量失败不回滚对话**：这是刻意设计（原则 4）——记账问题不应让用户的工作丢失。
10. **不要删 `migrations/` 或 `.dockerignore` 里放行它**：Dockerfile 要把它打进镜像，
    排除掉构建直接失败。
11. **压缩摘要落 `compactions` 表、原始消息只增不减**：长会话的历史加载是
    `[最新摘要] + [摘要之后的新消息]`，同一段历史只付一次摘要 LLM 费；摘要表是
    派生数据，删了下一轮会重新生成（机制见 §6.3）。曾有一版"压缩不落库、每轮重付
    摘要费"的实现现状，P3 episodic 记忆落地后已消除。
12. **`pi/server/__init__.py` 必须保持惰性导出**：它 eager import `create_app` 时，
    任何先碰 `pi.server.db` / `pi.observability.metering` 的脚本都会撞上环：
    `metering → server.db → server/__init__ → server.app → metering`（半成品）→ ImportError。
    现在用 PEP 562 的模块 `__getattr__` 按需解析，`from pi.server import create_app`
    照常工作。**别"顺手整理"成顶层 import**，也别指望靠调脚本里的 import 顺序绕开
    ——isort/formatter 会把它悄悄改回去。
13. **`str(make_url(...))` 会把密码打成 `***`**（SQLAlchemy 2.0 行为）：拿它去连库就是
    `1045 Access denied`，而且报错里看不出密码被替换过。要真实串用
    `url.render_as_string(hide_password=False)`，或者干脆对环境变量原值做字符串手术
    （`raw[:-len("/pi_py")] + "/pi_py_test"`）。仓库里现在没有 `str(url)` 的用法，别引入。
14. **`quota_tokens=0` 不是"禁止使用"而是"用默认配额"**：`quota_check` 是
    `quota = quota_tokens if quota_tokens > 0 else default_quota`，判据又是 `used < quota`。
    想封一个手工改库建出来的号，改 `is_active=0`（登录/请求会 401），别去把配额写成 0 或 1
    ——0 会被当成默认值，1 在用量为 0 时依然放行。
15. **`hash_password` / `verify_password` 必须走 `asyncio.to_thread`**：PBKDF2-HMAC-SHA256
    20 万次迭代是约 50ms 的纯 CPU 阻塞，而 `app.py` 里**所有**路由都是 `async def`
    （包括 `/healthz`、`/readyz`），FastAPI 把它们直接跑在事件循环上，没有线程池兜底。
    同步调用 = 整个服务冻住，连健康检查和所有 SSE 流一起停。实测（4 核，真实服务，
    独立进程探 `/healthz`，20 并发登录）：

    | | `/healthz` 最长间隔 | 主机 CPU | 20 次登录墙钟 | 健康探测失败 |
    |---|---|---|---|---|
    | 同步（旧） | **453.2ms** | 52.7% | 1388.7ms | 0 / 19917 |
    | `to_thread`（现） | **40.0ms** | 99.7% | 867.8ms | 0 / 20465 |

    旧版 CPU 只有 52.7% 是关键证据：**核闲着，活在排队**——不是算力不够，是串行。
    `pbkdf2_hmac` 在 C 层释放 GIL，所以线程池能真跨核并行，把闲着的核变成吞吐。
    代价是单次登录多约 10ms 派发开销（n=1：48.7ms → 58.2ms），换掉一次 453ms 全站黑屏，
    值。**别把它"简化"回同步调用**——尤其别以为 `PI_MAX_CONCURRENT_RUNS` 能挡住：
    那个信号量只管 `/runs`，登录和注册根本不在它后面，任何并发数配置都保护不了这条路径。
16. **反向代理部署必须设 `PI_FORWARDED_ALLOW_IPS`，而且设错了不报错**：uvicorn 的默认值是
    `127.0.0.1`（`config.py` 读 `FORWARDED_ALLOW_IPS`，没设就取这个），而
    `ProxyHeadersMiddleware` **只在连接方 IP 落在信任列表里时**才解析 `X-Forwarded-For`。
    Caddy 在 compose 里是独立容器，源 IP 是 `172.x`，不匹配 → 头部被静默丢弃 →
    `request.client.host` 对**所有**请求都等于 Caddy 自己的容器 IP。两个后果：

    - 审计日志的 `ip` 字段全是同一个值。被刷了也不知道是谁刷的，事后没法封、没法追责。
    - 任何按 IP 的限流会把全世界塞进同一个桶。限 10 次/小时的话，第 11 个人开始谁都注册不了。

    排查入口是 `cli.py` 启动时打印的那行 `trusted proxies for X-Forwarded-For: ...`
    （带 `flush=True`，否则重定向到日志文件时会被缓冲住，永远看不到）。
    两个 compose 已设 `172.16.0.0/12`（覆盖 Docker 默认网段）。

    **绝对不能写 `*`**：那会打开 `always_trust`，`get_trusted_client_host` 直接返回
    XFF 的**最左值**——也就是客户端自己填的那个，限流和审计同时形同虚设。
    指定网段反而是安全的：Caddy 是**追加**而非覆盖 XFF，uvicorn 从右往左找第一个不可信跳，
    所以客户端伪造的前缀会被忽略。这四条行为都由
    `tests/test_server.py::TestForwardedFor` 钉住（含 `*` 被伪造成功的那条反例）。
17. **两个"配错就等于没配"的静默降级，现在都改成启动即失败**：

    - `PI_SANDBOX` 的匹配是**字面量** `== "docker"`，其它任何值（包括看起来很像的
      `docker-pool`、`pooled`）都落到 `LocalRunner`。而 `LocalRunner.run` 是
      `asyncio.create_subprocess_shell(command, cwd=...)`，**不传 `env=`** → 继承应用的
      整个环境：`PI_JWT_SECRET`、带 RDS 密码的 `PI_DATABASE_URL`、`PI_REDIS_URL` 全都能被
      任何注册用户 `echo $PI_JWT_SECRET` 读走。一个拼写错误 = 沙箱静默关闭。
      现在 `create_app` 第一行就调 `validate_sandbox_mode(settings.sandbox)`，
      不认识的值直接 `ValueError` 拒绝启动；走 `LocalRunner` 的合法路径（空 / `local`）
      也会打一条 `warning` 说明后果。
    - `Policy.from_dict` 把 `path_sandbox` / `redact` 默认成 **False**。所以一个只列了
      `deny_command_patterns` 的策略文件，加载后会**顺手关掉**工作区沙箱和密钥脱敏
      ——正好和"加一份策略"的意图相反。现在 `server_policy()` 用
      `dataclasses.replace` 把这两个位强制打开：`PI_POLICY` 只能加规则，不能减隔离。

    这两条和 §17.16 是同一族问题：**安全开关的默认/错误状态必须是"吵"的**。
    判据很简单——如果一个配置项写错会导致隔离消失，它就不允许静默降级。
18. **仓库根 `policy.json` 拦得住误操作，拦不住恶意**：命令模式是给"模型手滑"用的
    安全网，**不是**防攻击的边界。`env`、`printenv`、`set`、`/proc/self/environ`
    和任意解释器一行代码读到的都是同一个环境——15 条正则一条也挡不住偷密钥。
    真正的边界只有两个：把 bash 关进容器（§17.19），以及不要把密钥放进应用环境。

    这份文件是**发布物**，所以 `tests/test_security.py::TestShippedPolicy` 对它做回归：
    22 条危险命令必须被拦（含 `rm -rf --no-preserve-root /` 这种长选项变体、
    `chown -R bob /`、命令位置的 `reboot`），15 条日常命令必须放行
    （`rm -rf ./build`、`grep -rn 'shutdown' src/`、`echo reboot later`、
    `dd if=a of=b`）。**误报比漏报更难查**：它会静默打断正常的 agent 工作，
    所以两个方向都要钉住。改正则前先往这两个列表里加用例。

    **而且只能有一份**：裸跑的 `.env` 用绝对路径 `PI_POLICY` 指向仓库根的
    `policy.json`，两个 compose 把**同一个文件**挂到 `/etc/pi-py/policy.json`
    （compose 里 `PI_POLICY` 是写死的字面量，不从 `.env` 插值）。别在 `deploy/`
    之类的位置再放一份"更严格的"——那会变成两种部署方式执行不同规则，
    而两边日志都显示策略已加载，出问题时无从对起。
19. **docker 的资源默认值是"无上限"，而注册是开放的**：不显式设的话
    `Memory=0`、`NanoCpus=0`、无 `PidsLimit`，容器内 cgroup 是 `memory.max=max`、
    `ulimit -u` unlimited。也就是说任何开出来的号都能用一条命令吃光宿主机
    ——`--network none` 和按用户 bind mount 都不覆盖这一类。现在
    `SandboxLimits` 在**四处**建容器路径上都带上限额（冷启动 CLI / 冷启动 Engine API /
    温池 CLI / 温池 API），漏一处就等于没限。

    两个容易踩的细节：CLI 吃后缀字符串（`--memory 1g`），Engine API 只吃字节
    （`HostConfig.Memory`）和 `NanoCpus`（1e9 = 1 核），所以有 `_parse_size` 做换算；
    `--user` 要放在**顶层 Config**（不是 HostConfig），且 `docker exec` 会继承容器的
    user，因此创建时设一次就够，`exec` 不用改。不显式给 group 会得到 `gid=0`，
    所以是 `uid:gid` 形式。

    调容量时算的是乘法：`PI_SANDBOX_MEMORY × PI_MAX_CONCURRENT_RUNS` 必须远小于物理内存
    （当前 1g × 8 = 8 GiB / 16 GiB 机器）。温池还有 `PI_SANDBOX_POOL_MAX` 个常驻容器
    在额外占位。
20. **进程内抓取工具已整体移除（2026-09，SSRF 关闭方案）**：`web_fetch`/`web_search`
    曾在应用进程内用 `httpx` 直连、无地址校验、`follow_redirects=True`（火山引擎元数据
    服务 `100.96.0.96` 实测可达）——沙箱断网对它们毫无约束。修复决策是**移除而非修补**：
    进程内抓取连"被模型调用"的可能都没有；沙箱内 bash 抓取完全替代。回归测试断言
    工具集里不再存在这两个工具。

21. **冷路径超时会漏一个容器（已知未修）**：`DockerRunner._run_via_cli` 是前台
    `docker run --rm`，超时后只做 `proc.kill()`——那杀的是**本地的 docker CLI 客户端**。
    SIGKILL 无法转发给容器，而 `--rm` 只在容器**自己退出**时生效，所以容器会继续跑到
    它的命令自然结束为止，期间照常吃 CPU 和内存。三条路径的行为不一致，改的时候别只看一条：

    | 路径 | 超时后 | 结果 |
    |---|---|---|
    | 冷 + CLI（`PI_SANDBOX_POOL=0`） | `proc.kill()` 杀本地客户端 | **容器泄漏** |
    | 冷 + Engine API | `POST /containers/{id}/kill` | 干净 |
    | 温池（默认） | 抛 `ExecTimeoutError` → `_drop(entry)` → `docker rm -f` | 干净 |

    以上是**读代码得出的**，没有真跑一次超时去复现（在生产机上故意留泄漏容器不合适）。
    默认配置走温池，所以现在不受影响；要修就修冷 CLI 那条（改成 `-d` + 轮询，
    或者超时后补一次 `docker rm -f`）。
22. **`.env` 会被 compose 插值，`PI_SANDBOX=docker` 会跟着进容器**：仓库 `.env` 现在为
    裸跑打开了 `PI_SANDBOX=docker`，而两个 compose 写的是 `PI_SANDBOX: ${PI_SANDBOX:-}`
    ——**会把这个值继承进去**。但 app 容器里没有 docker 客户端也没有 socket，
    于是 `get_runner("docker")` 抛 `RuntimeError`，每一次 bash 调用都失败（fail-closed，
    不会退回本机执行，但会让人一头雾水）。切 compose 之前要么在部署用的 `.env` 里
    把它清空，要么给容器一条到 daemon 的通路（`PI_DOCKER_HOST`，或挂 socket——
    那等于宿主机 root）。

    顺带：`PI_POLICY` 在 compose 里是**写死的字面量** `/etc/pi-py/policy.json`，
    不从 `.env` 插值，所以裸跑那条绝对路径不会漏进容器。两个 compose 挂载的
    `./policy.json` 就是仓库根那一份（§17.18）。

23. **压测的 prompt 必须 trivial，真实模型会把歧义 prompt 当成编程任务**：第一次真实
    模型压测（prompt="load test round"）平均 214s、p95 600s——模型把它理解成"写压测
    脚本"，反复用 `/ws/*`、`/tmp/*` 容器绝对路径调 write，被路径沙箱拒了 25 次还在
    换变体重试，最后烧满 run 超时。三条教训一起生效才修好：**(a)** 压测/自动化脚本的
    prompt 要写成 "Reply with exactly: OK" 这类无歧义任务（测服务不测模型行为）；
    **(b)** 拒绝消息必须可操作——只说 "escapes the sandbox" 模型会盲试,加上
    workspace 根 + "use a relative path" 它才有机会恢复；**(c)** 连续拒绝熔断是必要
    的止损:AgentLoop 连续 5 次 policy 拒绝即中止（`MAX_CONSECUTIVE_DENIALS`）。修后
    复验:8 并发 avg 3.8s。注意:只有**真实注册**的工具调用才进入拒绝计数——未知工具
    在 policy 之前就报错,不触发熔断（测试里 tools=[] 会静默绕过,踩过）。

24. **指标里的"失败"要先定性再看数**:压测时 `pi_tool_calls_total{ok=False}` 出现
    12 个 write"失败",审计日志里却 0 个失败——那 12 个是 **policy 拒绝**(审计里
    `allowed=False, ok=None`,没有 result_preview)。metrics 的 ok=False 覆盖三类:
    真崩溃、工具自报错、政策拒绝。查数时要 join 审计的 `(allowed, ok)` 两个字段,别
    直接下"工具坏了"的结论。这也是"每族指标一个记录点"设计的必然结果:tool 指标来自
    trajectory(含 denied),而审计是脱敏投影——两边口径不同。

25. **代理环境变量有三个门,每个门修法不同**:同一台机器上死掉的本地代理
    (`HTTP_PROXY=http://127.0.0.1:7890` + socks5)从三个地方漏进过进程——
    **(a)** httpx 直连(embedding client)→ `trust_env=False`;
    **(b)** gRPC(pymilvus)→ `grpc_options=[("grpc.enable_http_proxy", 0)]`;
    **(c)** OpenAI/Anthropic SDK → 自建 `http_client=httpx.AsyncClient(trust_env=False)`
    传进 SDK。漏了 (c) 的现场:每次模型调用都 `ImportError: Using SOCKS proxy, but
    the 'socksio' package is not installed`,SSE 流全断,服务端日志只有 CancelledError。
    诊断线索:客户端 ReadError + 服务端只有 CancelledError = **客户端先断**。另注意本
    环境 anthropic SDK 是 **httpx2** 分支——http_client 要用 SDK 实际 import 的那个
    httpx 构建,否则 `Invalid http_client argument`。服务端和压测客户端**两侧**都要
    剥代理 env(`env -u http_proxy -u all_proxy ...`)。

26. **压测客户端的超时要盖住"排队 + 执行"**:20 并发压 8 槽信号量,队列里的请求
    等待+执行很容易超过默认 120s 客户端超时 → ReadError → 压测脚本崩溃(老版本没有
    单请求容错)。`tools/loadtest.py` 已加 `--timeout` 参数(真实模型给 300+)和单请求
    错误容忍(status 0 计数不崩)。真实模型吞吐压测的预算要按
    "队列深度 / 并发槽 × 单 run 时长"算,不是按单 run 时长算。

27. **prometheus_client 0.26 的注册名剥 `_total`**:`Counter("pi_llm_calls_total")`
    在 registry 里注册名是 `pi_llm_calls`(导出时才加回 `_total`),测试里按全名匹配
    `metric.name` 会匹配不到——`m.name == name or m.name + "_total" == name`。Histogram
    的样本名还有 `_bucket/_count/_sum/_created` 后缀,断言时别一起捞。

28. **`asyncio.timeout` 落在 loop 内 try 时,超时以 ErrorEvent 出现**:loop 自己
    catch `TimeoutError`(Exception 子类)→ 记 RunError → yield ErrorEvent("TimeoutError:
    ...")→ 照常 TurnEndEvent;runner 的 `except TimeoutError` 只在超时落在 compaction
    阶段才触发。所以 run 状态判定不能只看 runner 的 except——要看 ErrorEvent 消息里
    有没有 "TimeoutError"(`run_status` 启发式),并且这两种路径**都**要计 status。

---

## 18. 与 AI agent 协作的工程纪律（2026-09-27 会话总结）

§17 记的是代码/环境坑；本节记**协作过程**的纪律——同样是踩出来的，但踩的不是代码。

1. **等待纪律：有界、异步、用对工具**。"等待"分三种，工具不同：
   - **有界条件等待**（服务就绪、Milvus 健康）→ 后台一次性 `until` 循环，**上限放循环内部**
     （如 30 次 × 2s，超限即失败退出），回合不阻塞；
   - **长任务**（压测、批量迁移）→ 后台跑 + **宽裕安全网**（如 3600s），完成等通知，
     等待期间做独立工作；安全网是兜底不是计划——本会话给压测套了按预估的 800s
     外层 timeout，任务没跑完就被掐死，对账只能另起小规模重跑；
   - **周期性观测**（压测中采样 in-flight）→ 用 Monitor 的定时事件流（2-4s 间隔 +
     到期上限），而不是前台 for-sleep 轮询（前台 sleep 还可能被 harness 禁用）。
   共同原则：**天花板必须有，但放在等待逻辑内部；等待放后台，完成即醒**。

2. **进程匹配自杀陷阱（本会话踩两次）**：`pgrep -f` / `pkill -f` 的模式只要出现在
   自己命令行里就会匹配到自己——第一次是命令行字面量，第二次是 `python -c` 脚本
   **内容**里的路径。修法：匹配字符串用拼接绕开自匹配（`'fake_mcp_ser''ver'`），或把
   清理拆成独立命令。杀服务用日志里的 PID，别用 pgrep。

3. **不移植别人的环境规则**：看到别的 agent 环境的提示词/经验（如 dsh 的
   "pwd 确认目录"、"Windows 裸退出码 1=被杀"）时，先问一句：**这条解决的是不是我
   这个环境的真实问题？** dsh 需要 pwd 规则是因为它的 checkout 与工作目录设计上
   分离，而本会话的 cwd 漂移只是险情（harness 有变更通知）；Windows 退出码 quirk 在
   Linux 上无对应物。**经验要本地化**：代码坑进 §17，环境坑进项目记忆，通用原则
   不进系统提示词——提示词解决"怎么做"，领域坑只能靠"踩过 + 记录"。

4. **设计文档的"断言"要对照代码核实再动手**：评审 RL 飞轮设计时，"复用
   runner/scorers 全不动"与"Docker 沙箱=rollout 环境"实际矛盾（run_task 根本没接
   沙箱）；"轨迹哈希去重"因 `run_id`/`started_at` 随机字段必然失效；partial credit
   所需数据在 Verdict 里不存在。三条都在实现前被发现，变成了方案的一部分。
   批判式审查的价值 = 让"文档声称"和"代码事实"在动手前对齐。

5. **"失败"要先定性再归因**：指标的 ok=False 覆盖三类（工具崩溃 / 工具自报错 /
   policy 拒绝），审计里拒绝是 `allowed=False, ok=None`——先 join 审计再下结论
   （详见 §17.24）。分布式故障的另一条快速定性：客户端 ReadError + 服务端只有
   CancelledError = **客户端先断**，先查客户端超时再查服务端（§17.25）。

6. **测试必须证明"测到了目标行为"**：熔断器测试初版 tools=[] 导致调用走
   unknown-tool 分支（policy 之前就报错），断言全部静默绕过而测试通过——测试通过
   ≠测到了。凡是依赖"被测路径被走到"的测试，先构造能进入该路径的前置条件，并
   断言路径本身的证据（如 trajectory 的 denied 标记），而不是只看结果。

7. **压测的闭环定义是"暴露→定性→修复→复验"**：跑通只是起点。本会话的完整
   闭环：metrics 让静默行为可见（in-flight 峰值、write 失败）→ audit 定性（25 次
   路径沙箱拒绝）→ 三层修复（trivial prompt + 可操作拒绝消息 + 连续拒绝熔断）→
   复验数据（avg 214s → 3.8s）证明效果。缺任何一环，压测就是白跑。

---

## 19. 与 main 分支的差距清单（未完成项，2026-09-27 对比基线 main@6381fd1）

dev 与 origin/main 是两条**无关历史**的并行线（见项目记忆）。本节钉住当前差距，
防止遗忘；两边都在动，处理前先 `git fetch origin main` 刷新基线。

**已覆盖**（main 有、dev 已补）：Prometheus 指标系统（且超越：llm 实时钩子、降级
计数器、全路径工具投影）、integration 真实栈测试、L15 日志脱敏（修了，main 只记录）、
embedding 用量计量、conftest pin 纪律、**run/轨迹落库**（jsonl 按天滚动 + 查询端点 +
时序图前端；2026-09-28 迁移 0005 再上 runs 表——轨迹双写 jsonl+DB，含管理员 runId
回放端点）、**Web 前端**（零构建三页
app/trajectory/admin，已决策不搬 Vue 工程）。

**未覆盖**（按建议处理顺序）：

| # | 缺口 | main 的形态 | 说明 / 建议 |
|---|---|---|---|
| 1 | 轨迹结构化落库 | `agent_runs` + `trace_fidelity` 表 | dev 侧已闭环：runs 表（迁移 0005，轨迹 jsonl+DB 双写，2026-09-28）+ 会话/run 级/admin 查询端点。与 main 双表形态的最终对齐待 fetch main 核对 |
| 3 | 迁移合流 | 生产库在 `0007_trace_fidelity` | 两边 0003/0004 **同名不同内容**（session_plan/user_memories vs compactions/memories）。合流必须设计整合迁移，**前提是定生产库未来形态** |
| 4 | 语义检索的兜底档 | MySQL 暴力余弦兜底 | 索引挂了我们只有词法兜底（可用，语义质量降档更狠）。可选增强 |
| 5 | 运维资产 | `deploy/pi-py.service`（生产实际走 systemd）+ L1~L15 事故记录 | 搬运即可；dev 文档目前仍以 compose 为主 |

**反向对账**：main 也没有 dev 的一半——MCP/Skills 工具源、RL 数据飞轮、泛化路径
沙箱、Milvus 向量记忆、连续拒绝熔断、§18 协作纪律。**谁也不是谁的超集**。

---

## 20. 产品形态与战略定位（2026-09-27 讨论结论）

**一句话定位**：可自托管、可扩展工具、可评估、可沉淀训练数据的 agent 平台后端。
竞争对手不是"带 UI 的 coding agent"，而是"没有评估闭环、没有飞轮、没有沙箱隔离"
的那批开源 agent 服务。

### 20.1 三层能力模型

| 层 | 内容 | 现状 |
|---|---|---|
| **服务层**（对外） | 多租户 agent API、工具（MCP/Skills）、记忆、沙箱、配额/限流/审计 | 完整，且深于 main |
| **数据层**（对内，战略资产） | 轨迹、评估、RL 飞轮（rollout→reward→export JSONL） | 飞轮已建，**轨迹双写落库**（jsonl 合规底稿 + runs 表结构化镜像，迁移 0005）——"存储"一环已补（本节为 2026-09-27 讨论结论，当时尚未上 DB 表） |
| **运营层**（对管理员） | metrics、审计查询、用量/配额管理 | metrics 有，审计 audit_events 表可查（jsonl 兜底），轨迹 runs 表 + admin runId 回放端点 |

数据层是本项目最独特的定位：服务系统 + 评估系统 + RL 数据生产系统三位一体。
飞轮边界是产品判断：**数据侧停手，训练交给 veRL/TRL，别回头把训练拉进来**。

### 20.2 形态决策（CLI / TUI / WebUI / SDK）

- **CLI ✅（运维型，保持）**：`serve / migrate / eval rollout` 是作业入口（起服务、
  迁移、评估、压测、飞轮 rollout）。**不扩展成交互式聊天 CLI**——历史已删过单用户
  CLI/TUI 模式，那与多租户服务定位冲突。
- **TUI ❌**：不做。历史删过；典型用户是单机开发者，定位冲突；"看指标/日志"属于
  监控生态（htop/Grafana/终端仪表盘），自造 TUI 是负资产。
- **WebUI 🟡（可选、后置、独立、控制台定位）**：独立客户端项目（如 main 的 web/），
  不进核心包；内容是 admin 控制台（用户/配额、审计查询、指标面板、飞轮作业视图），
  **聊天界面优先级最低**（薄壳，谁都会做）。时机：等持久化闭环（§19 ①②）和 API
  契约版本化完成后再动——UI 消费 API，API 没定型 UI 就是返工。
- **SDK ✅（优先级高于任何 UI）**：官方 thin client（类型 + SSE 流封装 + 契约测试）。
  对 API-first 产品，SDK 投入产出比最高：飞轮作业、自动化、未来控制台都调它，
  下游从此告别手拼 curl。

### 20.3 三个"不做"（战略边界）

不做 Plugin（设计文档已排除）；不做训练（RL 飞轮只到 JSONL）；不做本地单用户形态
（历史已证明该删）。

### 20.4 优先级

```
持久化闭环（轨迹落库 + 审计可查询）→ API 契约版本化 → 官方 SDK
→ （可选）控制台 WebUI → （最可选）聊天 UI
```

---

### 20.5 前端落地思路（仅思路，未实现）

能力再全，没有可视入口对外等于"不存在"——main 的 `web/`（Vue，29k 行，含聊天/
账号/管理/轨迹查看器）就是为此建的。但**前端是后端缺口的验收标准**：直接移植
会暴露 dev 的 API 面比 main 的前端预期窄一截：

| main 前端依赖的端点 | dev 现状 |
|---|---|
| 注册/登录/会话/run(SSE)/消息/usage | 有 |
| `DELETE /v1/me`（注销账号） | 无 |
| `GET/POST /v1/sessions/{id}/files`（工作区文件列表/上传） | 有但形态不同：用户级 `/v1/files`（POST 预签名直传 + commit + 列表/URL/DELETE，MinIO 直连不占带宽），非会话级工作区列表 |
| `GET/DELETE /v1/memories(/{id})`（记忆 CRUD） | 无（只有 remember/recall 工具，无 HTTP 面） |
| `GET /v1/admin/audit?event=` | 有但过滤参数不同（user/tool vs user/event） |
| `GET /v1/admin/traces(/{runId})`（轨迹回放查看器） | 有等价端点：`GET /v1/admin/trajectory/{run_id}`（路径名不同，随 runs 表 2026-09-28 落地） |
| run 请求体 `enable_search`/`builtin_tools`/`files` | 无（RunIn 只有 prompt/model） |
| SSE 事件 `plan` | 不发射 |

所以"做前端"实际上把 §20.4 的"持久化闭环 → 契约 → 控制台"打包提前——这是好事，
前端逼着后端补齐，而不是反过来。

三条路线（2026-09-27 讨论时推荐 A；**实际决策走 B，且已落地**）：

- **A. 完整移植 main 的 web/**：先补后端（memories CRUD、会话级 files 端点、
  deregister、run 扩展参数、audit 过滤对齐；轨迹落库与 traces 等价端点
  2026-09-28 已补，不在清单里），再把 web/ 搬进 dev 并
  **静态托管进 pi-py**（一个服务同时出 API + UI，自托管产品的标准形态）。代价
  最大，得到完整产品面。**已否决**——§19 已覆盖清单记录"已决策不搬 Vue 工程"：
  29k 行 Vue 是另一种维护负担，零构建三页与 dev 的后端节奏更匹配。
- **B. 为 dev 现有 API 建轻量控制台**（聊天 + 用户/配额/审计/metrics）：
  **已落地（2026-09）**——零构建三页挂 `/ui`（app.py:809-820，no-cache）：
  `app.html`（聊天：SSE 流式、模型选择器、思考过程折叠、markdown 渲染、文件
  上传面板、注册开关）、`admin.html`（管理台：stats/usage/audit/用户管理）、
  `trajectory.html`（轨迹时序图，三模式查看器）。实际范围超出 B 原定义——
  C 的"轨迹页"也一并做了；main 的面板形态（文件/记忆页）在 B 路线下不需要。
- **C. 先搬基础面板**（聊天/账号/admin），轨迹/文件/记忆页后续接上：折中。
  实际被 B 吸收。

落地路径（历史记录）：当时写的"若选 A：后端补齐 → 前端移植 → 静态托管 → 真实验证"
未执行。实际按 B 走的是**前端先行逼后端补齐**：三页前端驱动了 run 轨迹查询端点、
runs 表（迁移 0005）、`/v1/files` 预签名管线等陆续落地。

---

## 21. RAG 企业知识库 `src/pi/rag/`

> 需求来源：`RAG外派对接文档.md`（接口契约 + 交付清单 + 验收标准）。
> 状态：M0–M5 全部完成，真栈验证通过。本章是技术细节的唯一详述处；
> 进度与路线图见 `ROADMAP.md`，实测数据见 `.workbuddy/artifacts/`。

### 21.1 定位：一个可整体抽走的内核 + 三个薄壳

RAG 的设计目标不是"给 pi-py 加个知识库功能"，而是**工业级、可独立复用**：
`pi/rag/` 内核**零依赖 `pi.*`**（不 import `pi.tools` / `pi.server` / `pi.agent` / `fastapi`），
整个目录可以拷进任何别的项目直接用。内核只通过 **6 个 Protocol** 与外界交互，
具体后端全部依赖注入：

| Protocol | 职责 | 生产实现 | 离线默认件 |
|---|---|---|---|
| `Embedder` | 文本→向量 | `HttpEmbedder`（OpenAI 兼容 / DashScope） | `FakeEmbedder` |
| `ChunkStore` | SQL 真相源（文档/切块 CRUD） | `MysqlChunkStore` | `SqliteChunkStore` |
| `RagVectorStore` | 向量投影 | `MilvusRagVectorStore` | `InMemoryVectorStore` |
| `LexicalIndex` | BM25 词法检索 | `MemoryBM25Index` | 同左（进程内） |
| `Reranker` | cross-encoder 重排 | `HttpReranker` | `None`（跳过重排） |
| `UsageHooks` | 计量 + 可观测接缝 | `ServerUsageHooks` | `NoopHooks` |

**唯一允许 import `pi.*` 的内核文件是 `adapters.py`**——它把 pi 已有的
`EmbeddingClient` / `Database` engine / 计量 hook 适配成上述 Protocol。这条边界不是口头约定，
是**用 AST 检查真实 Import 节点钉死的**（`tests/test_rag_integration_m5.py` 用 `walk_packages`
扫整个 `pi.rag` 包，白名单只有 `pi.rag.adapters`；另有子进程断言"import `pi.tools` 不得
连带加载 pymilvus/adapters"，证明懒接缝真生效）。docstring 里合法提及禁词不会误报，
因为查的是 AST 的 Import 节点而非文本。

内核外的"三个薄壳"（都在 `pi.*` 侧，不在内核里）：

1. **`adapters.py`**（内核内，唯一耦合点）：`build_runtime()` 装配出一个 `RagRuntime`
   （store + vec + emb + lexical + reranker + retriever 的容器）。
2. **`pi/tools/rag.py` 的 `RagTool`**（name=`rag_search`）：实现 Tool 接口，
   自动享受 policy / 审计 / 配额（见 §7.1）。
3. **`pi/rag/cli.py`**：`pi-py rag {ingest, rebuild-index, eval, search}` 子命令。

### 21.2 真相源家规：SQL 是真相，Milvus 是可丢弃投影

`rag_docs` + `rag_chunks`（migration `0008_rag.py`）是**唯一真相源**；
Milvus collection `pi_rag_chunks` 只是**投影**——PK = `chunk_id`，可以整表 `drop()` 再从 SQL
重建（`pi-py rag rebuild-index`）。Milvus 里**不存 text**（SQL 才有），只存向量 + 过滤字段。

这条家规的推论：
- `RagVectorStore.drop()` 是合法的运维/测试原语，不是危险操作。
- 向量索引挂了不影响正确性，只降检索质量（见 §21.4 降级链）。
- **re-ingest 是 delete-then-insert，`chunk_id` 会移动**——所以 retriever 与 ingest
  **必须共享同一个 lexical index 实例**，否则 BM25 里留着旧 id，hydrate 会捞出**错误的块**
  （静默错答案，比报错更坏）。这是踩过的坑。

### 21.3 数据流

**Ingest（灌库）** —— 两个入口，同一内核：

```
入口 A：HTTP 上传（用户自助）     POST /v1/rag/ingest（multipart）
入口 B：CLI（运维/批量）         pi-py rag ingest --path <文件或目录>

  → parser.parse_file()      7 后端路由（md/txt/html/csv/tsv/xlsx/docx + pdf）
                             质量门（三个通用信号，均与文档内容/语言无关）：
                               ① 扫描版：text density < 阈值（无文本层）
                               ② 乱码：garbled-char 占比 > 30%
                               ③ 双栏/表格：行内最大字符间隙中位数 > 20pt（中缝）
                              任一命中 → needs_heavy_parser
  → [needs_heavy_parser] heavy_parser.parse()   外部 layout 解析（PI_RAG_HEAVY_PARSER，
                              默认 PaddleOCR-VL；MinerU 换配置即迁移）
  → clean_ocr_markdown()     vendor 中立清洗：行内 LaTeX→文本、HTML 表格→pipe 表格、
                              标签剥除（PaddleOCR/MinerU 输出同形状，共用）
  → 重新 parse（markdown）    清洗后的干净 Markdown 继续走正常 chunk/embed/index 链路；
                              未配 OCR 服务或 OCR 失败 → 落 NEEDS_HEAVY_PARSER
  → chunker                  语义切块（max_chars=800，overlap=120）+ contextual retrieval
                             embed_text = "文档标题 > 章节 > 小节\n\n" + text
  → embedder.embed()         批量向量化（真计费）
  → store.add_chunks()       写 SQL 真相（插入后按 (user_id,doc_key,seq) 查回真实 id，
                             不假设自增连续——MySQL lock_mode=2 interleaved 下并发会错配）
  → vector_store.upsert()    写 Milvus 投影
  → lexical.invalidate()     失效该用户的 BM25 缓存（下次检索重建）
```

ingest 终态：`READY`（含经 OCR 的扫描件）/ `INDEX_PENDING`（SQL 成功但向量缺失=活的降级）/
`NEEDS_HEAVY_PARSER`（需 OCR 但未配/失败）/ `FAILED`。

> HTTP 入口是异步的：上传立即返回 `pending`，后台任务跑完整条 ingest，状态写回
> `rag_docs.status`，前端轮询 `GET /v1/rag/docs`。文档管理端点（list/delete）也在此面：
> `GET /v1/rag/docs`、`DELETE /v1/rag/docs/{doc_key}`，全程按 `user_id` ACL。

**Retrieve（检索）**：见 §21.4。

### 21.4 检索机制：向量 + BM25 → RRF → rerank，四级降级不静默

```
query
  ├─ 向量通道：embed → Milvus int filter(user_id) 裸字面量 → top vector_k
  ├─ 词法通道：MemoryBM25Index.search(user_id, query) → top bm25_k
  ▼
RRF 融合（k=60，Cormack et al.）：Σ w_i/(k+rank_i)，向量 w=1.0，词法 w=lexical_weight
  tie-break 按 chunk_id 升序
  ▼
rerank（可选，HttpReranker cross-encoder）：对 top rerank_candidates 从头重打分
  ▼
final_k 命中 + 引用（doc / section / page）
```

**四级降级链**，每级都发 `on_retrieval` 噪音（**不静默**），outcome 词表与
`server/db.py` 对齐（8 个有界枚举值，不违反 Prometheus 基数纪律）：
```
hybrid（向量+BM25 都活）
  → vector_only（BM25 挂了）
  → bm25_fallback（向量挂了，纯词法）
  → sql_fallback（索引全挂，SQL LIKE 暴力兜底）
  → empty
```
rerank 失败是**附属系统失败不挂主流程**：保留 RRF 顺序、报 `rerank_failed`、
绝不因为重排端点死了就返回空（降级不能丢数据）。

**ACL**：v1 只做 user 级——Milvus int filter `user_id == ?` + SQL `WHERE user_id = ?`。
`RagTool` 有硬门：`ctx` 里 `user_db_id is None` → 直接 error **且不触达索引**
（未授权搜索会跨租户泄漏，"返回空"不是安全证据）。跨用户 0 泄漏有专门测试，
断言的是"走了过滤路径"而非只断言"没返回别人的数据"。

### 21.5 通道对称性（M4 血泪家规）

**所有消费 chunk 文本的阶段，看到的文本必须对称，都带 title_path**：

| 阶段 | 方法 | 内容 |
|---|---|---|
| 向量 embed | `Chunk.text_to_embed()` | contextual prefix（title_path）+ body |
| BM25 索引 | `Chunk.text_to_index()` | title_path + body |
| rerank 打分 | `RetrievedChunk.text_to_score()` | title_path + body |

第三个是 M4 收尾时才发现被漏掉的 bug：`HttpReranker` 原本只把裸 `c.text`（body）
喂给 cross-encoder，**丢了 title_path**——三通道里只有 rerank 看到的信息最少，
比产生它候选列表的两个通道还少。后果：答案**活在标题里**的 query（"LangSmith 在哪个
章节路径下"、"3.2 小节哪种异常"）cross-encoder 无从判断，退回 body 字面相似度，
把**同文档相邻块**顶上来。这在 A/B 里表现为 edge_case 切片 5 条 LOST（3 条从
rr=1.000 被打到 0.20–0.50）。修复后 edge_case 全线反超基线（见 §21.6）。

**家规**：任何"重排/重打分"阶段看到的信息**不得少于**产生其候选的阶段，
否则它会用更少的信息推翻更好的候选。

### 21.6 M4 A/B 评测结论（真栈，非 mock）—— **v1 单语料，已被 §21.6.1 部分推翻**

> ⚠️ **本节全部数字来自 v1 一个语料，不要单独引用。** 其中"BM25 唯一覆盖 0"与
> "降权单调改善"两条**已在第二语料 v2 上被证伪**（BM25 唯一覆盖 5/142，降权单调恶化）。
> 跨两语料都成立的只有：REFUTED 的融合侧旋钮（score gate / RRF k）、
> 以及 **cross-encoder 是真修复**。看结论请直接跳 §21.6.1。

评测先行铁律：M0 就建了 golden set（60 case，LLM 生成 + `answer_excerpts` 抗切块漂移）
+ Recall@k / MRR / 命中率 + A/B harness。M4 在真栈（真模型 + 本地 MySQL + 本地 Milvus，
390 chunk）上跑了 13 档配置，结论：

- **BM25 唯一覆盖 0/60**；gold 平均 rank **向量 1.53 vs 词法 2.63** → RRF rank-平均
  必然让 `hybrid_rrf60`(recall@5 0.950) **劣于** `vector_only`(0.983)。这是**预期行为，不是 bug**：
  RRF 只消费 rank，第二意见更弱时 rank-平均把 gold 拖到两者之间。
- **剂量响应单调**（因果确认）：`lexical_weight` 1.0→0.5→0.25→0.0 = recall@5 0.950→0.967→0.967→0.983。
- **REFUTED 的 4 个假设**：BM25 分值门（recall@5 变化恰好 0.000，因为 RRF 看不到 magnitude）、
  更严门 0.35（更差 0.933）、avgdl 畸变（prose penalty 仅 1.83x，限定 prose 反而更差）、
  RRF k（k=10/60/200 浮动仅 0.017）。**唯一有效的融合侧旋钮是 `lexical_weight`；真修复是 cross-encoder。**
- **cross-encoder 是真修复**：`hybrid_rrf60+rerank` 修掉 title_path bug 后，
  总体 recall@1 **0.742→0.908**、MRR **0.853→0.942**，edge_case 全线反超基线，
  case diff LOST 5→2、net +6→+11。
- **产出旋钮** `lexical_weight`（默认保持 1.0 论文中性值，改动必须有 A/B 证据；
  合理值是语料相关的）+ `PI_RAG_LEXICAL_WEIGHT`。
- **适用范围警告**：这是**一个** 390 chunk 里 351 是同质 CSV 行的语料。"BM25 无用"会过拟合；
  **可外推的结论是关于融合规则的**（无权重 RRF 把更弱通道当平等意见），这与语料无关。

### 21.6.1 corpus_v2 复核：v1 的"BM25 无用"结论被推翻

第二个语料 = 13 份中文医学指南 PDF（527 chunk，**全部 prose**，无 CSV 干扰块；
pdfplumber 不出 heading 层 → **title_path 全空**，§21.5 的对称性在本语料无从发挥）。
golden 151 case，rebind 后 142 scored。报告 `evals/reports/ab_20260929_023604.md`（全量）
与 `ab_20260929_112152.md`（排除 `lexical_leak` 的 81 case 子集）。

| | v1（390 chunk，351 同质 CSV） | v2 全量（527 chunk，纯 prose） | v2 排除 leak（81 case） |
|---|---|---|---|
| BM25 唯一覆盖 | **0** / 60 | **5** / 142 | **5** / 81 |
| gold 平均 rank（vector / lexical） | 1.53 / **2.63**（词法更弱） | 2.83 / **2.06**（词法更强） | 3.47 / **2.50**（词法更强） |
| recall@5：vector_only → hybrid | 0.983 → **0.950**（融合有害） | 0.762 → **0.806** | 0.663 → **0.721** |
| per-case diff（hybrid vs vector） | net **-4**（8 win / 12 lost） | net +9（32/23） | net +4 |
| per-case diff（rerank vs vector） | net **+11**（13 win / 2 lost） | net **+49**（56/7） | net **+30**（34/4） |
| `hybrid+rerank` recall@1 / recall@5 | 0.908 / 0.975 | **0.771** / **0.929** | 0.722 / 0.895 |

**两个门槛都满足**：
- 门槛 (a)（赢 per-case diff，不只是均值）：v2 全量 **+49**（56 win / 7 lost）、排除 leak 子集
  **+30**（34 win / 4 lost）。均值不是靠 8 胜 8 负凑出来的。
- 门槛 (b)（第二异构语料复核）：**v1 过拟合确诊**。"BM25 无用"是 v1 的 CSV artifact，
  不是普适规律。

**为什么 v1 的结论反了**：v1 语料里 351/390 是同质 CSV 行，向量通道在这种数据上近乎完美
（gold rank 1.53），BM25 是纯噪音源，融合只会把 gold 从第 1 名拖下来。v2 是长篇连续散文，
向量通道自己就不够准（gold rank 2.83），BM25 反而是更强的第二意见（2.06）——
融合从"拖后腿"变成"真帮忙"。

**关于 `lexical_leak`**：v2 有 42%（63/151）的 question 从 gold chunk 抄了长 verbatim span，
BM25 在这类 case 上属于"白赢"，不能作为词法通道价值的证据。因此跑了排除子集——
结论方向完全一致（BM25 唯一覆盖仍是 5，融合仍赢，rerank 仍 net +30），
说明 v2 的融合收益**不是抄原文抄出来的**。

**因此 `lexical_weight` 的正确值是语料相关的，默认保持 1.0（Cormack 中性值）不动**：
剂量响应在两个语料上**方向相反**，这是"该不该信第二意见由语料决定"的最强因果证据——

| `lexical_weight`（recall@5） | 1.0（shipped） | 0.5 | 0.25 | 0.0（=vector_only） | 方向 |
|---|---|---|---|---|---|
| v1 | 0.950 | 0.967 | 0.967 | **0.983** | 降权单调**改善**（BM25 是噪音） |
| v2 全量 | **0.806** | 0.803 | 0.799 | 0.762 | 降权单调**恶化**（BM25 有用） |

任何单语料上的"最优权重"都不该被 promote 成生产默认。v2 排除 leak 的子集上该 sweep
**非单调**（0.5→0.716、0.25→0.722、0.0→0.663），报告已标注"不要把这个 sweep 读成因果"
（子集只剩 81 case，noise 量级与 sweep 幅度可比）。

**生产结论（跨两语料一致，可 promote）**：`hybrid_rrf60 + cross-encoder rerank`
是唯一在两个语料上都赢的配置——v2 上把 recall@1 从 0.457 拉到 0.722、MRR 0.623→0.931，
v1 上 recall@1 0.742→0.908。**真修复永远是 cross-encoder，不是调融合权重。**
融合侧旋钮（score gate、RRF k）在两个语料上都 REFUTED：gate 变化恰好 0.000
（RRF 只消费 rank，看不到 magnitude），k=10/60/200 spread 仅 0.026–0.033。

**残留（已归因完毕，见 `evals/reports/badcase_v2_lost4_diagnosis.md`）**：v2 排除子集上曾有
4 条 LOST（`rag-038/045/094/096`，全 happy_path）。逐一挖完正文+分数后 **0 条是检索或 rerank
bug**，不影响门槛判定（4 vs 34，而 GAINED 有 34）——此前「怀疑相邻块正文相似导致 cross-encoder
分不清」的推测被测量证伪（全语料相邻重复块仅 0.6%）。四条根因：

- `rag-045`（连 `rag-046`）＝ golden set 缺陷：gold 块是 25 字符的 PDF 控制符垃圾
  （printable ratio 0.52），出题人在给「控制符序列」出题；rerank 把它压下去是对的。
- `rag-038` ＝ 语料缺陷：同一张 PDF 表格被 pdfplumber 抽成两种排版（`#15` 竖线碎片 38 字符 /
  `#16` 阅读序完整块 1958 字符），`_expand_any_of_gold` 因 verbatim 的竖线形态不匹配没把 `#16` 收进 gold。
- `rag-094` ＝ 语料缺陷：title_path 全空（527/527），cross-encoder 看不到「哪个食谱」，模板句
  把候选压到 1.5e-05 的分数差（`mine_badcases.py` 已改近 1.0 分数全精度打印，别再用 `.4f` 误判饱和）。
- `rag-096` ＝ 生成器缺陷：`_expand_any_of_gold` 把「冬季食谱1」（错误 section）的块误扩成 gold。

这四条还暴露了一个评测家规级的坑：**`_expand_any_of_gold` 在结构重复文档里会膨胀**
（`rag-017` = 54 个 gold key，占该文档 111 块近一半），配合 `recall_at_k` 的 **all-of** 语义
（`|gold found| / |gold|`，`test_recall_at_k_hand_computed` 用手算值钉死），**历史实际评分集**
（rebind 后 142 case / 排除 leak 81 case）的完美检索 recall@5 上限只有 **0.9708 / 0.9662**
（单 case 上限 0.093 = 5/54）。**家规：任何「perfect-retrieval ≠ 1.0」的评测数字，先查 gold
膨胀，再怀疑检索。** ⚠️ 口径陷阱：`ab_rag` 评分前会跑 `rebind_golden`，所以「文件里的 case 数」
≠「参与评分的 case 数」——拿未 rebind 的 151-case 文件离线算，会得到错误的 0.9545。

**已解决（2026-10-01）**，三条改动；**只有第 2 条改变了评测行为**：

1. **主指标改 any-of**：`hit@k` 升为 PRIMARY——RAG 的业务问题就是「答案找到了没」，一个含答案的
   chunk 即够；all-of `recall@k` 降为 SECONDARY 覆盖度指标。`EvalReport.metrics` 顺序、
   per-category/per-tag 表、`ab_rag.py` 的选优与 verdict 全部以 hit@5 为先；CLI 新增
   `--min-hit5`（主门禁），`--min-recall5` 保留为覆盖度门禁。历史 A/B 的**结论**不受影响——
   `case_diff` 一直用 `rr`（本身即 any-of 语义），所以「rerank 是决定性增益」从来不是 all-of 的产物。
2. **防复发 guard（唯一的行为变更）**：`MAX_ANY_OF_GOLD = max(KS) = 5` 同时装在
   `build_golden_set._expand_any_of_gold`（超限即**移除**该 case，meta 记 `template_repeat_dropped`）
   与 `harness.rebind_golden`（超限即 drop 并列入 rebind 报告）。**效果**：历史评分集中那 6 个
   gold 10–54 的 case（`rag-002/021/023/030/097/098`）不再参与评分 → 评分集 142 → **136**，
   完美检索 recall@5 **0.9708 → 1.0000**。真栈 A/B 复测（`ab_20261001_173721.md`）：`recall@5`
   **0.895 → 0.932**（leak 排除，81 → 77 case），而 `hit@1/hit@5/mrr` 几乎不动
   （0.914/0.963/0.931 → 0.909/0.961/0.928）——正是「消除 all-of 的结构性低估、不动 any-of」
   的预期指纹。
3. **清 golden set（文件清理，对指标零影响）**：`tools/prune_golden_set.py` 丢弃 9 个 gold>5 的
   模板重复 case（**151 → 142**，max gold 54 → 5）。这 9 个在 rebind 后本来就不参与评分（3 个
   `rag-017/019/027` 早已被判 cross-doc `ambiguous` 而 drop，6 个被第 2 条的 guard drop），所以
   **指标完全不变**。它的价值是让 golden 文件本身干净，并让「不跑 rebind 的离线分析」不再算出
   误导性的 0.9545。

另注：`corpus_v2.json` 142 case 中仍有 6 个（`rag-033/093/099/111/115/143`）每次 `rebind` 都会被判
**cross-doc ambiguous** 而 drop（excerpt 在不止一个文档出现）→ 实际评分集恒为 **136**。这是
chunking 相关属性、不是 case 缺陷，故**不做 prune**（rebind 会可见地报告它们，不是静默丢弃）。

rag-094/096 的**跨 section 误扩**（`#86`=冬季食谱3、`#37/#38`=冬季食谱1）**刻意不删**：它们的
gold 仍含**真** section 的块，any-of 主指标下「命中任一真 gold 即为 hit」永远正确；它们只稀释
已降级的 all-of recall，且手删也活不过 `rebind_golden()`（excerpt 确实在那些块里）。

工具 `tools/ab_rag.py`（`--reuse` 省 4 分钟、`--only` 复测单档省 5 分钟）、
`tools/mine_badcases.py`（bad case 端到端挖掘：query + gold 全文 + 各 config top-k 带 score +
"GT 是否真在 gold chunk 里"的空格归一化双检，专抓 PDF artifact 型 gold 缺陷）。

#### 解析层改进的评测验证（2026-10-02）

**动机**：`parse_pdf` 的双栏检测（→ 路由 PaddleOCR + `clean_ocr_markdown`）改的是**索引前的
输入**，它是否真的改善检索，只有 A/B 能回答。

**结果**（corpus v2，本地 5 份双栏医学指南 = 37 个 rebind 成功的 case；报告
`ab_20261002_144034.md`）：同一套检索配置下，**rerank 后的 top1 准确率接近翻倍**：

| metric | 旧（pdfplumber 双栏交错，142 case 全量） | 新（PaddleOCR 版面解析，37 双栏 case） |
|---|---|---|
| recall@1 | 0.429 | **0.824** |
| hit@5 | 0.883 | **0.946** |
| recall@5 | 0.854 | **0.946** |
| mrr | 0.672 | **0.878** |

**读数**：增益集中在 `recall@1`/`mrr`（top1 精度）而非覆盖率——正是「双栏交错把答案片段打散、
rerank 后仍排不到第一」这一失效模式的修复方向（同轮定性验证：答案片段在 pdfplumber 索引里
连续命中 0/4，PaddleOCR 索引 4/4）。

> **严谨性边界**：两组 case 集不同（37 双栏 vs 142 全量），**不是严格对照**。要严格对照需用旧
> 解析器在同一 37 case 上复测（临时关掉双栏路由）。当前 37 个 case 恰好都是双栏文档的 case，
> 而旧解析器在这批 case 上只会更差，故结论方向可信、量级待严格复测。

**踩过的两个评测工具坑（已修 / 已在 README 标注）**：
1. `ab_rag.py` 之前没给 `IngestPipeline` 传 `heavy_parser` → 双栏 PDF 被标 `needs_heavy_parser`
   后 `chunk=0`，评测**静默退化**成只剩单栏文档的 10 case。已修（从环境变量构建并装配）。
2. `ab_rag.py` 的 `PI_ITEST_DATABASE_URL` 默认值写死 `3306`，而本机 3306 被 CubeSandbox 占用、
   pi-py 的 MySQL 在 13306 → 不设就把语料灌进**错误的库**且不报错。见 `evals/reports/README.md`。

### 21.7 server 接线（M5）

照 `ctx.memory` 的**同一个接缝**注入，不新造机制：
- `ServerSettings.rag_enabled`（`PI_RAG_ENABLED`，默认开=能力 fail-safe）：关掉则
  `BuiltinToolProvider(rag_enabled=False)` 让模型**根本看不到** `rag_search`
  （看得到却总报错比没有更坏）。
- `app.py` 装配 `RagRuntime` + `ServerUsageHooks`：rerank 走**独立 model tag**
  `rerank/<model>`（否则月度按模型分解会说谎）；`metrics.retrieval(outcome=f"rag_{outcome}")`。
- `RunManager(rag=runtime)` → `agent.ctx.rag = runtime`（`ToolContext.rag` 字段照 `memory` 先例声明）。
- lifespan 关闭 runtime；`app.state.rag` 暴露给端点。
- **embedding 统一为 OpenAI 兼容形状**（`/compatible-mode/v1/embeddings`）。`pi.llm.embedding.EmbeddingClient`
  与 `HttpEmbedder` 现在都只认这一种，不再有 style 选择——换模型只需改 URL+model。
- **`allow_memory_vector`：CLI 可 True（单进程自洽），server 必须 False**
  （多 worker 下进程内索引看着健康却啥都不返回，比诚实降级更坏）。

### 21.8 配置

内核 `RagConfig.from_env()` 读全部 `PI_RAG_*`（ServerSettings 层的四个开关/限额见 §13，内核参数表见下文）。要点：
- **embedding 复用 server 级 `PI_EMBEDDING_*`**（同一端点也服务语义记忆），
  `PI_RAG_EMBEDDING_*` 可按部署覆盖。
- **Milvus 复用 `PI_MILVUS_URI`**，`PI_RAG_MILVUS_URI` 可覆盖；两者都空 →
  standalone 用 `InMemoryVectorStore`，server 则禁用向量通道。
- `rrf_k` / `rerank_enabled` / `rerank_candidates` / `sql_fallback_k` **没有环境变量**，
  走 `RetrievalConfig` 代码默认（改动属于调参，须过 A/B，不暴露成运维旋钮）。

### 21.9 CLI 偏差记录

任务书 §4.9 描述的 `--file-id`（走 `tools/files.py` / `FileRepo` / `ctx.files`）在**本分支不存在**，
`ToolContext` 也没有 `fs` / `ensure_runner`。故走 §4.7 的 `--path`（文件或目录递归），
已在 `cli.py` docstring 记录此偏差与理由——**以代码为准，不以任务书为准**。

---

## 附录：一次 run 的时序（文字版）

```
用户 -> POST /runs (token)
  鉴权: JWT 解码 -> jti 黑名单? -> 撤销纪元? -> 账号激活?
  限流: 每用户窗口计数 -> 429?
  配额: 当月用量 -> 402?
  -> SSE 200 打开
  RunManager: 抢会话锁(失败->流内 error) -> 并发信号量 -> 读历史
  AgentLoop: [压缩?] 流式调模型 -> 工具调用? -> 策略检查 -> 执行(沙箱?) -> 回喂 -> 循环
  成功: 批量持久化消息 -> 记用量
  finally: 释放锁
用户 <- event: done
```

