# pi-py 开发者手册（ARCHITECTURE.md）

> 面向后来人的完整说明：项目是什么、怎么设计的、每个模块每个函数干什么、
> 如何启动和使用、有哪些坑。读完本文 + `README.md`，你应该能独立维护和扩展这个项目。
>
> 最后更新：2026-09-06 · 代码规模约 10,600 行 Python 源码 + 364 个离线测试（363 passed / 1 skipped，
> **套件曾 flaky、现已修复**：`TestDeregister` 与后台记忆抽取赛跑，实测 10 次全套件运行有 6 次会红这一条，
> 修后单独跑 30/30、全套件连跑 5 次一致。**注意修的是测试的确定性，注销与在飞抽取之间那个
> 生产级竞态仍然开着**，见 `deploy/environments.md` L11 与 §15）
> + 4 个真实基建集成测试（`pytest integration/`，花真钱，见 §15 末尾），
> 外加 `web/` 的 Vue3+TS 前端（手写约 3,400 行 + 测试约 1,000 行，56 个单测 / 11 个联调用例，见 §18；
> 另有 codegen 生成的 `api/schema.d.ts` 约 2,400 行，不计入手写）
>
> ⚠️ 上面的数字会随开发漂移，**不要当验收标准**。要基线就跑 §15 那两条命令。
> 环境与生产/测试之分的说明见 **`deploy/environments.md`**（14 条已核实的"地雷" L1–L14）。
> 其中 **L1**（生产 schema 被 `create_all` 污染）、**L12**（`.env` 里 `PI_METRICS_TOKEN`
> 被空值遮蔽 → 生产 `/metrics` 无鉴权）与 **L14**（conftest 钉死清单漏了 `PI_METRICS_TOKEN`，
> 由 L12 的修复暴露）**已于 2026-09-06 22:00–22:15 修复并验证**；
> 仍然开着的是 **L3**（生产缺 `PI_PUBLIC_BASE_URL` → 附件功能关闭）、**L4**（两环境共用
> `PI_JWT_SECRET`）、**L5**（另 5 个测试账号仍是公开默认密码）、**L13**（产品没有改密码接口）。
>
> 本包已收敛为**纯服务端形态**：本地单人 CLI/TUI、本地 SQLite 会话存储、
> Windows/WSL 支持均已移除（见 §12）。
>
> **数据架构（2026-09-05 起的定位）：MySQL 是唯一真相源**——用户、会话、消息、
> 计量、长期记忆（含 embedding blob）、审计事件、执行轨迹全部落库；Milvus 只是
> 可丢弃可重建的向量索引（`tools/rebuild_milvus.py`）；JSONL 审计是镜像不是本体。

---

## 1. 项目定位

pi-py 是 **earendol-works/pi**（TypeScript 版编码智能体外壳）的 **Python 实现**，
目标不是做一个聊天机器人，而是一个**可执行工具、可审计、可多租户部署的编码智能体服务**。

它只有**一个运行形态**：多用户服务（`pi-py serve` + HTTP/SSE API）。内核
（`pi.agent` + `pi.llm` + `pi.tools`）与运行形态无关，可以单独 import 使用，
但仓库里不再有本地单人入口。

| 形态 | 入口 | 场景 |
|---|---|---|
| **多用户服务** | `pi-py serve` + HTTP/SSE API | 企业级服务：JWT 登录、配额、限流、审计、Docker 沙箱 |

> 历史上还有"本地单人"形态（`pi-py chat / tui / run`，SQLite 存会话）。它已连同
> `pi/tui/`、`pi/session/`、`pi/env.py` 一起删除；`pi.cli` 只剩 `serve` / `migrate`。

设计上的北极星原则（理解所有代码的钥匙）：

1. **一次 run 是原子单位**：要么整轮对话（含所有工具调用）成功后一次性持久化，要么失败不留半截状态。
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
pip install -e ".[dev]"         # 只跑测试：pytest + aiosqlite（测试用一次性 SQLite 文件）
```

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

跑过 `cd web && npm run build` 之后，同一个端口还提供浏览器界面：直接开
`http://127.0.0.1:8300/`。前端由 app 进程自己托管（同源，没有 CORS），细节见 §18。

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
│  tools/* (8 个工具 + sandbox)   llm/* (provider 适配 + 降级链)│
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
  ├─ async with asyncio.timeout(600s):
  │    AgentLoop.run(prompt):
  │      追加用户消息 → 调 provider.stream() 流式收事件
  │        → 模型要工具？→ policy.check() → 执行 → 结果回喂 → 再来一轮
  │        → 不要工具？→ 结束
  │      （历史超阈值先压缩；每步写审计和 trace span）
  ├─ 成功：整批消息 MessageRepo.append_many（一次事务）
  ├─ 成功：UsageTracker.record（失败只记日志，不回滚对话）
  ├─ 成功：AgentRunRepo.append 轨迹落库（run 行 + steps，一次事务；失败只记日志）
  │        └─ 顺带每小时一次：轨迹保留期清理 delete_older_than
  ├─ 后台不 await：记忆抽取 spawn_extraction（repo 先落、Milvus 镜像 best-effort）
  └─ finally: 释放 session 锁
```

一次 run 在 MySQL 里的完整留痕：`messages`（对话本体）、`usage_records`（计量，
含记忆开销的 `turns=0` 行）、`agent_runs`+`agent_steps`（执行轨迹）、
`audit_events`（每个工具调用与认证事件，经后台 drainer 异步落）、
`user_memories`（抽取出的持久事实）。`sessions.plan` 缓存当前计划。

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
│   │   ├── bash.py read.py write.py edit.py grep.py find.py ls.py plan.py
│   │   └── sandbox.py       命令执行隔离：LocalRunner / Docker 冷路径 / 预热池；
│   │                        SandboxLimits（内存/pids/cpu/user）在四处建容器路径统一生效
│   ├── security/
│   │   ├── policy.py        策略引擎（拒绝清单/命令模式/路径沙箱）
│   │   ├── redact.py        出站脱敏（发给模型前遮蔽密钥）
│   │   └── audit.py         审计 JSONL（按天滚动）
│   ├── observability/
│   │   ├── tracing.py       noop/jsonl/otel 三后端 span（otel 经 OTLP/gRPC 导出，span 成树）
│   │   ├── metrics.py       Prometheus 计数器/直方图 + /metrics 渲染
│   │   ├── metering.py      用量记录 + 月度汇总 + 配额检查
│   │   └── prices.py        模型单价表 + 成本估算
│   ├── memory/              长期记忆（MySQL 是真源，Milvus 只镜像）
│   │   ├── repo.py          UserMemoryRepo：user_memories 表，每个谓词都带 user_id
│   │   ├── store.py         VectorStore 抽象 + Milvus 实现；pymilvus ImportError 时退 NoOpStore
│   │   ├── embed.py         向量化（维度钉死 PI_EMBEDDING_DIM，空串过滤见 .env 注释）
│   │   ├── rerank.py        二段检索的精排端点；未配置则退回单阶段 cosine
│   │   ├── extract.py       run 结束后从 transcript 抽事实（注入指令当数据不当命令）
│   │   ├── arbitrate.py     定时矛盾/重复梳理（Redis 锁，多副本安全，只能 merge）
│   │   └── service.py       编排：检索注入 + 后台抽取 + 维护循环（ensure_index/sync_pending/decay）
│   └── server/              多用户服务（FastAPI）；`__init__.py` 惰性导出 create_app，
│       │                    只 import db.py 不会连带拉起整个 app（缘由见 §17）
│       ├── config.py        ServerSettings.from_env（所有环境变量）
│       ├── app.py           路由 + 认证依赖 + 启动引导 + 响应模型（OpenAPI 契约，见 §11.3.1）
│       ├── runner.py        RunManager + SSE 序列化
│       ├── db.py            SQLAlchemy ORM + 仓储（MySQL / PG 通用）
│       ├── cache.py         缓存/锁后端（内存 / Redis）
│       ├── auth.py          PBKDF2 哈希 + JWT
│       └── ratelimit.py     每用户固定窗口限流
├── tests/                   364 个测试（纯本地可跑，无外部依赖）
├── migrations/              Alembic 迁移 0001–0007（head = 0007_trace_fidelity：建表、用户激活、
│                            会话计划列、用户记忆、审计事件、run 轨迹、轨迹保真度）
├── tools/loadtest.py        SSE 压测工具
├── tools/seed_testdb.py     给 *_test 库灌可复用的测试数据（幂等，拒绝跑在生产库上）
├── tools/sandbox_bench.py   docker 预热池容量压测（直打 sandbox 层，扫并发用户数）
├── tools/rebuild_milvus.py  从 MySQL 重建向量索引（零 API 调用；生产 ns 需 --yes）
├── tools/dump_openapi.py    用一次性 SQLite 起 app，dump openapi() → web/openapi.json
├── web/                     前端（Vue 3 + TS + Vite）；openapi.json 是入库的契约，
│                            TS 类型由它 codegen 生成，不手写
├── deploy/                  Caddyfile（SSE 友好 TLS）、云端部署手册、.env 模板、
│                            environments.md（生产/测试环境对照与地雷清单）
├── policy.json              服务端安全策略（`PI_POLICY` 指向它；两个 compose 也挂这一份）
├── Dockerfile               多阶段镜像（含 alembic，支持 `pi-py migrate`）
├── docker-compose.yml       本地全栈（app+PG+Redis）
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
| `Plan` | 结构化任务计划（title + steps） | `submit_plan` 的载荷。边界是承重的：这是**不可信的模型输出**，要落 TEXT 列、要走 SSE 帧，200 + 20×300 把一份计划封在 ~6KB。steps 是纯字符串而非对象——没有 status 字段，Phase 1 不追踪进度，现在冻结枚举值将来还得拆掉 |
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
| `on_message` | None | 每条新消息的回调（服务层用它攒批，run 成功后一次事务落库） |
| `max_turns` | 40 | 防死循环：模型反复调工具的硬上限 |
| `compact_threshold` / `compact_keep` | 80,000 / 8 | 压缩触发阈值（字符数）/ 压缩后保留的尾部消息数 |
| `policy` / `audit` / `tracer` | None | 安全策略 / 审计 / 追踪（§9、§10） |
| `session_id` / `user_id` | — | 只用于审计和 trace 打标 |

方法逐个说：

| 方法 | 作用 |
|---|---|
| `run(user_text)` | 公开入口：追加用户消息 → 包一层 `agent.run` trace span → 委托 `_run_inner` |
| `_run_inner` | 主循环：① 超阈值先压缩；② while 循环：调 `provider.stream`（出站消息先过 `redact_messages` 脱敏）、收 TextDelta 边收边 yield、收 ToolCallDelta 按 id 攒参数；③ 有工具调用且 `stop_reason=="tool_use"` 就逐个 `_run_tool` **顺序**执行（成功执行的终止型工具结束本轮，见下）、结果作为一条 user 消息回喂（批内每个调用恰好一个结果块、按调用顺序）、继续循环；否则跳出；④ 任何异常转成 `ErrorEvent`（UI 永远能收到结构化结果）；⑤ 最后必发 `TurnEndEvent(usage, turns)` |
| `_run_tool(call)` | 单工具执行的完整管线：未知工具→错误块；解析 arguments（必须为 JSON 对象）→失败→错误块；`policy.check()` 拒绝→错误块+审计拒绝记录；执行崩溃→错误块+审计；成功→结果块+审计（并透传 `Tool.terminal` 与 `ToolResult.payload`——错误路径一律带默认值，**失败的终止型工具结束不了本轮**）。错误块（`is_error=True`）会回喂给模型，让它知道失败并可自我纠正 |
| `_maybe_compact` | 估算大小超阈值则调 `compact()`，替换 `self.messages`，发 `CompactionEvent` |
| `_audit` | 审计写入封装：出站参数也先脱敏再记 |

**终止型工具的批内语义**（`Tool.terminal = True`；`submit_plan` 是第一个，Phase 2
的审批工具会是第二个）：

- **顺序执行，不提位**：`submit_plan` 之前的调用照常真跑（有副作用、有审计）。把它
  提到批首会静默否掉模型合法的前置调用（比如先 `ls` 看一眼再规划），还会让 transcript
  里结果与调用错位。
- **终止以成功为条件**：只有 `is_error == False` 的终止型工具才结束本轮。被策略拒绝
  或参数畸形的 `submit_plan` 会把错误回喂、本轮继续——让模型修参数，而不是让用户
  既没计划也没回答。
- **同批剩余调用合成跳过结果**：不进 `_run_tool`（`tool.execute` 不被触达就是重点）、
  也不过 policy（策略管的是执行，没执行就无可管）。合成 `is_error=True` 的
  `ToolResultBlock`（文案含 "did not execute and had no side effects"——下一轮模型读
  历史时要能分辨"没跑过"和"跑了一半失败"），补发 `ToolCallEndEvent(ok=False)`
  （`ToolCallStartEvent` 对每个参数分片都发过，不发 end 帧那一行 UI 会永远停在
  "执行中"），并逐条审计（`allowed=False`、`reason="skipped: ..."`、`ok=None`）。
- **配对不变量是这里唯一真正难的部分**：OpenAI/Anthropic 都拒绝"assistant 里有
  `tool_call` 但紧随 user 消息缺对应 `tool_result`"的请求，而且是在**下一轮**才拒绝
  ——不是产生它的那一轮。所以回喂的那条 user 消息必须为批内**每个** `ToolCallBlock`
  提供恰好一个结果块、按调用顺序。跳过合成删掉的话，历史存进去就再也读不出来了。
- 终止且 payload 是 `Plan` 时，最后 yield 一个 `PlanEvent`（每次 run 至多一个，见
  §6.2），然后 break。

**为什么 `run` 是异步生成器（yield 事件）而不是返回最终结果？**
因为消费端（HTTP SSE 客户端）需要在过程中实时渲染——模型每吐一个字、每调一个工具
都要即时可见。事件流是唯一不需要缓冲整轮的方案。

### 6.2 `events.py` — 事件词汇表

`TextDeltaEvent / ToolCallStartEvent / ToolCallEndEvent(ok, result 预览) /
CompactionEvent / PlanEvent / TurnEndEvent / ErrorEvent`。
事件对象本身不含任何传输格式；唯一的序列化点是服务层的
`runner.event_to_sse()`——把事件拍平成 SSE 帧（`event:` 名 + `data:` JSON）。

三个**只有读 `loop.py` 才知道**、但客户端必须知道的性质：

- **`ToolCallStartEvent` 是在 `ToolCallDelta` 分支里 yield 的**（`loop.py:141`），
  所以它是**每个参数分片发一次**，同一个 `id` 会到达好几次；而且它**只带 `id` 和
  `name`，不带 arguments**。客户端必须按 `id` 去重，不能把每一帧当成一次新调用；
  想展示"要调什么参数"只能等落库后从 `GET /messages` 拿（前端 §18 就是这么做的）。
- **`ToolCallEndEvent.result` 是预览，不是结果**：发出前就被
  `content[:PREVIEW_LEN].replace("\n", " ")` 截到 **200 字符**并把换行压成空格
  （`loop.py:166`，`PREVIEW_LEN` 是模块常量）。完整结果只存在于**回喂给模型并落库的**
  那条 `ToolResultBlock` 里。`event_to_sse()` 自己还有一个 400 字符的上限，但因为
  上游已经截到 200，**那个 400 永远不会生效**——SSE 文档里直接引用了 `PREVIEW_LEN`
  这个常量，`test_server.py` 有一例盯着它，改常量而不改文档会红。
- **`PlanEvent` 每次 run 至多一个**，位置在最后一个 `ToolCallEndEvent` 之后、
  `TurnEndEvent` 之前——`submit_plan` 是终止型工具，run 随之结束。**被跳过的同批调用
  仍然各有一个 `ToolCallEndEvent`（`ok=False`）**：start 帧对每个调用的每个参数分片都
  发过，少一个 end 帧那一行 UI 就永远停在"执行中"。

### 6.3 `compaction.py` — 上下文压缩

| 函数 | 作用 |
|---|---|
| `estimate_size(messages)` | 粗略字符数（文本+工具参数+工具结果），作为阈值判据 |
| `render_messages(messages)` | 把消息拍成人类可读文本喂给摘要模型，单块截断 2000 字符 |
| `compact(provider, messages, keep_last)` | 取 `messages[:-keep_last]` 让 LLM 总结成纪要，返回 `[摘要标记消息] + 尾部原文`；摘要为空则原样返回（dropped=0） |

摘要提示词（`COMPACTION_PROMPT`）强制保留：目标与约束、已做决定及理由、
文件变更、命令结果、未完成事项。

**压缩结果目前不落库**（值得注意的一处实现现状）：`AgentLoop` 有 `on_compact` 回调
可用于持久化，但服务端**没有接线**——`RunManager` 只传 `on_message`，`MessageRepo`
也只有 `append_many` / `list_for_session` / `count_for_session`，没有"重写整个会话"的
方法。后果：压缩只在当次 run 的内存里生效，库里保留完整原始历史，下一轮重新加载、
重新压缩。这是**功能正确**的（本轮新消息的 `idx` 由 `count_for_session` 递增，不受
前缀压缩影响），代价是长会话每轮都重付一次摘要 LLM 调用、且消息表只增不减。
若要修，入口是给 `MessageRepo` 加一个事务性的 `replace_many`，再在 `runner.py`
里把 `on_compact` 接上（原 CLI 的 `SessionStore.replace_messages` 就是这么做的）。

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
    terminal: bool = False  # 终止型工具：成功即结束本轮（见 §6.1 批内语义）
    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult
    # ToolResult: content + is_error + payload(Any=None)
```

- `ToolContext`：`cwd`（工作目录）、`max_output`（30,000 字符，防撑爆上下文）、
  `runner`（命令执行器，None=本机直跑，沙箱模式下注入，见 §7.3）。
- `resolve_path(ctx, raw)`：相对路径按 `ctx.cwd` 解析——**所有文件工具的寻址基准**。
- `SKIP_DIRS`：grep/find 自动跳过的目录（.git、node_modules、venv…）。
- `truncate()`：统一截断并附 `[truncated, N more chars]` 提示。
- `terminal`：**类属性而非循环里的工具名单**。终止语义由 `AgentLoop` 统一实现
  （§6.1），循环至今不硬编码任何工具名——再加第二个终止型工具（Phase 2 的审批
  工具）不需要动循环的心脏。
- `ToolResult.payload`：结构化副产物。工具结果本身（`content`）回喂模型，payload
  则交给循环变成事件——今天唯一的用法是 `submit_plan` 装一个 `Plan`，循环据此
  yield `PlanEvent`。

**为什么工具结果有 `is_error`？** 失败不是异常——把错误作为正常结果回喂给模型，
让它读到报错并自我纠正（改参数重试），这是编码智能体可用性的关键。真正的异常
（工具崩溃）由 `AgentLoop._run_tool` 兜住转成错误块。

### 7.2 八个内置工具一览

| 工具 | 参数 | 行为与关键限制 |
|---|---|---|
| `bash` | command, timeout(≤600, 默认120) | 走 `ctx.runner`（本机或沙箱）；输出合并、返回退出码；非零退出且输出以 `Error:` 开头 → `is_error` |
| `read` | path, offset, limit | 带 6 宽行号输出（模型做 edit 时按行号定位）；默认 2000 行、单行 2000 字符；NUL 字节判二进制拒读 |
| `write` | path, content | 自动建父目录、整体覆盖写 |
| `edit` | path, old_string, new_string, replace_all | **精确字符串替换+唯一性守卫**：匹配 0 次报"未找到"，多次且未开 replace_all 报"不唯一"；成功返回 unified diff（≤60 行）。这是防模型"凭印象改文件"的核心护栏 |
| `grep` | pattern, path, include | Python `re` 正则全树搜索，返回 `path:line: text`；≤200 条、单文件 ≤1MB；跳 SKIP_DIRS 和二进制 |
| `find` | pattern, path | fnmatch 相对路径 glob，≤500 条 |
| `ls` | path | 目录在前（带 `/`）、文件带大小，≤500 项 |
| `submit_plan` | title(≤200), steps(1–20 条，各 ≤300) | **Phase 1 任务规划，只记录不拦截**：按 `Plan` 模型边界校验，payload 装 `Plan`；`terminal=True`——成功即结束本轮（同批剩余调用合成跳过结果，§6.1）。无路径参数，policy 无从拒绝 |

`all_tools()`（`__init__.py`）返回全部八个的实例列表，是唯一的工具注册点。
**加工具就在这里注册**（扩展指南见 §16）。

**工具面刻意不含任何出网工具**。历史上的 `web_fetch` / `web_search`（`tools/web.py`）是
**删掉而不是修好**的：本地抓取跑在应用进程内、不进容器，所以 `PI_SANDBOX=docker` 和
`--network none` 对它毫无约束，而它没有地址校验且跟随重定向——等于一条通往云厂商元数据
服务的开放 SSRF（完整因果见 §17 第 20 条）。联网能力改由**模型端点自己的** builtin tools
提供：`POST /v1/sessions/{id}/runs` 请求体的 `builtin_tools` 字段（`web_search` /
`web_extractor` / `code_interpreter`，见 `RunIn` 与
`llm/openai_provider.py::BUILTIN_TOOL_TYPES`），由 provider 侧发起请求，不涉及本进程的
网络位置。`tests/test_security.py::test_the_local_tool_surface_has_no_internet_tool` 钉住
这条不变量：**任何注册进 `all_tools()` 的工具，其所在模块都不许 import HTTP 客户端**
（`sandbox.py` 不在 `all_tools()` 里，它的 httpx 是打 Docker Engine API，不是出网）。

### 7.3 `sandbox.py` — 命令执行隔离（本层最复杂的文件，~800 行）

三种执行器，实现同一个 `CommandRunner` 协议（`run(command, cwd, timeout)` +
`prewarm(cwd)` 预热钩子）：

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
| `set_plan(session_id, plan_json)` | 写 `sessions.plan`（`Plan.model_dump_json()` 的字符串）。**last write wins，没有计划历史表**——产生每个计划的 `submit_plan` 调用本身已是 messages 里不可变的 `ToolCallBlock`，历史不缺；per-session run 锁保证写者唯一。调用时机在 `append_many` 之后（§11.5） |

`sessions.plan` 是该表**唯一的 nullable 列**，且必须 nullable：MySQL 拒绝 TEXT 列的
字面 DEFAULT（错误 1101），PostgreSQL 拒绝在已有数据的表上 ADD COLUMN ... TEXT
NOT NULL 而不给默认值——同时满足两家的形状只有 NULL。`test_mysql_compat.py` 钉着
这一点（完整经过见 §17.24）。

`MessageRepo`：

| 方法 | 作用 |
|---|---|
| `append_many(session_id, entries)` | 批量追加，`entries` 为 `[{'idx', 'role', 'blocks'}, ...]`；`blocks` 是 `Message.model_dump_json()` 的字符串。一次事务写完——这就是原则 1（run 原子性）的落地点 |
| `list_for_session(session_id)` | 按 `idx` 升序读回，调用方用 `Message.model_validate_json` 反序列化 |
| `count_for_session(session_id)` | 现有消息数，`RunManager` 用它算新消息的起始 `idx` |

**没有"重写整个会话"的方法**（原 `SessionStore.replace_messages` 的对应物不存在），
所以压缩结果无法落库——完整因果与修法见 §6.3。

---

## 9. 安全层 `src/pi/security/`

### 9.1 `policy.py` — 策略引擎

所有工具执行前的闸门（`AgentLoop._run_tool` 里调用）。

| 组件 | 作用 |
|---|---|
| `Policy` | 四个开关：`deny_tools`（工具黑名单）、`deny_command_patterns`（bash 命令正则黑名单，大小写不敏感）、`path_sandbox`（路径沙箱）、`redact`（出站脱敏） |
| `load_policy(path)` | 从 JSON 文件加载；路径为空 → 返回 None（没有策略对象 = 全放行） |
| `check(policy, tool, args, cwd)` | 返回 `PolicyDecision(allowed, reason)`。顺序：工具黑名单 → bash 命令模式 → 路径沙箱（把 `read/write/edit/ls/grep/find` 的路径参数解析为绝对路径，必须落在 `cwd` 之内，防 `../../` 逃逸） |

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

### 9.3 `audit.py` — 审计日志（MySQL 优先）

`AuditLogger`：**每条记录先落 MySQL `audit_events` 表，JSONL 文件只是镜像**。
写路径是有界的 `asyncio.Queue`（容量 10000）+ 一个后台 drainer 任务（批 100 条、
一个事务一批）；`attach_db(db)` 在 lifespan 里挂库、`close()` 取消 drainer 并冲刷
余量。三个刻意的容错决定：

- **队列满 / 插入失败只告警，不抛**——审计失败绝不能顺着请求路径炸掉一次 run；
  落不了库的那批记录留在 JSONL 镜像里（`tests/test_security.py` 有失败注入测试：
  第一批失败不影响后面的批次继续入库）。
- **drainer 每批独立 try/except**：一批坏数据（或一次网络抖动）只丢那一批。
- **管理端查询读表不读文件**（`GET /v1/admin/audit`）：历史不再受每日文件轮转限制，
  actor/tool/event 三个索引列做过滤，500 条封顶。记录比被审计的请求**晚一拍**到达
  （异步 drainer），查询侧要容忍。

`audit_events` 表的形状：`ts` / `event` / `actor`（user 和 username 归一；
既是租户过滤键也是**注销擦除键**）/ `tool`（仅 tool_call 事件，其余 NULL）/
`payload`（**记录原样 JSON**——记录加字段不需要迁移，管理端 API 形状也永远不变）。
迁移 `0005_audit_events.py`。

记录类型：`tool_call()`、`compaction()`、`memory()`、`auth()` 四种。JSONL 镜像
仍然保留（`audit-<日期>.jsonl`，按天滚动，线程锁保护）：用普通工具就能读、数据库
整个挂掉时是最后的痕迹。

`auth()` 记注册和登录的**每一次尝试**（`action` / `ok` / `reason`），带客户端 IP 和
User-Agent，**不记密码**。三点注意：

- **所有字段都截断**（username ≤64、ip ≤45、ua ≤200、reason ≤32）。登录失败路径是攻击者
  控制的，而 `LoginIn.username` 没有长度限制——不截断的话，一个请求就能把审计表写满。
  JSONL 本身会转义换行，所以伪造不出第二条记录（`tests/test_security.py` 钉了这条）。
- **`ip` 只有在 `PI_FORWARDED_ALLOW_IPS` 覆盖到反向代理时才是真客户端**，否则记的是
  Caddy 容器 IP。详见 §17 第 16 条。
- 审计数据从此**含个人数据**（IP + UA）。审计表暂无保留期（取证需要历史完整）；
  JSONL 镜像按天滚动即自然封顶，数据库侧将来要定策略再定。

---

## 10. 可观测层 `src/pi/observability/`

### 10.1 `tracing.py` — 三种 tracer 后端

统一接口：`tracer.track(name, attrs)` 上下文管理器产出 span，异常自动记
`set_status(False)` + `record_exception`，结束记耗时。

- `NoOpTracer`：默认，零开销；
- `JsonlTracer`：零依赖，每个 span 一行 JSON 写 `~/.pi-py/traces-日期.jsonl`；
- `OtelTracer`：桥接 OpenTelemetry SDK 并经 **OTLP/gRPC** 导出到采集器（Jaeger /
  Tempo / OTel Collector）。没装 exporter 就回落 jsonl，**并且打一条 warning**——
  静默回落正是"设了 `PI_TRACER=otel`、采集器里什么都没有、又不知道为什么"的成因。

**span 是成树的，不是平铺的**：`Tracer.track` 把当前 span 压进一个 `ContextVar`
栈，子 span 拿栈顶作 parent。用 ContextVar 是因为一次 run 在一个 asyncio task 里流式
执行，挂在 tracer 实例上的属性会跨请求串味。

- `JsonlTracer`：`trace_id` **按根 span 生成**（一次 run 一个 trace），子 span 继承
  并带上 `parent_span_id`，根 span 的 `parent_span_id` 为空。此前它是**进程级**生成
  一次——开机以来所有 run 共用一个 id，日志文件根本无法按 run 还原成树。
- `OtelTracer`：`TracerProvider` **按实例持有，绝不注册全局**。
  `trace.set_tracer_provider` 拒绝覆盖，第二次 `create_app`（每个测试、每次 reload）
  会继续往第一个 provider 导出，shutdown 也 flush 错的那个。provider 挂了
  `BatchSpanProcessor(OTLPSpanExporter(...))`——**此前只有裸 provider，没有任何
  processor，span 建好、计时、结束，然后一条都出不去**。采样是
  `ParentBased(TraceIdRatioBased(rate))`：决定在根上做一次，整棵树要么全采要么全不采，
  不会留下半棵树。`OtelSpan` 适配器把本模块的 `set_status(ok, desc)` 翻成
  `StatusCode`——OTel 自己的 `set_status` 收到 bool 会静默丢弃并告警，而那恰好是
  异常路径。`shutdown()` 在 lifespan 收尾时 flush 批处理器。
  构造函数留了 `exporter=` 注入口，测试用它塞 `InMemorySpanExporter`，
  不必把 OTLP 指向一个没人监听的端口再读回重试告警。

埋点位置：`agent.run` → 内含 `llm.call`（每轮，带 stop_reason 与本轮 token）、
`tool.call`（每次）、`memory.retrieve`（每次 run 一次）→ 内含 `memory.embed` /
`memory.recall` / `memory.join` / `memory.rerank` 四个阶段。

`get_tracer(backend, ...)` 按 `PI_TRACER` 选择；端点解析顺序
`PI_OTLP_ENDPOINT` → `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` →
`OTEL_EXPORTER_OTLP_ENDPOINT` → `http://localhost:4317`，`http://` 前缀即用明文
gRPC（同机/同 compose 网络的采集器都是明文，TLS 才写 `https://`）。
未知的 `PI_TRACER` 值退化成 NoOp，同样**打 warning**（一个拼写错误过去等于
"完全没有追踪"，而且无声）。

**HTTP 请求级 span 是刻意不加的**：`/runs` 是 SSE 流式响应，中间件的 `call_next`
在响应头就绪时就返回了，那时包一个 span 会在流真正结束前就 end，测出来的时长是错的。
关联靠的是 `agent_runs.request_id` ↔ `X-Request-Id` ↔ 访问日志那一列（§11.5.1）。

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

### 10.4 `metrics.py` — Prometheus 指标与 `/metrics`

span 回答"这一次 run 发生了什么"，指标回答"现在是不是所有人都这样"。两者刻意分开：
轨迹按 run 存、可采样、留 30 天；指标是聚合的、永远开着，也是**告警唯一能挂的东西**
——Jaeger 里一屏健康的 run，说明不了有人根本起不了 run。

**采集点只有一个：`TraceRecorder`**。它是唯一看全了一次 run 所有事件、并且已经算好
各段时长的对象，指标因此不可能和它旁边的轨迹对不上。run 级汇总由 `RunManager` 在
写轨迹的同一处调 `run_finished`，两者共用同一个 `duration_ms`。

| 指标 | 类型 | 标签 |
|---|---|---|
| `pi_runs_total` | Counter | `status`, `model` |
| `pi_runs_in_flight` | Gauge | — |
| `pi_run_duration_seconds` | Histogram | `status` |
| `pi_run_turns` | Histogram | `model` |
| `pi_tokens_total` | Counter | `model`, `direction` |
| `pi_llm_calls_total` / `pi_llm_call_duration_seconds` | Counter / Histogram | `model`(+`ok`) |
| `pi_tool_calls_total` / `pi_tool_call_duration_seconds` | Counter / Histogram | `tool`(+`ok`) |
| `pi_memory_retrievals_total` | Counter | `outcome` |
| `pi_memory_retrieve_duration_seconds` / `pi_memory_facts_kept` | Histogram | — |
| `pi_memory_index_fallbacks_total` | Counter | `path` |
| `pi_trace_failures_total` | Counter | — |

几个刻意的取舍：

- **标签只有有界的维度**（model / tool / status / outcome）。绝不按用户名、会话 id、
  prompt 打标签——那是 `agent_runs` 的职责，而一个随用户数增长的标签会顺带拖垮指标后端。
- 两个 `model` 含义不同：`pi_runs_total` 的是这次 run **要的**模型（`PI_MODEL` /
  run 自带的），`pi_llm_calls_total` 的是**实际应答的** provider。配了
  `PI_FALLBACK_CHAIN` 时两者会不一样，而这个差值正是"静默降级"在仪表盘上的样子。
- token 只在 run 级计一次：逐轮相加会把第 2 轮起的重复历史重复计费。
- `pi_memory_index_fallbacks_total` 只在**没走成向量库**时递增（`breaker` /
  `index_failed` / `index_empty`）。走通是常态，一个每次 run 都跳的计数器没人看。
- `pi_runs_in_flight` 用 `async with metrics.in_flight()` 而非 started/finished 一对
  调用：run 抛异常的那条路径也必须把 gauge 降回来，而手动 dec 正是最容易被跳过的那行。
  只涨不跌的 gauge 会产出最有说服力的假饱和告警。
- `pi_trace_failures_total` 数的是轨迹写库失败——因为"轨迹绝不能弄失败一次 run"
  同时也意味着**没有别的地方会说它失败了**。

`prometheus-client` 属于 `observability` extra（`production` 已含）。没装时所有记录
方法都是 no-op、`render()` 返回 None，`/metrics` 因此答 **503 并带上原因**，而不是
一个空的 200。`PI_METRICS=0` 同样走这条路。

`/metrics` 用 `PI_METRICS_TOKEN` 保护：**设了**就要求 `Authorization: Bearer`，
token 不对答 **404 而不是 403**（"禁止访问"等于告诉扫描器这里有个端点）；**没设**
就是敞开的，启动时打一条 warning。序列全是聚合的，但流量量和在用模型本身就构成信息，
端口对外发布时应当设 token。端点 `include_in_schema=False`：它不是 web/ 生成
TypeScript 的那份契约，抓取方读的是 exposition 格式而不是 schema。

compose 里 Jaeger（16686 UI / 4317 OTLP）和 Prometheus（9090）都在
`observability` profile 下，默认不起；两个 UI 端口都只绑 **127.0.0.1**——它们都不鉴权，
而一条 trace 里带着 prompt、工具入参和工具输出。用 `ssh -L` 进去看，不要开端口。

---

## 11. 多用户服务 `src/pi/server/`

### 11.1 `config.py` — ServerSettings.from_env

所有服务端配置的集中读取点。关键项：`database_url`（PI_DATABASE_URL，**必填**——
为空直接抛 `RuntimeError`，服务拒绝启动，没有本地文件兜底）、`jwt_secret`
（PI_JWT_SECRET，缺省自动生成并持久化到
`~/.pi-py/jwt.secret`——**生产必须显式设置**）、`max_concurrent_runs`(8)、
`run_timeout_seconds`(600)、`rate_limit_runs_per_min`(20)、
`default_quota_tokens`(100万)、`redis_url`、`sandbox*`、`audit_path`、`policy_path`、
`web_dist`（PI_WEB_DIST，默认仓库内 `web/dist`；目录不存在**不算错误**，只是这台部署没有
前端，见 §18）、`forwarded_allow_ips`（PI_FORWARDED_ALLOW_IPS，默认 `127.0.0.1`——
**反向代理部署必须改**，理由见 §17 第 16 条）。

### 11.2 `auth.py` — 密码与令牌

- `hash_password / verify_password`：PBKDF2-HMAC-SHA256，20 万次迭代、随机 16 字节盐，
  存为 `pbkdf2$次数$盐$摘要` 自描述格式；验证用 `secrets.compare_digest` 防时序攻击。
  这两个是**同步阻塞**函数（约 50ms），异步化的责任在调用方：`app.py` 用
  `asyncio.to_thread` 包起来，新增路由别直接调（缘由与实测见 §17 第 15 条）。
- `create_token / decode_token`：JWT HS256，载荷含 `sub/iat/exp/jti`。
  **jti 是注销和踢人的钩子**（见 11.4）。

### 11.3 `app.py` — FastAPI 应用（路由全表）

| 端点 | 鉴权 | 响应模型 | 作用 |
|---|---|---|---|
| `GET /healthz` | 无 | `HealthOut` | 存活探针（Docker HEALTHCHECK 用） |
| `GET /readyz` | 无 | `ReadyOut`（200/503 同体） | 就绪探针：检查 DB 和缓存，任一异常 503 |
| `POST /v1/auth/register` | 无（刻意免鉴权） | `RegisterOut` | 开放注册，一律普通用户；重名 409 |
| `POST /v1/auth/login` | 无 | `LoginOut` | 验密 → 发 JWT |
| `POST /v1/auth/logout` | 用户 | `LogoutOut` | 把当前 token 的 jti 拉黑至过期 |
| `GET /v1/me` | 用户 | `MeOut` | 当前用户名 |
| `GET /v1/sessions` | 用户 | `SessionListOut` | 会话列表（`SessionSummary[]`，新→旧，上限 50；每项含当前 `plan`，见下） |
| `POST /v1/sessions` | 用户 | `SessionCreatedOut` | 创建会话（workspace = `workspace_root/用户名/`），回带 `cwd` |
| `GET /v1/sessions/{id}` | 用户 | `SessionSummary` | 会话详情（仅限本人，`_owned_session` 强制属主校验；含当前 `plan`） |
| `GET /v1/sessions/{id}/messages` | 用户 | `MessageListOut` | 消息历史（`blocks` 是 `pi.models.Block` 判别联合） |
| `POST /v1/sessions/{id}/runs` | 用户 | SSE（`Sse*Data`） | **核心**：提交 prompt，返回 SSE 流（限流→配额→RunManager） |
| `GET /v1/admin/users` | 管理员 | `AdminUserListOut` | 用户列表（上限 200） |
| `PATCH /v1/admin/users/{u}` | 管理员 | `UserUpdateOut` | 改配额 / 启停账号（禁用时顺带踢掉其所有在线 token；不能禁用自己）；`changed` 回报真正写了哪些字段 |
| `POST /v1/admin/users/{u}/revoke` | 管理员 | `RevokeOut` | 只踢令牌不禁账号 |
| `GET /v1/admin/audit` | 管理员 | `AuditOut`（**刻意不细分**） | 审计历史，**读 MySQL `audit_events`**（可按 user/tool/event 过滤，新→旧，上限 500；延迟一拍——见 §9.3 的后台 drainer） |
| `GET /v1/admin/traces` | 管理员 | `TraceListOut` | 执行轨迹列表：每次 `POST /runs` 一行 `agent_runs`（可按 user/session/status/anomaly 过滤，新→旧，上限 200；列表**不含 steps**，一页一条查询） |
| `GET /v1/admin/traces/{run_id}` | 管理员 | `TraceRunOut` | 单次 run 的完整步骤序列（tool_call/retrieval/llm_call/plan/compaction/error，含时长、完整入参与完整结果）+ `first_idx..last_idx` 的对话回放；404 = 没有或已被保留期删掉 |
| `GET /metrics` | 无（`PI_METRICS_TOKEN` 设了才要） | Prometheus 文本格式 0.0.4 | 聚合指标，见 §10.4。**不在 OpenAPI 文档里**（抓取方读 exposition 格式，web/ 也不该为它生成类型）；关掉或缺依赖答 503 |
| `GET /v1/usage` | 用户 | `UsageOut` | 本人当月用量（按模型）+ 配额余量 |
| `GET /v1/memories` | 用户 | `MemoryListOut` | 本人长期记忆列表（读 MySQL `user_memories`，上限 `limit`，默认 100） |
| `DELETE /v1/memories` | 用户 | `MemoryClearOut` | 清空本人长期记忆（MySQL 删行 + Milvus 按用户清向量），落审计 |

**依赖链**：`current_user`（解码 JWT → jti 黑名单 → 用户撤销纪元 → 查库确认激活）；
`require_admin` 在其上再加管理员校验。所有需要登录的路由都挂这两个依赖。

**`SessionSummary.plan`（Phase 1 任务规划）**：列表和详情**两个**端点都回传当前计划
——刷新页面要能看到和实时流画出的同一张计划卡。`null` 表示尚未跑过 `submit_plan`；
新计划覆盖旧计划（不可变历史在 transcript 里的 `submit_plan` 调用本身）。解析处
`_plan_of()` **没有损坏兜底**，理由同 `get_messages`：这一列只可能被
`Plan.model_dump_json()` 写过，解析失败是应当暴露的 bug，不是需要遮掩的状态。

路由表之外还有一个 **`app.mount("/", StaticFiles(web_dist, html=True))`**，在
`create_app` 的**最后一行**注册。它不在上表里，因为它不是路由而是 Mount，
而 Mount 在 `/` 上匹配**一切**路径——所以顺序就是它能不能用的全部原因：晚于所有路由
注册，`/v1/*`、`/healthz`、`/readyz`、`/openapi.json`、`/docs` 才不会被前端吞掉。
`TestWebUiMount` 钉住了这个顺序（含反例：未登录打 `/v1/me` 必须得到 401 JSON，
而不是 SPA 的 index.html）。详见 §18。

### 11.3.1 契约：OpenAPI 是唯一真相源

`web/` 的 TypeScript 类型全部由 `web/openapi.json` codegen 生成，不手写。生成方式：

```bash
python tools/dump_openapi.py     # 起一个用一次性 SQLite 的 app 实例，dump app.openapi()
```

脚本在 `import pi` **之前**钉死全部环境变量（原理同 `tests/conftest.py`，见 §12.2），
所以它读不到生产 `.env`，也碰不到真库；`web/openapi.json` 是**故意入库**的，这样契约
变更能在 diff 里被 review，codegen 也不需要先把服务跑起来。

几条必须知道的约定：

- **响应模型写在返回注解上**（`-> RegisterOut`），FastAPI 自动推成 `response_model`。
  副作用是好事：出站 body 会被校验，路由哪天开始返回意外形状，测试就红，不会等到
  浏览器里才发现。
- **错误体统一是 `{"detail": "..."}`**（`ErrorOut`），401/403/404/402/429/400/409 全部
  同形，所以前端只需要一条 `ApiError` 分支。422 是 FastAPI 自己的
  `HTTPValidationError`，503 是 `ReadyOut`——这两个是**有意的例外**，
  `TestOpenApiContract` 显式跳过它们。
- **SSE 契约没法用 `responses` 表达**：事件名走 `event:` 行、不在 JSON 里，OpenAPI
  没有"按事件名选一"的写法。做法是把九个 `Sse*Data` 模型做成 `anyOf` 挂在
  `text/event-stream` 上，再由 `custom_openapi()` 把它们的 `$defs` 手工并进
  `components.schemas`——因为 run 路由返回 `StreamingResponse`，FastAPI 根本发现不了
  这些模型，不并进去前端就得手写流式契约（恰恰是最不能写错的那份）。
- **模型是文档，所以有测试看着它**：`TestSseEventPayloads` 把 `runner.event_to_sse()`
  的每种事件都跑一遍并拿对应模型校验，反向也查（文档里有、服务端不发的 = 陈旧文档）。
  没有这组测试，`Sse*Data` 就会和 runner 各自演化。
- `run` 路由标了 `response_class=StreamingResponse`，否则 FastAPI 会顺手给 200 也写一个
  `application/json` 的空 schema，让文档谎报 body 类型。
- **`LoginOut.username` 是库里存的那个名字，不是请求里那个的回声。** MySQL 默认排序规则
  大小写不敏感，登录表单填 `Alice`、账号实际是 `alice` 也能登上；前端要显示身份，
  回声就会显示错。有了它，登录后不必再打一次 `/v1/me`。`TestAuth` 里有一例把两者
  和 `/v1/me` 的返回一起对齐。

**加响应模型时抓出来的真 bug**：`messages` 表的 `blocks` 列存的是**整条序列化的
`Message`**（`runner.py` 写 `m.model_dump_json()`、读历史时 `Message.model_validate_json()`，
两边自洽），但路由当年直接 `json.loads(列)` 塞进 `blocks` 字段，于是 HTTP 响应长这样：

```jsonc
// 旧：role 重复一次，真正的内容在 blocks.blocks 里
{"idx": 0, "role": "user", "blocks": {"role": "user", "blocks": [{"type":"text","text":"hi"}]}}
// 新：
{"idx": 0, "role": "user", "blocks": [{"type":"text","text":"hi"}]}
```

老测试只断言了 `role` 和条数，所以一直没发现；`blocks: list[Block]` 一上，形状不对就
直接 500，bug 才浮出来。`TestMessagesContract` 是它的回归测试。列名本身仍然误导
（叫 `blocks` 存的是 message），改列名要动迁移，留到 §17.23。

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
  text_delta / toolcall_start / toolcall_end / compaction / plan / turn_end / error。
  plan 分支**不做长度截断**——`Plan` 自身的字段边界已把帧压在 ~6 KB。
- **计划持久化的时机**：流中收到 `PlanEvent` 时只把 `ev.plan` 存进局部变量；
  `session_repo.set_plan` 在 `append_many` **之后**、绝不流中写。run 是全有或全无
  （原则 1）：messages 是记录本体，`sessions.plan` 只是它的缓存——先写计划会在
  落库失败时留下一行描述"从未落地的 transcript"的计划，计划面板将指向不存在的
  消息。排在这个位置还**免费继承超时语义**：超时的 run 只要 buffer 非空仍会把
  消息和计划一起冲刷落库。
- run 超时（`PI_RUN_TIMEOUT_SECONDS`）用 `asyncio.timeout` 包住整个循环，超了发
  ErrorEvent 但**已产生的消息仍会持久化**（buffer 非空就写）——这是刻意的，
  部分结果比全丢有用。

### 11.5.1 执行轨迹留痕：`agent_runs` / `agent_steps`

“所有交互都在 MySQL 留痕”的第三张拼图：每次 `POST /v1/sessions/{id}/runs`
对应 `agent_runs` 一行（run_id 是 `uuid4().hex[:12]`，**不回传给客户端**——它是
内部观测键，不是 API 概念），工具调用/**记忆召回**/**模型调用**/计划/压缩/错误对应
`agent_steps` 若干行。
**text_delta 永不入库**——那是 messages 的职责，轨迹只记“做了什么”。

`kind` 是无约束的 `String(16)`，所以新增 `retrieval` / `llm_call` 两种**不需要迁移**；
`args`/`detail` 是 TEXT（上限 20 万字符），召回明细直接以 JSON 落在 `detail` 里。

- **`TraceRecorder`**（`runner.py`）：run 期间在内存里攒一份轨迹。`observe(ev)` 是
  AgentEvent→步骤的唯一翻译点：ToolCallStart 记单调时钟起点，ToolCallEnd 按调用 id
  配对算时长；RetrievalEvent 落一条 `retrieval`；LlmCallEvent 每轮落一条 `llm_call`；
  错误事件定状态（“run timed out” 前缀 → `timeout`，否则 `error`）；
  TurnEnd 收 usage/turns。**流结束时一次性写入**（`AgentRunRepo.append`，run 行和
  steps 一个事务）——和 usage 行同生命周期：先于它也只多一个“轨迹描述了不存在的
  transcript”的孤儿风险。
- **两类事件只进轨迹、不上 SSE**：`TRACE_ONLY_EVENTS = (RetrievalEvent, LlmCallEvent)`，
  run 端点直接跳过它们。两者都没有 `Sse*Data` 模型，放过去就等于每次 run 都往线上发一帧
  `event: unknown`——而 web/ 的 `KNOWN_EVENTS` 是个闭集，正是为了让未记录的帧不能永远被
  无声忽略。
- **`retrieval` 步骤是"模型为什么不记得"的唯一物证**。注入的记忆**从不进 messages**
  （loop.py 刻意让它只存在于出站副本里，不落库、不进压缩摘要、不计入 estimate_size），
  所以 `first_idx..last_idx` 的对话回放里根本看不到它。这一行的 `args` 是查询原文，
  `detail` 是 `MemoryService.retrieve_traced` 的整份账：
  `outcome`（injected / no_hits / gated_out / rerank_empty / embed_failed /
  recall_failed / join_failed / disabled / empty_query / raised）、`index`（**召回到底
  走没走成向量库**：index / index_empty / index_failed / breaker——后三者是 MySQL 全表
  暴力余弦降级，结果集不同）、每个候选的 `cosine` 与 `rerank` 分数及 `verdict`
  （kept / below_cosine_gate / below_rerank_gate / over_top_k / absent_in_repo /
  empty_text / recalled）、当时的阈值（`min_similarity` / `rerank_min_score` /
  `top_k` / `recall_k`）、各阶段毫秒（`stages_ms`）、以及**实际注入的文本**。
  没有它，"没存进去 / 没召回 / 被阈值筛掉 / rerank 丢的 / embedding 挂了"这五种情况
  在 run 内部长得一模一样，都是一个空字符串。
- **`llm_call` 步骤是每轮一行**：模型名、stop_reason、本轮 token、时长，失败的那轮也记
  （loop 在 `except` 里先 yield 再 re-raise——run 级的 ErrorEvent 只说"这次 run 断了"，
  这一行才说"断在第几轮"）。轨迹里因此能一眼分开"慢的一次调用"和"循环的五次调用"：
  run 级的总时长和总 token 对这两者是一样的。
- **只在记忆真被配置过时才召回**：`_retrieve_for` 判的是 `MemoryService.configured`
  （给了 store 和 embedder），不是 `enabled`。没配向量库的安装不会每次 run 都写一条
  "disabled" 步骤；而**配了却没起来**的仍然会写——那恰恰是最需要被解释的 run。
- **异常标志（flags）**：`trace_flags()` 四个判定——`status != ok`（timeout/error）、
  `turns <= 0`（empty：用户付了钱模型什么都没答）、`failed_tools >= 3`（tool_storm：
  循环或环境坏了）、`memory_failed`（召回的某个阶段抛了异常）。逗号拼进一列，管理端
  `anomaly=true` 过滤的就是它。`memory_failed` **刻意只标记"坏了"，不标记"没召回到"**：
  对还没有事实的用户，空召回就是正确答案，把它也标记上会淹掉真正出问题的那些 run。
- **保留期**：`PI_TRACE_RETENTION_DAYS`（默认 30，0=永久）。`_maybe_retain_traces()`
  在 run 收尾时顺带触发，**每小时最多一次**（进程内节流），`delete_older_than` 先删
  steps 再删 runs（外键顺序）。轨迹是运维数据不是账单，30 天足够排障。
- **失败静默**：轨迹写入失败只记日志，绝不回滚对话/计量（同原则 4）——但会递增
  `pi_trace_failures_total`（§10.4），否则"绝不弄失败一次 run"同时也意味着没有别的
  地方会说它失败了。
- **端点**：`GET /v1/admin/traces`（列表，不含 steps）和
  `GET /v1/admin/traces/{run_id}`（含 steps 的完整视图）；都要求管理员。
- **注销级联**：`purge_user` 一并擦掉（steps 先于 runs）。
- **已知坑**：`AgentRunRepo.append` 的 id 必须在 flush 后、commit 前捕获
  （见 §11.6 的 MissingGreenlet 说明——这条是修过的真 bug：每个带轨迹的 run 都
  先提交成功、再在 `return run.id` 上炸掉，错误被 except 吞掉只剩一行日志）。

### 11.6 `db.py` — ORM 与仓储

九张表（SQLAlchemy 2.0 Mapped 风格）：`users` / `sessions` / `messages` /
`usage_records` + 留痕三件套 `user_memories` / `audit_events` / `agent_runs`+`agent_steps`。
后四者是"所有交互都在 MySQL 留痕"的落地：记忆（§11.9）、审计（§9.3）、
执行轨迹（§11.5.1）。

- `engine_kwargs(url)`：MySQL 方言追加 `charset=utf8mb4`（防实例默认 latin1 乱码）
  + `pool_recycle=280`（低于常见 wait_timeout，池内连接永不失效）。
  应用启动和 alembic 共用此函数，保证两边引擎配置一致。
- `Database`：异步引擎 + `pool_pre_ping=True`（生产保留：防 RDS HA 切换后的死连接）。
- 仓储（`UserRepo/SessionRepo/MessageRepo/UserMemoryRepo/AuditEventRepo/AgentRunRepo`）：
  每个方法独立 `AsyncSession`，写操作各自提交。这种"每方法一会话"的写法简单但有
  隐藏开销（每次隐式 BEGIN/ROLLBACK 一个往返）——公网部署时是延迟大头之一；同 VPC
  部署后 <1ms，不值得优化。若将来要做请求级会话共享，从这些仓储入手。
- `UserRepo` 有 `set_active / set_quota / set_admin` 三个改属性的方法，但只有前两个
  挂着管理员路由；`set_admin` **刻意不暴露**（见 §11.3 的权限模型）。
- **`AgentRunRepo.append` 的 id 要在 flush 后、commit 前捕获**：裸 `AsyncSession`
  默认 `expire_on_commit=True`，commit 之后碰 `run.id` 会触发 lazy IO，在
  greenlet 上下文之外直接炸 `MissingGreenlet`。`AgentRunRepo.append` 把它存进局部
  变量再返回（`SessionRepo.create` 用的是另一个惯用法：`await s.refresh(row)`）。
- `purge_user(uid)`：注销级联删除，**单事务**按外键顺序擦全部七张表
  （user_memories → audit_events（按 actor=username）→ agent_steps → agent_runs →
  messages → sessions → usage_records → users），返回各表删除数的回执。这是
  `DELETE /v1/me` 的核心——密码重确认通过才执行，返回的 counts 就是"擦干净了"的
  证据。

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

### 11.9 长期记忆层 `src/pi/memory/`（MySQL 真相源 + Milvus 索引）

Mem0/Zep 风格的抽取式事实库：run 结束后把会话里的持久事实（约定、偏好、环境）
抽出来**先存 MySQL `user_memories`**，再镜像进 Milvus；下一轮 run 前检索相关事实
注入上下文。**MySQL 是唯一真相源**——每行带 packed float32 embedding blob（1024 维
= 4KB/行）和 `milvus_synced` 回执；Milvus 是**可丢弃、可重建的纯索引**（确定性
主键 = MySQL 行 id，upsert 幂等），`tools/rebuild_milvus.py` 一条命令从 MySQL
零 API 调用重建。层位置：在 `pi.agent` **之下**（智能体核心只拿到一个裸回调）、
与 `pi.tools` 并列；只有 `server/runner.py` 和 `server/app.py` import 它（单向依赖
规则不破）。默认关闭——`PI_MILVUS_URI` 和 `PI_EMBEDDING_MODEL` 缺一个就降级为
NoOp，服务照常起（增强类子系统失败降级不失败，§设计原则 4）。

七个文件各管一段：

| 文件 | 职责 |
|---|---|
| `repo.py` | `MemoryRepo` 协议 + `DictMemoryRepo`（测试/无库进程内实现）。`user_memories` 的实现住在 `server/db.py` 的 `UserMemoryRepo`（要用服务的引擎）。`pack_embedding`：float32 小端 |
| `store.py` | `VectorStore` 协议 + `NoOpStore`/`InMemoryStore`/`MilvusStore`。Milvus：分区键 `user_id`（1024 桶，**租户隔离的唯一保证**——所有查询必须走 `_user_filter()`，`int()` 强转后拼接，绝不内联表达式）；AUTOINDEX+COSINE；**确定性主键 = MySQL 行 id，upsert/delete**（auto_id 的老 collection 会被启动检查直接拒绝，提示 drop + rebuild）；一致性 Bounded。`drop()` 只给重建工具用 |
| `extract.py` | 抽取提示词 + JSON 解析（永不 raise）+ `transcript_chars`（skip-short-turns 护栏：< `PI_MEMORY_EXTRACT_MIN_CHARS`(400) 的短对话不值得花钱抽） |
| `embed.py` | `OpenAIEmbedder`（复用 OPENAI_* 凭据，`dimensions` 参数做 MRL 截断）+ `HashEmbedder`（测试/无 key 用，token 重叠驱动 cosine） |
| `rerank.py` | `DashScopeReranker`（qwen3.7-text-rerank 真端点）；不可用/失败→退回 cosine 排序 |
| `service.py` | `MemoryService`：写路径（去重→touch→满额逐出最久未确认）、读路径（索引→repo join→rerank）、`drain()`、维护编排（sync_pending/decay）、metering、仲裁编排 |
| `arbitrate.py` | 仲裁提示词 + `parse_merges`（永不 raise）+ `arbitrate()` 流式调用 |

**写路径**（run 结束后由 `runner.py` 触发，**后台不 await**——用户已经拿到答案，
阻塞会白占会话锁和全局信号量）：新事实先过 `redact_text`（秘密不出进程），
embed 后按 cosine 找重复：≥0.92 视为同一条 → **touch**（repo 一条 UPDATE 换向量、
`last_seen_at` 前进、标记未同步——id 稳定，不再是删旧插新）；满 100 条 → 逐出
`last_seen_at` 最旧者。**写序：repo 先落，索引 upsert 随后 best-effort**——镜像
失败只把行留在 `pending_sync()`，维护循环重试，用户数据永远不因 Milvus 抖动丢。
每次抽取的模型花费按 `turns=0` 记进 `usage_records`（`turns=0` = "不是一次 run，
是记忆开销"；用户关闭记忆则一行都不记）。两处刻意的护栏：抽取失败只记 warning；
注入的指令文本是**数据不是命令**（提示词里写明，且有测试钉住）。

**读路径**（每轮 run 前）：查询 embed → **索引搜（Bounded）→ repo join 过滤僵尸
（索引滞后于 MySQL 时，join 剔除已删/已衰减的行）**；索引不可用（熔断开路）或
**零命中**时退化成 repo 全扫暴力 cosine（每用户上限 100 条向量，代价可控——零
命中必须回 MySQL：实测 Bounded 一致性下刚 upsert 的向量 ~0.5s 内搜不到，而 repo
join 只能过滤索引给的 id、救不回索引看不见的，没有这条回退，用户追问最快的那一拍
恰好读到"昨天"的记忆）→ cosine 0.35 召回下限（实测阈值：降到 0 会把离题查询也
送进 rerank，升到 0.5 会丢相关事实）→ rerank 精排（0.3 分值下限）→ 取前 5 条注入。
全程任何失败都返回空串——检索失败不能 fail 用户的回合。embedding/rerank 花费
同样 `turns=0` 入账。

这四个阶段各自有 span（`memory.embed` / `memory.recall` / `memory.join` /
`memory.rerank`，挂在 `memory.retrieve` 下，见 §10.1），整份账——走了哪条召回路径、
每个候选的分数与被判掉的理由、当时的阈值、各阶段耗时、实际注入的文本——由
`retrieve_traced` 返回并落成一条 `retrieval` 步骤（见 §11.5.1）。**"返回空串"因此
不再是终点**：空召回和召回炸了，在轨迹里是两个不同的 `outcome`。

**仲裁（定时矛盾梳理）**：写路径只看得见本次 run 的新事实，去重只认 cosine ≥0.92，
而网关的模型漂移会产出 0.58–0.92 的"语义近重复"（实测 qwen-plus 连续两次输出
3/5 才逐字节相同；temperature=0 无效，是网关整体行为），矛盾/近重复只有拿着
**该用户的全量事实**才看得出来——所以做成独立的后台循环而不是塞进写路径：

- `_memory_arbiter_loop`（`app.py` lifespan 里起，配了 `PI_MEMORY_ARBITER_MODEL`
  才存在；sleep-first，无启动风暴；关停随 lifespan cancel）
- 每 `PI_MEMORY_ARBITER_INTERVAL_SECONDS`(900s) 一扫：Redis 锁
  `memory:arbiter`（TTL=间隔，多副本只有一个在扫）→ 拿 `usage_records` 当变更日志
  （`turns=0 AND model=<抽取模型> AND created_at >= checkpoint` = 脏用户，
  `PI_MEMORY_ARBITER_BATCH`(20) 个一批）→ 逐用户取全量事实送 qwen-plus
  → 应用合并动作 → checkpoint 存 Redis（86400s TTL）。**检查点边界是 `>=`，
  同秒的行会重复扫**：宁可重复不可漏掉，且扫本身只花一次锁内的钱
- 仲裁自己按 `session_id=arbiter`、`turns=0` 计费 → 用的是仲裁模型名，脏用户查询
  按抽取模型名过滤 → **一扫永远不会把自己标脏**，无自触发循环
- 合并语义刻意保守：只允许 `merge` 动作（删一组、插合并后的一条），不允许丢信息；
  合并文本以新说法为准；没列出的 id 一律不动；`拿不准就不合并`；一组 2–4 个 id；
  单用户单扫最多 5 个合并；单个合并失败（并发写走了成员）不中断整批
- 合并结果：`created_at` 继承组内最旧（出处不丢）、`source_session=arbiter`、
  文本先 `redact_text` 再入库

**维护循环**（`_memory_maintenance_loop`，`app.py` lifespan 起，memory 开着就跑，
降级模式也跑——那正是它存在的意义）：每 `PI_MEMORY_MAINTENANCE_INTERVAL_SECONDS`
（900s）一扫，Redis 锁 `memory:maintenance` 保证多副本只有一个在扫：

1. `ensure_index`：collection 不在就建（索引整个被 drop 过的情形）；
2. `sync_pending`：`pending_sync()` 的行重放 upsert，成功 `mark_synced`——Milvus
   抖动期间落库的行在这里追平；
3. `decay`：`PI_MEMORY_DECAY_DAYS`（默认 90，0=关）——`last_seen_at` 早于截止的
   行 `deactivate_older_than` 软删（行还在 MySQL，只是不再召回），向量按用户清。
   软删不是硬删：90 天没被重申的事实先失去效力，账号注销时才物理删除。

仲裁循环独立于维护循环（自己的锁、自己的 checkpoint），但同在 lifespan 里。

**`tools/rebuild_milvus.py`（全量重建工具）**：`python tools/rebuild_milvus.py --yes`。
三个拒绝护栏：生产命名空间 `pi` 不带 `--yes` 拒跑；MySQL 一条活跃事实都没有时
拒跑（清空生产索引必须有明确的先行动作）；`PI_DATABASE_URL`/`PI_MILVUS_URI`
缺一拒跑。流程：drop → setup → 按 `active_embedding_page`（id > after_id 键集分页，
不 OFFSET）遍历 MySQL → 按用户分组批量 upsert（向量直接取行内 blob，**零 API
调用**）→ 恰好把 upsert 过的 id `mark_synced`。表结构坏了、索引膨胀了、换了
embedding 模型重算后，都是同一条命令。

实测成本（阿里 MaaS 网关，2026-09）：抽取 qwen-flash 每 run ~2400 in / ~150 out、
~1.5s；仲裁 qwen-plus 2 条事实 401 in / 52 out / 1.5s，按满额 100 条折算约
5–8k in / 一两百 out，一扫 20 个用户几毛钱量级、分钟级时长——所以批量和间隔是
节流阀。模型选择上有个反直觉的坑：qwen3.5/3.6/3.7-*flash 是思考模型，推理在
`reasoning_content` 里（OpenAIProvider 读不到）但按 completion_tokens 计费，
抽取一次 13–32s / 1900–2600 out tokens；**qwen-flash 才是非思考的快模型**。

路由面：`GET /v1/memories`（列出，读 MySQL）、`DELETE /v1/memories`（清空本人：
MySQL 删行 + Milvus 按用户清向量）。两者都落审计。

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

包导入时按顺序找 `.pi-py.env` / `.env` / `~/.pi-py/.env`，读 `KEY=VALUE` 行注入
环境变量（**已存在的环境变量优先**，即显式设置永远赢）。找到第一个存在的文件就停。
值两侧的引号会被剥掉；空行与 `#` 开头的行忽略。

**这条机制有个容易踩的后果**：仓库根放着生产 `.env` 时，**任何 import pi 的进程
都会继承它**——包括 `pytest`。测试自己会 monkeypatch `PI_DATABASE_URL`，但历史上
没有任何测试设置 `PI_REDIS_URL`，于是测试会把锁 / 限流 / 撤销键写进生产 Redis
（只是命名空间被随机化成 `test-xxxx`，不与 `prod:*` 冲突，但确实在污染生产实例），
`PI_TRACER=jsonl` 也会让测试往 `~/.pi-py/` 写 trace 文件。

因此 `tests/conftest.py` 在 **import pi 之前**把这些变量钉死：

```python
os.environ["PI_REDIS_URL"] = ""   # → MemoryBackend，绝不连真 Redis
os.environ["PI_SANDBOX"]   = ""   # → LocalRunner，绝不起真容器
os.environ["PI_POLICY"]    = ""
os.environ["PI_TRACER"]    = "noop"
os.environ["PI_MILVUS_URI"] = ""  # → NoOpStore，绝不连真向量库
os.environ["PI_EMBEDDING_MODEL"] = ""
os.environ["PI_RERANK_URL"] = ""
os.environ["PI_MEMORY_MODEL"] = ""
os.environ["PI_MEMORY_ARBITER_MODEL"] = ""
os.environ["PI_WEB_DIST"] = "/nonexistent-pi-web-dist"
os.environ["PI_METRICS_TOKEN"] = ""   # 见下面这段教训
os.environ["PI_METRICS"] = "1"
```

因为"已存在的环境变量优先"，这几行赋值就让 `.env` 里的对应项失效。

⚠️ **这份清单是"承重墙"，而且已经漏过一次**：`PI_METRICS_TOKEN` 原先不在其中。
漏着的时候看不出来，因为 `.env` 里那个键恰好是**空值**（本身是 L12 那个 bug）——
空值漏进测试与钉成空串效果相同。等 L12 修好、`.env` 里只剩末尾那份真 token，
它立刻漏进套件：`/metrics` 对不带 token 的请求改答 **404**（这是设计，见 §17），
于是 `TestMetricsEndpoint` 里两个"假设端点敞开"的用例从 200/503 翻成 404。
**教训：往 `.env` 加任何 `PI_*` 时，都要问一句"离线套件的行为依赖它吗"，
依赖就必须在 conftest 里钉住。** 完整记录见 `deploy/environments.md` **L14**。
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
它**不会**被 `pi/__init__.py` 自动加载（加载器只认 `.pi-py.env` / `.env` /
`~/.pi-py/.env`），必须显式 source——这正是想要的：忘了 source 就还在生产配置上，
而种子脚本的护栏会因此直接拒绝运行。文件含密码，权限 600，已进 `.gitignore`。

种子数据（密码默认 `pi-test-123`，`--password` 可改）：

| 账号 | 状态 | 用来验什么 |
|---|---|---|
| `admin` | `is_admin=1`；**密码已于 2026-09-06 轮换，不再是默认值** | 管理员端点；提权走 `UserRepo.set_admin`，就是"改库"那条路 |
| `alice` / `bob` / `carol` | 普通用户，各 2 个会话（一个 4 条消息、一个空） | 正常链路、跨用户 404、utf8mb4（消息里带中文和 4 字节 emoji） |
| `overquota` | 配额 1000、当月已用 1540 | `POST /runs` 立刻 402 |
| `disabled` | `is_active=0` | 登录 401 |

> 重跑 `seed_testdb.py` **不会**把 `admin` 的密码冲回默认值：脚本先 `by_username()`，
> 只在返回 `None` 时才 `create()`，对已存在账号只做"必要时提权"，从不改写 `password_hash`。
> 轮换后的值刻意不写进仓库（有公开远端）。另注：**产品里没有任何改密码的接口** ——
> `UserRepo` 只有 `set_active`/`set_quota`/`set_admin`，没有 `set_password`，路由侧也没有，
> 所以轮换只能直接 `UPDATE users SET password_hash=…`（哈希用 `pi.server.auth.hash_password()`）；
> 且改密码**不会**让已签发 token 失效（`current_user()` 全程不比对密码），要踢人得调
> `POST /v1/admin/users/{u}/revoke` 抬 epoch。见 `deploy/environments.md` **L5**/**L13**。

护栏：脚本开头检查 `PI_DATABASE_URL` 的库名，不以 `_test` 结尾就拒绝退出，
免得对着生产库灌出一堆账号。另外用量记录写的是**当天**日期，而 `/v1/usage` 和
配额检查只统计当月——跨月之后要重灌一次才有配额数据。

**默认的 pytest 不用这个库**：单测仍然一律用一次性 SQLite（`conftest.py` 钉死环境变量），
测试库只服务手工验证和压测。这条边界别混——pytest 一旦连上常驻库，测试之间就会
互相看见数据，"146 passed" 也就不再可复现了。

唯一的例外是 `integration/` 目录（见 §15 末尾）：它必须显式
`pytest integration/` 才会跑，目录自带 conftest 会 source `.env` 再 source
`.env.test`，并带两道护栏（库名不以 `_test` 结尾、Milvus 命名空间为 `pi` 都直接
拒绝启动），所以误连生产只有一种可能——有人改了护栏本身。

---

## 13. 配置速查表

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PI_MODEL` | `openai/gpt-4o` | 默认模型（`provider/model` 格式） |
| `PI_FALLBACK_CHAIN` | 空 | 逗号分隔降级链 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | — | OpenAI 兼容端点凭据 |
| `ANTHROPIC_API_KEY` | — | Anthropic 凭据 |
| `PI_DATABASE_URL` | **无（必填）** | `mysql+aiomysql://...` 或 `postgresql+asyncpg://...`；缺失则服务拒绝启动 |
| `PI_REDIS_URL` / `PI_REDIS_NS` | 空 / `pi` | Redis（锁/限流/撤销）；空=进程内退化 |
| `PI_JWT_SECRET` | 自动生成 | **生产必须显式设置**；改了=所有已发 token 作废 |
| `PI_TOKEN_TTL_MIN` | 720 | JWT 寿命（分钟） |
| `PI_MAX_CONCURRENT_RUNS` | 8 | 全局并发 run 上限（信号量） |
| `PI_RUN_TIMEOUT_SECONDS` | 600 | 单次 run 超时 |
| `PI_RATE_LIMIT_RUNS_PER_MIN` | 20 | 每用户每分钟 run 上限 |
| `PI_DEFAULT_QUOTA_TOKENS` | 1,000,000 | 新用户默认月配额 |
| `PI_TRACER` | jsonl | noop / jsonl / otel（**回落会打 warning**，不再静默；未知值=noop+warning） |
| `PI_OTLP_ENDPOINT` | 空 | `PI_TRACER=otel` 的 OTLP/gRPC 采集器。空则依次读 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT`，最后落 `http://localhost:4317`；`http://` 前缀=明文 gRPC |
| `PI_TRACE_SAMPLE_RATE` | 1.0 | 采样比例，`ParentBased` 决定在根上做一次（整棵树同进同出）；自托管采集器建议保持 1.0 |
| `PI_SERVICE_NAME` | pi-py | span 的 `service.name` |
| `PI_ENVIRONMENT` | 空 | span 的 `deployment.environment`。多套部署共用一个采集器时必须设，否则"这条 trace 是哪套环境的"无从判断 |
| `PI_METRICS` | 1 | `/metrics` 开关；关掉（或缺 `prometheus-client`）答 503 并说明原因，不是空的 200 |
| `PI_METRICS_TOKEN` | 空 | `/metrics` 的 Bearer token。设了就要求，token 不对答 **404**（不是 403）；没设=敞开，启动打 warning。端口对外发布时应当设 |
| `PI_POLICY` | 空 | 策略 JSON 文件路径；**只能加规则**，`path_sandbox`/`redact` 被 `server_policy` 强制打开 |
| `PI_SANDBOX` | 空 | 空/`local`=本机执行（启动时打 `warning`）；`docker`=容器隔离；**其它值启动即报错**，不再静默降级 |
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
| `PI_WEB_DIST` | 仓库内 `web/dist` | 前端构建产物目录，由 app 进程自己挂在 `/`；目录不存在=这台部署没有 UI，不报错（见 §18） |
| `PI_ALEMBIC_DIR` | 仓库根 | 容器内迁移脚本位置（Dockerfile 已设） |
| `PI_MILVUS_URI` / `PI_MILVUS_TOKEN` / `PI_MILVUS_NS` | 空 / 空 / `pi` | Milvus（记忆向量库）；URI 空 = 记忆整体关闭（NoOp 降级）。命名空间决定集合名 `{ns}_memories`，**测试必须换 ns** |
| `PI_EMBEDDING_MODEL` / `PI_EMBEDDING_DIM` | 空 / 512 | OpenAI 兼容 embedding 模型；dim=0 表示用模型原生宽度（服务启动时探测后建集合，永不 schema/向量不一致）。也是记忆开关之一 |
| `PI_RERANK_URL` / `PI_RERANK_MODEL` | 空 | rerank 端点（两段式检索第二段）；空 = 纯 cosine 排序，功能可用精度略降 |
| `PI_MEMORY_MODEL` | 空 | 抽取模型（每 run 一次，选**非思考**的快模型；空 = 不抽取，记忆只读） |
| `PI_MEMORY_TOP_K` / `PI_MEMORY_RECALL_K` | 5 / 20 | 注入条数 / cosine 召回条数 |
| `PI_MEMORY_MIN_SIMILARITY` / `PI_MEMORY_DEDUP_SIMILARITY` | 0.35 / 0.92 | cosine 召回下限 / 去重上限（均实测阈值，见 §11.9） |
| `PI_MEMORY_MAX_FACTS` | 100 | 每用户事实上限（满额逐出最久未确认） |
| `PI_MEMORY_EXTRACT_MIN_CHARS` | 400 | 短对话跳过抽取的字符护栏 |
| `PI_MEMORY_EXTRACT_CONCURRENCY` | 4 | 后台抽取并发 |
| `PI_MEMORY_INJECT_MAX_CHARS` | 2000 | 注入上下文的字符预算 |
| `PI_MEMORY_RERANK_MIN_SCORE` | 0.3 | rerank 分值下限（rerank 自己的分值域，与 cosine 不可比） |
| `PI_MEMORY_ARBITER_MODEL` | 空 | 仲裁模型（矛盾/近重复定期合并，选判断力强的；空 = 仲裁关闭） |
| `PI_MEMORY_ARBITER_INTERVAL_SECONDS` / `PI_MEMORY_ARBITER_BATCH` | 900 / 20 | 仲裁扫描间隔 / 单扫脏用户上限（节流阀，成本见 §11.9） |
| `PI_MEMORY_MAINTENANCE_INTERVAL_SECONDS` | 900 | 维护循环间隔（ensure_index → sync_pending → decay，Redis 锁单副本执行） |
| `PI_MEMORY_DECAY_DAYS` | 90 | 事实衰减：`last_seen_at` 超过这么多天未被重申 → 软删（行留 MySQL、不再召回）；0 = 关 |
| `PI_TRACE_RETENTION_DAYS` | 30 | 执行轨迹保留期（`agent_runs`/`agent_steps`，小时级惰性清理，见 §11.5.1）；0 = 永久 |

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
- `docker-compose.yml`：本地全栈（app + Postgres + Redis，健康检查门控）。
- `docker-compose.cloud.yml`：云变体——不含数据库容器，指向火山引擎托管
  MySQL/Redis 的**内网**域名；密钥全部来自 `.env`（`deploy/env.cloud.example` 模板）；
  可选 `--profile tls` 加 Caddy（`deploy/Caddyfile.cloud`，`{$DOMAIN}` 注入，
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
python -m pytest -q     # 363 passed, 1 skipped —— 全本地，不需要网络/数据库/模型
                        # ✅ 不再 flaky（曾约 6/10 次红在 TestDeregister），见下面

export PATH=/usr/local/node/bin:$PATH        # node 不在默认 PATH 里
cd web && npm test      # 56 passed —— SSE 分帧、两个 transcript reducer、SSR 渲染断言
cd web && npm run typecheck   # vue-tsc --noEmit
```

两条都是 2026-09-06 的实测基线（Python 25.3s / 前端 1.1s）。**别把数字当验收标准**，
它们会随开发漂移 —— 本文档里就曾经同时存在 174 / 291 / 332 / 333 四个互相矛盾的总数。

✅ **Python 套件的 flaky 已修掉**：2026-09-06 曾连续 10 次全套件运行（当时总数 348 例，
现在 364），4 次全绿、6 次 `1 failed`，失败永远是
`test_server.py::TestDeregister::test_the_cascade_wipes_every_trace_and_the_token`。
根因是该用例的 `_counts()` 快照没有等在飞的**后台记忆抽取**收尾，而抽取无论成功失败都会写一条
`turns=0` 的 `usage_records`（`memory/service.py:732`），落在快照之后就让 `purged` 比 `before` 多 1。

修法：新增 `_wait_usage_settled()`，照同类里已有的 `_wait_audit_flushed` 同一模式轮询数据库，
等计数**连续 5 次不变**（静默）而不是等某个固定行数——固定值会把这个用例耦合到 fake provider
恰好产出几条抽取上，等于把契约测试变成了对夹具的断言。实测**单独跑 30/30 全过**
（修前 4/20 失败），全套件连跑 5 次均 `363 passed, 1 skipped`。**只改了测试，没碰产品代码。**

⚠️ **但修的是测试，不是产品。** 生产语义上的竞态仍然开着：用户点注销的那一刻若有抽取在飞，
清库可能漏掉那条 `usage_records`，回执也会错报一行。对"可审计的数据删除"这个承诺来说是
实打实的缺口，正解是**注销与在飞抽取互斥**（或等它们收尾），与 Run 地基 / append-only
事件日志那条线一并考虑。完整分析见 **`deploy/environments.md` L11**。

测试组织（离线套件都在 `tests/`，共 364 例；前端另有一套 Node 侧的，见 §18.5；
花真钱的 `integration/` 另算，见本节末尾）：

| 文件 | 例数 | 覆盖 |
|---|---|---|
| `test_memory.py` | 125 | 长期记忆全链路（离线，`HashEmbedder`+`InMemoryStore`+桩 Milvus）：`_user_filter` 租户隔离与查询形状、Milvus DDL/方言兼容、抽取 JSON 解析（缺字段/截断/注入指令是数据不是命令）、写路径核心语义（**repo 先落、索引镜像 best-effort、pending_sync 追平**；touch 确认而非重插、复活禁止、最旧不被逐出、满额逐出最久未确认）、**维护循环**（ensure_index→sync_pending→decay 的顺序与幂等、Redis 锁不持有就不扫）、**衰减**（软删行留 MySQL、索引向量按用户清）、metering（`turns=0` 计入 `usage_records`、失败也记账、关闭即不记）、**仲裁**（合并保最旧 `created_at`、秘密先脱敏再入库、单组合并不失败整批、未列出的不动、无模型即关闭）、两段式检索（索引→repo join→rerank；**索引滞后/零命中回退 MySQL 全扫**；熔断开路回退；cosine 召回 + rerank 精排 + 阈值降级到纯 cosine）、rerank 端点真 HTTP、检索/抽取失败不炸主流程、后台抽取不阻塞 run |
| `test_sandbox_pool.py` | 41 | 预热池（假传输，无需真 docker）：复用/预热/并发去重/回收/重建/驱逐/关闭；容器资源限额（`_parse_size`、`SandboxLimits` 校验与两种渲染、CLI/Engine API 两条建容器路径都真的带上了限额）；`PI_SANDBOX` 非法值必须报错而不是静默降级 |
| `test_server.py` | 83 | 全 HTTP API：开放注册（含并发重名）、登录、会话、run SSE、跨用户隔离、限流（`TestClient` 进程内驱动 + 临时 SQLite）；认证事件审计（每个出口都落一条、不落密码）与 `X-Forwarded-For` 取真实 IP（可信 CIDR / 默认只信本机 / 伪造前缀 / `*` 反例，见 §17.16）；**契约回归**：`event_to_sse` 的每种事件都对得上文档里的 `Sse*Data` 模型（正反两向 + 真流端到端）、OpenAPI 里没有未定型的响应体、错误体统一 `ErrorOut`、`messages.blocks` 是扁平数组（见 §11.3.1）、工具结果预览的截断长度与文档里引用的 `PREVIEW_LEN` 常量一致；`LoginOut.username` 与 `/v1/me` 报同一个人；**`TestWebUiMount`** 钉住前端挂载顺序（见 §11.3 / §18）；**`TestPlanRun`** 走真 HTTP+SSE 路径打 `submit_plan`（线序 + 持久化回读）；**`TestMemoryWiring`/`TestArbiterSweep`/`TestMaintenanceSweep`**：配置真到达 `MemoryService`、脏用户查询、Redis 锁、lifespan 后台循环；**`TestDeregister`**：注销级联（七张表回执、token 即死、向量镜像只剩别人、workspace 删除、再注册白纸）；**`TestAuditMysql`**：每条审计落 MySQL、管理端读表不读文件（删掉 JSONL 也能查）；**`TestTraces`/`TestTraceFlags`**：轨迹逐步记录（含每轮 `llm_call` 与失败的那一轮）、**`retrieval` 步骤记下召回明细**、**召回阶段抛异常时 run 仍成功但被打上 `memory_failed` 且能被 `anomaly=true` 捞出**、失败 run 可过滤、鉴权门、保留期删除、四个异常判定；**`TestMetricsEndpoint`**：run 完成后序列真的动了、`in_flight` 归零、序列里不含用户名与会话 id、`/metrics` 不在 OpenAPI 文档里、token 门禁（错 token 答 404）、`PI_METRICS=0` 答 503 并说明原因；**`TestSseEventPayloads`** 另钉住 `retrieval`/`llm_call` **绝不上线**（否则每次 run 都发一帧 `event: unknown`）；**`TestSessionFiles`**：附件三路由的集成层（上传/列表/下载，含**无认证**的 `GET /files/{sid}/{name}` 与 `PI_PUBLIC_BASE_URL` 未设时的 400；单元层见 `test_model_capabilities.py`，环境侧的暴露面分析见 `deploy/environments.md` L3） |
| `test_planning.py` | 16 | **任务规划（#45）**。工具层 8 例：计划作为 payload 记录、不碰磁盘、参数化拒绝越界/畸形计划（步数超 20、步长超 300、空标题、缺 steps、空 steps）、校验错误文案截断、成功文案能整段活过 SSE 预览、`all_tools()` 注册且带 `terminal` 标记；循环层 6 例：成功的 `submit_plan` 结束 run、同批后续调用被跳过且**不执行**、跳过调用有审计、无效计划不结束本轮、一批里两个 `submit_plan` 先者胜、跳过合成结果让历史对下一轮仍配对（配对不变量，见 §6.1） |
| `test_security.py` | 31 | 策略拒绝、路径逃逸、脱敏、审计（含认证记录的截断与防伪造行）、JWT；`server_policy` 只加不减（策略文件无法关掉 `path_sandbox`/`redact`）；对**仓库根那份生效的** `policy.json` 做回归：22 条危险命令必须拦、15 条日常命令必须放行（见 §17.18） |
| `test_observability.py` | 23 | 计量、配额、价格、降级链；**tracer**（span 父子树与"一次 run 一个 trace_id"、OTLP 导出到 `InMemorySpanExporter` 的整棵树 + ERROR 状态 + exception 事件 + resource 属性、缺 exporter 与拼错 `PI_TRACER` 都要**出声**地降级）；**metrics**（各序列真的产出、`in_flight` gauge 在异常后必须降回来、健康的索引不算降级、没有任何序列带用户/会话标签、关掉即 `render()` 为 None） |
| `test_deployment.py` | 12 | 缓存后端、本地沙箱执行器、迁移可达性；其中 1 例（真 Redis 限流）在 localhost:6379 无服务时 **skip** |
| `test_launch.py` | 7 | 注销/撤销、管理员端点、审计按天滚动 |
| `test_mysql_compat.py` | 4 | `engine_kwargs` 的方言分支 + 布尔默认值在 MySQL/PG/SQLite 三方言下的 DDL 兼容 + **`sessions.plan` 列的跨方言可移植性**（nullable TEXT，见 §17.24） |
| `test_smoke.py` | 2 | 端到端：fake 模型驱动完整 agent 循环（write→read→edit→grep 四次工具调用）+ `on_message` 回调 |
| `test_compaction.py` | 1 | 压缩：摘要替换旧历史、保留尾部 |
| `test_rebuild_milvus.py` | 5 | 重建工具（FakeStore 桩）：三个拒绝护栏（生产 ns 无 `--yes`、空 MySQL、缺 URI）、键集分页遍历、按用户分组 upsert、`mark_synced` 恰好清空 pending_sync、输出报告 |
| `test_model_capabilities.py` | 14 | **模型原生能力 / 会话附件**（此文件曾长期漏在本表之外）。三层：*线格式层* —— 带文件的用户消息翻成网关要求的 content-array 形态，能力开关真的到达请求（`extra_body` / 裸 tool 类型）；*循环层* —— 调网关侧执行的工具时用**空** tool result 应答（网关收到空应答才自己去跑），同时历史保持配对不变量；*集成层*在 `test_server.py::TestSessionFiles`。附件路由与 `PI_PUBLIC_BASE_URL` 的环境侧后果见 `deploy/environments.md` L3 |

> 原有 `test_cli_polish.py`（7 例，覆盖 chat REPL 的斜杠命令、自动标题、`--json`
> 事件流）随本地 CLI 一起删除；`test_deployment.py` 里的 GBK 解码例随 Windows 支持删除。
> 101 → 93 的差额（8 例）全部来自这两处，没有覆盖率损失。（93 是**那次删除之后**的
> 数量，不是当前总数；之后陆续补了认证审计、`X-Forwarded-For`、沙箱资源限额、策略回归
> 与 OpenAPI/SSE 契约，再后来加了整个长期记忆模块与会话附件，再后来是可观测层
> （span 父子树 + OTLP 导出 + `/metrics` + 召回明细），现在见上表 364 例
> —— 上表逐文件相加**正好等于** `pytest --collect-only` 的总数，2026-09-06 已核对。
> 改测试时记得同步这张表，否则它又会像 `test_server.py`（曾写 92，实际 76）和
> `test_model_capabilities.py`（整个文件曾不在表里）那样漂移。）

**测试约定**：

- 一律用 `FakeProvider` + 临时目录/一次性 SQLite 文件，绝不依赖真实模型或外部服务；
- `conftest.py` 里的 **`StrictFakeProvider`**：`FakeProvider` + 配对规则——真 provider
  都会拒绝"assistant 消息里有 `tool_call` 而紧随 user 消息缺对应 `tool_result`"的请求，
  且是在**下一轮**才拒绝；这个类把同一条规则放在喂流之前执行。跑重加载历史的测试
  因此会在 bug 真正所在的那一轮红掉（§6.1 配对不变量的测试级守卫，#45 的跳过合成
  结果就是靠它验的）；
- 异步测试用 `asyncio.run(main())` 包裹（未引入 pytest-asyncio 依赖）；
- `conftest.py` 除了把 `src/` 加进 `sys.path`，还在 import pi **之前**钉死
  `PI_REDIS_URL` / `PI_SANDBOX` / `PI_POLICY` / `PI_TRACER` / `PI_WEB_DIST`
  / `PI_MILVUS_URI` / `PI_EMBEDDING_MODEL` / `PI_RERANK_URL`
  / `PI_MEMORY_MODEL` / `PI_MEMORY_ARBITER_MODEL` / `PI_METRICS_TOKEN` / `PI_METRICS`，
  防止仓库根的生产 `.env` 被自动加载后把测试引到真 Redis / 真 docker / 真模型网关 /
  真向量库上，或让 `/metrics` 的鉴权门禁改变断言结果（原理见 §12.2，
  以及"这份清单已经漏过一次"的教训）。
  `PI_WEB_DIST` 指向一个不存在的路径是**为了路由确定性**：`web/dist` 一旦存在，
  app 就会在 `/` 上挂一个匹配一切路径的 Mount，整套测试的路由行为于是取决于
  "有没有人碰巧跑过 `npm run build`"。需要真目录的用例（`TestWebUiMount`）自己指；
- `aiosqlite` 只是**测试依赖**（`[dev]` extra），生产路径不含 SQLite。

### 15.1 集成测试层：`integration/`（花真钱，显式运行）

```bash
set -a; . ./.env; . ./.env.test; set +a    # conftest 自己也会做一遍，双保险
python -m pytest integration/ -q           # 4 例，全真：qwen-flash/qwen-plus、
                                           # embedding+rerank 网关、pi_py_test MySQL、
                                           # test 命名空间 Redis、it_memories Milvus、
                                           # docker 沙箱
```

离线套件证明的是**机制**（桩驱动），这一层证明的是**接线**——真实网关返回的
形状、真实 Milvus 的分区/索引行为、真实 MySQL 的方言，桩都替你扛不到。跑一次
约 30 秒、几万 token（主要是两轮 qwen-flash chat + 一轮 qwen-plus 仲裁），所以
**不进 CI**，需要验证部署或改动了 memory 接线时手动跑。

4 个用例：

| 用例 | 验什么 |
|---|---|
| `test_extraction_stores_facts_and_a_repeat_reconfirms_them` | 真抽取入库（3–5 条区间断言）；再用 `FixedProvider` 原样重放已存文本（cosine=1.0，绕开网关漂移），必须 touch 而不是重插 |
| `test_retrieval_injects_stored_facts` | 真检索：问题"用什么管理依赖"注入含 uv 的事实，embedding 花费入账 |
| `test_arbitration_merges_a_real_contradiction` | **真仲裁**：先插 uv 后插 pip 两条矛盾事实，真 qwen-plus 合并成一条，合并后文本含新说法、`created_at` 保最旧、`source_session=arbiter`、仲裁花费按 `session_id=arbiter` 入账 |
| `test_a_run_writes_memory_and_the_next_run_uses_it` | 完整 HTTP 金路径：注册→登录→真模型 run（声明偏好的提示词本身超 400 字，否则会合法地被 skip-short-turns 护栏跳过抽取）→后台抽取落 MySQL+Milvus→**第二个 run 的回答用上了注入的记忆**→`turns=0` 计费行真落 MySQL→**六张留痕表逐一用裸 SQL 断言**（user_memories 含 embedding blob 与 `milvus_synced=1` 回执、messages、agent_runs/steps、audit_events、usage_records）→清库清集合 |

这一层的三条硬边界：

- **目录不在 `testpaths` 里**：`pytest` 永远只跑 `tests/`，离线套件保持零成本、
  无网络也能全绿；`integration/` 必须打全路径显式触发。
- **两道护栏拒绝生产**：conftest 在 import 任何 pi 代码之前检查 `PI_DATABASE_URL`
  的库名必须以 `_test` 结尾、Milvus 命名空间强制为 `it`（`.env.test` 里有、代码里
  再钉一次），任一不满足直接 `SystemExit`。误连生产只剩"改了护栏本身"一种可能。
- **一个事件循环跑整个会话**：共享的 embedder/reranker 持有绑定首次运行所在循环
  的 httpx 连接池，`asyncio.run()` 每次换循环会炸，所以 conftest 提供 session 级
  `loop`/`run` fixture；server 用例则因为 TestClient 的 portal 在另一条线程另一
  个循环里，SQL 清理走一次性引擎，绝不复用 app 的连接池。测试里也只 `drain()`
  不 `close()`——close 会关掉共享 Milvus 线程池，后面的用例全灭。

每次会话前后 `it_memories` 集合都会 drop：前 drop 防上一次崩溃泄漏状态，后 drop
把集群留在干净状态（生产 `pi_memories` 从未被测试创建过）。MySQL 侧每例自建自删
`it_<hex>` 用户（连 usage/sessions/messages 一起清）。

---

## 16. 常见扩展怎么做

| 需求 | 做法 |
|---|---|
| 加一个新工具 | `tools/` 下建类（`name/description/input_schema/execute`），在 `tools/__init__.py` 的 `all_tools()` 注册；若带路径参数，记得让 `policy._extract_path` 认识它（路径沙箱才管得住） |
| 加一个会终止回合的工具 | 在工具类上设 **`terminal: bool = True` 类属性**即可——循环是通用的、不硬编码任何工具名，结束语义、同批跳过合成、配对不变量、审计全部白拿（§6.1）。要给前端发专门事件就在 `ToolResult.payload` 里装结构化数据，循环负责转成事件（`submit_plan`→`PlanEvent` 即此模式） |
| 接新的模型厂商 | `llm/` 下实现 `LLMProvider`（把厂商流翻译成三种 StreamEvent），在 `registry.resolve` 加分支 |
| 加服务端新路由 | 在 `app.py` 的 `create_app` 内定义，挂 `current_user`/`require_admin` 依赖；需要新表就加 ORM + 迁移 |
| 改系统提示词 | `prompt.py` 的 `SYSTEM_PROMPT`（由 `runner.py` 传给 `AgentLoop`） |
| 调整压缩策略 | `compaction.py` 的提示词；阈值是 `AgentLoop` 的构造参数 `compact_threshold` / `compact_keep`（默认 80,000 字符 / 保留 8 条）。**未暴露成环境变量**，`RunManager` 也没传，要改就在 `runner.py` 构造 `AgentLoop` 处加参数 |

## 17. 注意事项与坑（前人踩过的）

1. **`PI_JWT_SECRET` 是多实例的命根**：不设的话每个实例各自生成，跨实例登录互认不了；
   重启后换了=全员登出。生产一定显式注入且不要轮换（除非有意踢人）。
2. **注册永远开放，管理员只能改库授予**：`UPDATE users SET is_admin=1 WHERE username='...'`。
   这意味着 8300 一旦暴露到公网，任何人都能开号（代价见 §11.3）。
   想清库重来：按外键顺序清 `usage_records → messages → sessions → users`
   （TRUNCATE 会因外键失败）。
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
11. **压缩不落库**：长会话每轮都会重新付一次摘要 LLM 调用，`messages` 表只增不减
    ——这不是 bug 而是当前实现现状（`on_compact` 没接线），因果与修法见 §6.3。
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
20. **本地出网工具就是 SSRF，而且和沙箱一点关系都没有（已靠删除闭合）**：曾经的
    `web_fetch` / `web_search`（`tools/web.py`）用 `httpx` 跑在**应用进程内**，不进容器，
    所以 `--network none` 对它们毫无约束。`web.py` 只做了 `^https?://` 前缀检查（还会自动
    补前缀），没有任何地址校验，并且 `follow_redirects=True`。火山引擎元数据服务
    `100.96.0.96` 实测可达——也就是任何注册用户都能让**宿主机**去请求元数据服务。

    中间态是靠 `policy.json` 的 `deny_tools` 把这两个工具整个禁掉，**那是缓解不是修复**。
    最终处理是**删掉 `web.py`**：联网能力改由模型端点自己的 builtin tools 承担
    （`RunIn.builtin_tools`，provider 侧发起请求，不涉及本进程的网络位置）。
    `policy.json` 的 `deny_tools` 因此为空，那条注释保留了完整因果。

    留下的纪律：再加任何本地抓取工具之前必须做两件事——**解析后**拒绝私网/环回/
    链路本地/元数据地址，以及**对每一跳重定向重新校验**（只校验第一跳等于没校验）。
    `tests/test_security.py::test_the_local_tool_surface_has_no_internet_tool` 会在你
    给任何注册工具 import HTTP 客户端时直接红。
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

23. **`-> dict` 让 OpenAPI 变成废纸，也让出站 body 没人校验**：15 个路由原来全标注
    `-> dict`，dump 出来每个响应都是 `{"additionalProperties": true, "type": "object"}`，
    `components.schemas` 里只有 6 个请求模型。这样的文档没法 codegen，前端只能手写类型
    ——于是同一份契约有两个真相源，谁先改谁埋雷。补上响应模型之后 39 个 schema，
    并且**第一个抓出来的就是真 bug**（§11.3.1 的 `blocks` 套娃）：它活了很久，因为
    老测试只断言 `role` 和条数，而 `-> dict` 意味着 FastAPI 对返回内容一个字都不检查。

    教训和 §17.17 是同一个：没有类型/校验的地方，错误不会报错，只会在下游以奇怪的
    形状出现。**给路由加响应模型的收益主要是"出站也过一遍校验"，文档只是副产品。**

    遗留的坑：`messages.blocks` 列名叫 blocks，存的却是整条 `Message`
    （`{"role": ..., "blocks": [...]}`）。读写两端自洽，只是名字骗人；改名要动
    Alembic 迁移和已有数据，收益不抵风险，暂时留着——但**别再往这个列里塞别的
    东西**，也不要按列名去猜它的内容。

24. **TEXT 列的默认值夹缝：MySQL 和 Postgres 要的形状互斥，NULL 是唯一公约数**。
    加 `sessions.plan`（迁移 0003）时踩的：MySQL 对 TEXT/BLOB/JSON 列**直接拒绝
    字面 DEFAULT**（DDL 期错误 1101）；PostgreSQL 则拒绝在**已有数据**的表上
    `ADD COLUMN ... TEXT NOT NULL` 而不给默认值。给默认值 MySQL 炸，不给且 NOT NULL
    Postgres 炸——同时满足两家的形状只有"nullable TEXT、无 server_default"。

    最阴险的一点：照抄 0002 给布尔列用的 `server_default` 写法在 **SQLite 上编译得
    干干净净**（开发/测试方言），到生产 MySQL 跑 `pi-py migrate` 才炸。钉住它的回归
    是 `test_mysql_compat.py::TestPortableDDL::test_sessions_plan_column_is_portable`：
    对三方言编译 `CreateTable`，断言 DDL 里没有 DEFAULT，并整行钉死
    `plan TEXT,`（比较前剥掉引号——SQLite 把标识符加引号，`PLAN` 在它的关键字表里；
    MySQL/PG 不加）。让失败发生在测试期，而不是生产 migrate 期。

25. **裸 `AsyncSession(engine)` 的 `expire_on_commit` 陷阱（MissingGreenlet）**：
    `Database.sessionmaker` 建的是 `expire_on_commit=False`，但各仓储方法里手写的
    `async with AsyncSession(self.db.engine) as s:` **不是**——commit 后再碰 ORM 对象
    属性会触发隐式 lazy refresh，而这时已经在 greenlet 上下文之外，直接炸
    `MissingGreenlet: greenlet_spawn has not been called`。两个惯用法：id 在 flush 后、
    commit 前存进局部变量（`AgentRunRepo.append`），或 `await s.refresh(row)`
    （`SessionRepo.create`）。最阴的是这类 bug 在生产里**静默**——写入已提交成功，
    只是函数尾部抛异常被 `except Exception` 吞成一行日志，数据在、功能像正常，
    直到有人写测试直接调仓储才炸出来。

26. **Bounded 一致性下 Milvus 的 upsert-then-search 有 ~0.5s 盲窗**：实测火山引擎
    serverless Milvus，刚 upsert 的向量在约半秒内搜不到。`_recall` 因此把**零命中**
    也当作需要回退 MySQL 全扫的信号（不是只有索引报错才回退）——repo join 只能
    过滤索引给的 id、救不回索引看不见的。没有这条，用户追问最快的那一拍恰好读到
    "昨天"的记忆。离线测试 `test_an_index_that_lags_its_upsert_still_recalls_from_the_repo`
    钉着。

27. **审计入 MySQL 后，管理端查询要容忍"晚一拍"**：审计记录走有界队列 + 后台
    drainer，不占请求路径。这意味着（a）`GET /v1/admin/audit` 紧跟着一次操作去查
    可能还查不到（测试里要轮询）；（b）进程退出时 lifespan 的 `close()` 负责冲刷
    余量，顺序在 `db.dispose()` 之前——调换顺序会拿死引擎冲刷。

---

## 18. 前端 `web/`

### 18.1 定位与刻意不做的事

一个**最小可用**的两屏客户端（登录 / 会话），Vue 3 + TypeScript + Vite + Pinia + Naive UI。
目标是"能用、契约不漂、改后端时前端会红"，不是做一个完整产品。

刻意**没有**的东西，以及为什么：

| 没做 | 理由 |
|---|---|
| vue-router | 只有两个界面，`App.vue` 按 `auth.signedIn` 一个布尔切换。引入路由等于为一个 `v-if` 付一整套 history/守卫/懒加载的复杂度 |
| 客户端表单校验 | 边界（长度、必填）定义在 `RegisterIn` 上，会以 422 到达浏览器，`errorMessage()` 已经能渲染它。把数字在前端再抄一遍，就是给它**第二个会漂的地方** |
| `localStorage` 存 token | 见 18.4 第 1 条 |
| 配额/用量面板 | 402 的 detail 里已经带了"已用 X / 共 Y tokens"，够用 |
| 显示 `SessionCreatedOut.cwd` | `SessionListOut` 里没有这个字段，显示出来一刷新就没了——**宁可不显示，也不要显示一个会消失的东西** |
| 管理端界面 | 管理员端点存在，但没有 UI；改库授予管理员的现状（§11.3.1）本身就要求运维直连数据库 |

### 18.2 目录与数据流

```
web/
  openapi.json              入库的契约（tools/dump_openapi.py 产出）
  src/api/
    schema.d.ts             ← codegen 产物，不手写
    types.ts                从 schema.d.ts 里挑出用到的类型别名
    client.ts               fetch 包装：注入 Bearer、统一 ApiError、读 X-Request-Id
    endpoints.ts            每个路由一个函数（签名来自 types.ts）
    sse.ts                  SSE 帧解析 + postSse 异步迭代器
  src/stores/
    auth.ts                 token / username / busy / error / restored / signedIn
    chat.ts                 会话列表 + 消息 + 流式绘制（本层最复杂的文件）
  src/views/                LoginView.vue / ChatView.vue
  tests/                    sse / transcript / render + fixtures（真服务器抓的）
  tests/live/               联调用例（需要一个跑着的服务，默认不跑）
```

数据流单向：`endpoints.ts` → `stores/*` → `views/*`。视图不直接碰 fetch，store 不碰 DOM。

`chat.ts` 里有**两个**把线上事件变成界面的函数，职责必须分清：

- **`painter(messages)`** —— 消费 SSE 事件流，边流边画。它是**一次性的、有状态的**
  （记着"当前这条 assistant 消息"和"上一个事件是不是 toolcall_end"）。
- **`toUi(history)`** —— 消费 `GET /messages` 的持久化历史，纯函数、无状态。

两者产出同一个 `UiMessage[]` 形状，所以视图不需要知道数据是流来的还是读来的。
`send()` 的流程是：乐观推入用户消息 → `painter` 边流边画 → **流结束后无条件
`await reload(sid)`**，用持久化的历史**覆盖**刚才画的东西。

覆盖是对的，不是浪费：线上 SSE 的工具结果是被 `AgentLoop` 截到 200 字符、换行压成空格的
**预览**，而 `toolcall_start` 根本不带参数（两条都见 §6.2）；持久化历史里两样都是全的。
所以"先画个大概、再用真相覆盖"是唯一能既低延迟又显示完整内容的办法。

**计划卡（`submit_plan`，#45）在两个 reducer 里的形状不同**，要分开理解：

- **live（`painter`）**：`case "plan"` 把计划作为**独立的一条 assistant 消息**推入
  （与 compaction 同一模式；role 用 assistant 就是为了对上 reload 后 `toUi` 的产物）。
  服务端保证 plan 帧是本轮最后一个内容事件（§6.2），所以它后面不会再有内容。注意
  live 形状里**同时**还有 `submit_plan` 的工具行——toolcall_start/end 帧照常把它画成
  一个普通工具调用。
- **reload（`toUi`）**：持久化历史里**没有**专门的计划块——计划唯一幸存的副本是
  `submit_plan` 那个 `ToolCallBlock` 的 `arguments`（工具结果只是句简短确认文案；
  `sessions.plan` 也不在 messages 端点里）。所以 `toUi` 用 `parsePlan(arguments)` 把
  调用**折叠成一张卡**，并刻意不把该调用推进 `ui.tools`——卡已经展示了它，下面再挂
  一行原始 JSON 是噪音（调用仍注册进 map，配对的 tool_result 因此被吸收，不会漏成
  孤儿 user 气泡）。
- **`parsePlan` 守卫很窄**：JSON 解析失败、不是对象、title 不是字符串、steps 不是
  非空字符串数组——任一不满足返回 `null`，调用落回普通工具行路径。原始参数仍是
  用户该看到的证据，和 `prettyArgs` 同一个理由。
- **失败预扫描**：校验失败的 `submit_plan` 从未成为计划（没有 PlanEvent、没写列），
  就不能渲染成卡；而它的 `is_error` 结果**晚一条消息**才到，折叠在看到调用时无从
  判断——所以先扫一遍历史收集失败 id，再看调用。
- 于是 live 与 reload 的形状差异是真实存在的：live 是"工具行 + 卡"，reload 只剩
  "折叠卡"。`send()` 流结束后的无条件 reload 会让折叠形状胜出，故这是短暂的差异，
  但调试时要知道两者都对。

### 18.3 契约与托管

**契约工作流**（改后端路由时必须走完）：

```bash
python tools/dump_openapi.py     # 1. 重新 dump web/openapi.json
cd web && npm run gen:api        # 2. 重新生成 src/api/schema.d.ts
npm run typecheck                # 3. 后端改了名字，这一步会红
```

类型不手写，所以后端一次重命名的后果是**前端编译失败**，而不是运行时某个字段悄悄变
`undefined`。`web/openapi.json` 入库正是为了让契约变更出现在 diff 里（§11.3.1）。

**产物由 app 进程自己托管**：`create_app` 最后一行
`app.mount("/", StaticFiles(settings.web_dist, html=True))`。理由是**没有 CORS 中间件**，
而加一个只会让浏览器源和 API 源不同、毫无收益——同源就是目的。于是裸跑
`pi-py serve` 之后 `http://host:8300/` 直接就是界面，不需要动 Caddy
（两个 Caddyfile 都只 `reverse_proxy`，都不托管静态文件）。

挂载**顺序**是它能不能用的全部原因：Mount 在 `/` 上匹配一切路径，晚于所有路由注册，
`/v1/*`、`/healthz`、`/readyz`、`/openapi.json`、`/docs` 才不会被吞。开发期
`npm run dev` 走 vite 的 `/v1` 代理（`vite.config.ts`），SSE 也走同一条代理。

### 18.4 六个不踩一遍就想不到的坑

1. **token 存 `sessionStorage`，不是 `localStorage`。** 按标签页隔离、关标签即失效。
   `localStorage` 会跨标签页存活到永远，且对源上任何脚本可读——在 XSS 面前它把
   "一次注入"放大成"永久凭据"。代价是刷新页面要重新登录？不会，`sessionStorage`
   在同标签页刷新后仍在；只有新开标签页才需要重新登录，那是**有意的**。

2. **`auth.restore()` 只在 401 时清 token。** 启动时拿存下的 token 打一次 `/v1/me`：
   401 说明令牌真的废了（过期/被踢/账号禁用），清掉退回登录页；**其它任何失败**
   （网络断、502、超时）都保留令牌，因为一次网络抖动不该把人登出。
   `restore()` 还**保证不抛异常**，并且用 `restored` 这个 ref 让 `App.vue` 在它落定前
   只渲染一个 spinner——否则会先闪一下登录页再跳进会话。

3. **`reactive` 数组的 `push` 存进去的是原始对象。**
   `arr.push(obj)` 之后继续改 `obj` 是**改不到界面上的**：proxy 的 set 陷阱被绕过了，
   流式 delta 一个都不会到 DOM。必须把元素**从数组里读回来**
   （`arr[arr.length - 1]`）拿到 proxy 再改。`painter` 里两处都这么做，并写了注释——
   这个 bug 的症状是"数据明明变了但界面不动"，非常难查。

4. **`strict` 不包含 `noUncheckedIndexedAccess`。** `arr[i]` 的类型是 `T` 而不是
   `T | undefined`，于是：拿它和 `undefined` 比是 **TS2367 编译错误**，而在空数组上读它的
   属性是**运行时抛异常且 tsc 一声不响**。这两个坑都实际踩过，都改了：
   `tail` 那个 computed 改成先判长度再和 `null` 比；`isThinking` 加了显式长度守卫。
   **凡是索引访问，自己判长度。**

5. **中文输入法的 Enter 必须看 `e.isComposing`。** 用 Enter 确认候选词时，浏览器发的
   `keydown` 就是 `key === "Enter"`。没有这个守卫，选词的瞬间消息就发出去了——发出去的还是
   半截拼音。`ChatView.onKeydown` 的三个条件是 `key === "Enter"` && `!shiftKey` &&
   `!isComposing`。

6. **Naive UI 的 `NAlert` 关掉之后是内部状态，props 更新不会让它复活。**
   所以两条错误横幅都挂了 `:key="chat.error"` / `:key="chat.streamError"`：内容一变就是
   一个新组件。不这么做的话，用户关掉第一条错误之后，**第二条永远看不见**。
   同一类问题：两个条件渲染的 `NAlert` 如果直接做 `grid-template-rows: auto auto 1fr auto`
   容器的子元素，同时出现时会多出第五个 item，把 transcript 挤出它那一行——所以外面包了
   一个 `.banners`。

### 18.5 测试：四层，以及每层证明不了什么

这台机器**没有浏览器**，所以验证是靠分层堆出来的，而不是靠"我看了一眼"。
清楚每层的边界比多写几个用例重要：

| 层 | 位置 / 命令 | 证明了 | **证明不了** |
|---|---|---|---|
| 协议层 | 手工 curl 穿 vite 代理 | SSE 头（`cache-control: no-cache`、`x-accel-buffering: no`、chunked）、`X-Request-Id` 过代理不丢、UTF-8 中文不烂、`blocks` 真的是扁平数组 | 任何前端代码 |
| 客户端代码层 | `tests/live/api.live.ts`，`PI_LIVE_API=... npm run test:live`（11 例） | 拿**真的** `client.ts`/`sse.ts`/`endpoints.ts` 打真服务器：`parseFrame` 在真字节上正确、`ApiError` 带真的 12 位十六进制 request id、**abort 能以 `AbortError` 释放 reader 而不挂死**、流出来的文本经 `toUi` 折叠后与持久化历史逐字相同；另覆盖注册 409、登录换 token、会话增列、月度用量与生效配额、记忆列表、非管理员被 403 挡在管理端外、注销后账号与 token 同时死 | 界面长什么样 |
| 纯逻辑层 | `tests/sse.test.ts`(9) + `tests/transcript.test.ts`(21) | SSE 分帧规则、`painter` 与 `toUi` 两个 reducer。fixture 是从真服务器抓的会话（含工具调用与失败），不是手编的 | 组件是否真的用了这些函数 |
| 渲染层 | `tests/render.test.ts`(26)，用 `vue/server-renderer` | store 状态 → 模板产出的 HTML：绑定名写错、prop 名写错、条件分支反了，都会红（这些 **typecheck 和打包都放行**） | **DOM 事件**：SSR 只渲染不派发，所以 `v-model`、`@keydown`、表单提交这一层完全没被覆盖 |

三条核心不变量都做过**变异检验**（改坏源码看测试是否变红）：`painter` 的消息边界启发式、
`toUi` 的结果回填、以及后端 `PREVIEW_LEN` 与 SSE 文档描述的一致性。渲染层的变异检验是
删掉 `{{ m.text }}`。#45 的两处也做过：删掉 `toUi` 的计划折叠（卡退化为普通工具行）
和删掉失败预扫描（校验失败的 `submit_plan` 被渲染成卡）都会红。

`tests/live/*.live.ts` **故意**不匹配 vitest 默认的 include glob
（`**/*.{test,spec}.?(c|m)[jt]s?(x)`），所以 `npm test` 永远不需要服务器，也不用写
exclude 配置。`npm run build` = `vue-tsc --noEmit && vitest run && vite build`：
**没有 CI，构建就是产物被托管之前唯一的闸门**，所以测试挂在 build 上而不是单独一步。

### 18.6 已知缺口

- **中断一轮会丢掉整轮记录**（后端问题，不是前端的）。`runner.py` 的 `if buffer:`
  持久化块在 `async for` **之后**，而 `finally:` 只释放锁——所以用户点"停止"，
  工具**已经真的跑过了、副作用是真的**，数据库里却一个字都没有。前端如实反映了这一点：
  `send()` 在流结束后用持久化历史覆盖，于是被中断那一轮的 prompt 会**在界面上消失**，
  `chat.ts` 里写着注释说明这是服务端的真相而不是前端丢状态。修它要动 `runner.py`
  （把已产出的消息在 `finally` 里落库），不是改前端。
- **`toolcall_start` 不带参数**，所以审批类界面无从展示"你要批准的是什么"。Phase 2
  的人工确认要先给这个事件加上 arguments。
- **计划卡的 live/reload 形状不一致**（#45 的已知取舍）：live 是"工具行 + 卡"，
  reload 折叠成只剩一张卡（成因与理由见 §18.2）。`send()` 的无条件 reload 让折叠
  形状最终胜出，差异是短暂的；记录在此是防止有人把其中一边"修"成另一边——
  两边各自都是有意的。
- **没有做视觉验证**。布局、配色、响应式、长文本溢出、暗色模式，全部未经人眼确认；
  渲染层测试只证明"产出了这些标签和类名"。
- **`messages.blocks` 列名仍然误导**（叫 blocks 存的是整条 `Message`），前端按
  `MessageOut.blocks: Block[]` 消费的是**路由已经拆好的**扁平数组，与列名无关（§11.3.1）。

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
             (成功的 submit_plan 终止循环, yield plan 帧 —— §6.1)
  成功: 批量持久化消息 -> [有计划? 写 sessions.plan] -> 记用量
  中断(客户端断开): 什么都不落库（工具却已经真跑过了，副作用是真的 —— 见 §18.6）
  超时: 已产出的消息和计划仍会落库（§11.5）
  finally: 释放锁
用户 <- event: done
```

