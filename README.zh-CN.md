# pi-py

[English](README.md) | **中文**

[![ci](https://github.com/15838328874/pi/actions/workflows/ci.yml/badge.svg)](https://github.com/15838328874/pi/actions) · [![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **实测战绩**：真实模型企业测评 **5/5** ✅ · 安全回归 **40/40** ✅ · SandboxFS 组合 **11/11** ✅ · Python 3.12 · CubeSandbox 会话级沙箱

一个**可自托管、可扩展的 AI Agent 平台**：沙箱化工具执行、多租户治理（JWT / 配额 / 限流 / 审计）、统一执行轨迹（Canonical Trajectory）、带判分与 A/B 的评测体系、RL 数据飞轮（rollout → reward → SFT/RLVR JSONL），以及基于 MCP + Skills 的协议级工具扩展。Coding 是第一个完整落地的场景，而不是边界。它是 [pi coding agent](https://github.com/earendil-works/pi)（TS 版单机 CLI）的 Python 重实现，以多用户 HTTP 服务形态交付：流式 LLM 层、asyncio agent 循环、编码工具与沙箱（生产用 CubeSandbox microVM，本地用 Docker）。包分层与 pi 对齐：

| pi (TypeScript)   | pi-py (Python)                                        |
| ----------------- | ----------------------------------------------------- |
| `pi-ai`           | `pi.llm` (openai / anthropic / fake)                  |
| `pi-agent-core`   | `pi.agent` (asyncio loop + events)                    |
| `pi-coding-agent` | `pi.tools` + `pi.prompt`                              |
| daemon / server   | `pi.server` (FastAPI + SSE), `pi.cli` (serve/migrate) |

## 亮点

完整叙事、设计取舍与踩坑实录都在 [ARCHITECTURE.md](ARCHITECTURE.md)：

| 亮点 | 一句话 | 详情 |
|---|---|---|
| 🏜️ **会话级沙箱（方案 B）** | 每个回合**新建独立 VM**（71ms 冷启）、用完即销毁：零常驻、崩溃隔离、内存模型 = 并发回合数 × 256Mi 而非会话数 | [架构总览](ARCHITECTURE.md#3-整体架构) · [设计笔记](docs/cube-sandbox-design-notes.md) |
| 🛡️ **SSRF 防线** | 进程内抓取工具（web_fetch/web_search）已**移除**，抓取一律降级到沙箱内执行——宿主内网（MySQL/Redis/云元数据）对模型不可达 | [坑 20（已解决）](ARCHITECTURE.md#17-注意事项与坑前人踩过的) |
| 🗂️ **工作区闭环归档** | 每回合基线快照 → 结束 tar.gz + 差异元数据（added/modified/deleted），MinIO 惰性接口就绪 | [实测数据](ARCHITECTURE.md#附录实测数据速查) |
| 🧪 **自治验证** | 真实模型跑企业任务：数据分析/日志解析/GitHub 情报/文档摘要/数据清洗 **5/5 PASS**；安全回归 **40/40**；沙箱组合 **11/11** | [测试体系](ARCHITECTURE.md#15-测试) |
| ☁️ **生产就绪** | 部署手册（10 节）+ 生产就绪审计（7 项修复、剩余清单全部闭环） | [部署手册](docs/production-deployment.md) · [审计](docs/production-readiness.md) |
| 🔐 **多租户治理** | JWT / 配额 / 限流 / 审计 / 会话级分布式锁（同会话并发直接拒绝，跨会话独立沙箱并行） | [架构总览](ARCHITECTURE.md#3-整体架构) · [架构手册](ARCHITECTURE.md#13-配置速查表) |

> **文档地图**（各管一段，知识点不重复）：
>
> | 文档 | 定位 | 什么问题看它 |
> |---|---|---|
> | `README.md` | 门面 | 这是什么、怎么装、怎么跑（快速上手入口） |
> | `ARCHITECTURE.md` | 技术手册 + 叙事 | 每个模块每个函数、配置全表（§13）、坑清单（§17）、差距清单（§19）；设计取舍、测试样例、术语表、实测数据 |
> | `ROADMAP.md` | 状态与路线图 | 什么做完了、什么没做、下一步做什么（含环境区分表） |
> | `docs/`（专项） | 沙箱 / 部署 / 设计存档 | 沙箱设计笔记、生产部署手册、生产就绪审计、文件传输模式、run 持久化与媒体输出设计存档 |
>
> **推荐阅读路径**：先看本页 `## 亮点` 建立全局印象 → 想深入了解设计取舍、踩坑故事、实测细节 → 直接读 [**ARCHITECTURE.md**](ARCHITECTURE.md)；对照代码逐模块也看它；做没做、下一步 → [ROADMAP.md](ROADMAP.md)。

仅支持 Linux。曾经的本地单机 CLI/TUI 形态、本地 SQLite 会话存储与 Windows/WSL 支持已全部移除——这个包就是服务本身，别无其他。

## 安装

```bash
cd pi-python
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[production]"   # 可部署服务：asyncpg + aiomysql + redis + alembic + otel
pip install -e ".[dev]"          # 仅测试依赖：pytest + aiosqlite
```

## 配置

环境变量，或 `.env` 文件（项目根 `./.env`，或 `~/.pi-py/.env`；找到第一个存在的文件即停，已存在的环境变量永远优先）：

```bash
PI_DATABASE_URL=mysql+aiomysql://user:pass@host:3306/pi_py   # 必填
PI_REDIS_URL=redis://user:pass@host:6379/0                   # 锁 / 限流 / 撤销
PI_REDIS_NS=prod                                             # 键命名空间，隔离不同环境
PI_JWT_SECRET=<openssl rand -hex 32>                         # 生产必填
PI_MODEL=openai/qwen3.8-max
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
# 可选向量语义记忆：四个变量全配才启用（否则纯词法检索）
PI_EMBEDDING_URL=https://.../compatible-mode/v1/embeddings
PI_EMBEDDING_API_KEY=sk-...
PI_EMBEDDING_MODEL=qwen3.7-text-embedding
PI_MILVUS_URI=http://milvus-host:19530
# 可选额外工具来源（空 = 关）
PI_MCP_SERVERS='[{"name":"filesystem","command":["npx","-y","@modelcontextprotocol/server-filesystem","/ws"]}]'
PI_SKILLS_DIR=/opt/pi-py/skills
```

要点：

- `PI_DATABASE_URL` **必填**。MySQL（`mysql+aiomysql://`）或 PostgreSQL
  （`postgresql+asyncpg://`）；缺失则服务拒绝启动。没有本地文件兜底——SQLite 仅作为测试依赖存活。
- 任何 OpenAI 兼容端点都可以通过 `OPENAI_BASE_URL` 接入（阿里云百炼已验证：
  `token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`）。
- `<think>...</think>` 推理段会在流中自动剥离（qwen/deepseek 风格）。
- 向量语义记忆（`remember`/`recall` 工具 + 每轮开始自动注入）：四个 `PI_EMBEDDING_*` /
  `PI_MILVUS_URI` 变量启用 Milvus 向量检索，`memories` 表是唯一事实源；不配（或缺任一）= 词法检索。
  Milvus 故障自动降级为词法——影响的是检索质量，不是正确性。
- MCP stdio server 作为应用子进程启动并继承其环境变量：请把 `PI_MCP_SERVERS` 视为管理员级配置。
  MCP/skill 工具走同一道策略闸门；未知工具的 path 类参数由通用路径沙箱收敛到 workspace。
  它们不声明任何能力，因此 allow-list 策略下被 fail-closed 拒绝（见企业安全一节）。
- 没有 API key？设 `PI_MODEL=fake/demo`——脚本化 provider 用固定回复把每条 HTTP 路径端到端跑通。
- 永远不要提交 `.env`（已 gitignore）。谨慎轮换 `PI_JWT_SECRET`：换掉它所有已签发 token 立即失效。

## 运行

```bash
pi-py migrate                                  # 对 PI_DATABASE_URL 执行 alembic upgrade head
pi-py serve --host 0.0.0.0 --port 8300
```

**本地测试环境**（MySQL + Redis + Milvus 全套 Docker 化 + 真实模型，与生产严格区分）：
见 [`deploy/local-dev.md`](deploy/local-dev.md)，基础设施编排在 `deploy/docker-compose.local.yml`，
环境变量模板在 `deploy/env.local.example`（生产是云端托管 MySQL/Redis/Milvus + `docker-compose.cloud.yml`）。

注册默认开放（公网演示用 `PI_ALLOW_REGISTER=0` 关掉）：每个注册者都是普通用户。管理员只能直连数据库授予——没有提权路由：

```sql
UPDATE users SET is_admin=1 WHERE username='alice';
```

因为注册免鉴权且无限制，请把端口挡在公网之外（安全组/防火墙）。暴露它意味着任何人都能开号，每个号都自带月度 token 配额和一个 workspace 目录。

```bash
curl -X POST :8300/v1/auth/register -d '{"username":"alice","password":"..."}'
TOKEN=$(curl -s -X POST :8300/v1/auth/login -d '{"username":"alice","password":"..."}' | jq -r .access_token)
SID=$(curl -s -X POST :8300/v1/sessions -H "Authorization: Bearer $TOKEN" -d '{"title":"t"}' | jq -r .id)
curl -N -X POST :8300/v1/sessions/$SID/runs -H "Authorization: Bearer $TOKEN" -d '{"prompt":"hi"}'
```

## 工具

12 个内置工具，注册在 `tools/__init__.py::all_tools()`：

`bash`（超时+退出码）、`read`（行号、分页）、`write`、`edit`（精确唯一匹配、replace_all、unified diff）、
`grep`（正则，跳过 VCS/构建目录）、`find`（glob）、`ls`、`remember`、`recall`、`spawn_subagents`、
`list_files`、`fetch_file`（共 12 个；进程内的 `web_fetch`/`web_search` 因 SSRF 风险已移除——沙箱内 bash 是抓取路径）。

三个额外工具来源，由 `ToolRegistry` 合并（内置优先；同名冲突保留内置；一个来源坏了不影响其他）：

- **MCP**（`PI_MCP_SERVERS`，JSON 数组，`{"name","command":[...]}` stdio 或 `{"name","url"}` HTTP
  server，走官方 `mcp` SDK）：每个 server 的工具成为普通工具——走同一道策略闸门、路径沙箱、审计与追踪。
  server 挂掉表现为逐调用工具报错（v1 无自动重连）。
- **Skills**（`PI_SKILLS_DIR`，`<skill>/SKILL.md` 包目录，Anthropic Agent Skills 风格）：精简索引注入
  系统提示词，`use_skill` 按需加载完整指令，每个 `scripts/*` 文件成为在沙箱内执行的
  `skill_<name>_<script>` 工具（脚本先被暂存进 workspace，因为沙箱只挂载 workspace）。
- **RAG**（`rag_search`，默认开；`PI_RAG_ENABLED=0` 整个移除）：企业文档检索，带引用溯源
  （文档/章节/页码）与用户级 ACL。文档经 HTTP 上传端点（`POST /v1/rag/ingest`，异步入库）或
  `pi-py rag ingest` 入库；扫描件/图片路由到外部 OCR 服务（`PI_RAG_HEAVY_PARSER=paddleocr`，
  同一协议可换 MinerU）。详见 `ARCHITECTURE.md` §21。

上下文压缩：历史超过 80,000 字符自动触发——LLM 写的摘要替换旧前缀，最近 8 条消息原文保留，
仅对当前 run 的内存生效。摘要同时落 `compactions` 表（经 `on_compact`，fail-soft），原始消息
只增不减；后续每轮加载 `[最新摘要] + [摘要之后的新消息]` 而非重新总结——同一段历史只付一次摘要费。

## 企业安全

`PI_POLICY` 指向一份应用于**每一次**工具调用的 JSON 闸门。仓库根的 `policy.json` 是两种部署形态
共用的**生效策略**（裸金属让 `PI_POLICY` 直接指向它；两个 compose 把同一文件挂到
`/etc/pi-py/policy.json`）。结构：

```json
{
  "deny_command_patterns": [
    "(?:^|[;&|(]\\s*)sudo\\b",
    "\\brm\\s+(-{1,2}[a-z-]+\\s+)*/(\\s|$|\\*)"
  ],
  "path_sandbox": true,
  "redact": true
}
```

- **deny_tools** — 工具黑名单，执行前拒绝
- **deny_command_patterns** — bash 命令正则黑名单（大小写不敏感，`re.search`）
- **path_sandbox** — 文件工具限制在会话 workspace 子树内（`../../` 逃不出去）
- **redact** — 出站脱敏（API key、阿里云/AWS/GitHub/Slack token、中国手机号、身份证号、内网 IP）；
  只遮发给 LLM 的那份副本，落库历史保留原文
- **allow_capabilities** — 能力白名单（JSON 数组）。非空时，工具必须**声明了能力**且声明的集合
  **全部**在白名单内才放行——是子集而非交集，所以 bash 的 `filesystem.read`/`filesystem.write`
  不会在缺 `process.execute` 时被放进纯读环境。未声明能力的工具（所有 MCP/skill 工具）被拒绝：
  **fail-closed**。空（默认）= 白名单关。
- **deny_capabilities** — 任何声明的能力与该集合**有交集**的工具被拒绝（先于白名单检查）

能力词表（声明于 `tools/*.py`）：`filesystem.read`（read/ls/grep/find、list_files）、
`filesystem.write`（write/edit、fetch_file）、`process.execute` + 两个 filesystem 能力（bash）、
`memory.read`（recall）、`memory.write`（remember）、`agent.delegate`（spawn_subagents）、
`knowledge.retrieve`（rag_search，只读检索本用户已入库文档）。检查顺序：deny_tools →
deny_capabilities → allow_capabilities → bash 模式 → 路径沙箱。

模式刻意锚定命令位置（`(?:^|[;&|(]\s*)`）：裸的 `"sudo"` 也会拦掉 `grep -rn 'sudo' src/`，而
**误伤比漏拦更难排查**——它会静默破坏普通 agent 工作。`tests/test_security.py::TestShippedPolicy`
对随仓策略钉了两个方向的回归：22 条必须拒绝的命令、15 条必须放行的命令——改正则前先往这两个
清单加用例。

策略文件只能**加**规则：`server_policy()` 强制打开 `path_sandbox` 和 `redact`，即使文件里省略或
显式设为 `false`（`Policy.from_dict` 默认两者为 `False`，否则一份只列拒绝规则的文件会顺手关掉
工作区沙箱和脱敏）。不配任何策略文件时，服务仍以 `path_sandbox + redact` 运行。每一次工具调用
（放行**和**拒绝的）都追加进审计日志 `~/.pi-py/audit.jsonl`，按天滚动为 `audit-YYYY-MM-DD.jsonl`
（JSONL：时间戳、用户、会话、工具、参数、决定、结果）。

## 多用户服务

| 关注点 | 实现 |
|---|---|
| 认证 | JWT（HS256，PyJWT），PBKDF2 口令哈希（20 万次迭代）经 `asyncio.to_thread` 移出事件循环，并发登录不冻结服务；注册免鉴权开放，每个账号都是普通用户——管理员只能 `UPDATE users SET is_admin=1 ...` |
| 会话隔离 | 会话/消息按用户隔离；跨用户访问一律 404（不泄漏存在性） |
| 并发 | 会话级锁（每会话同时只跑一个 turn）+ 全局信号量（`PI_MAX_CONCURRENT_RUNS`，默认 8）+ run 超时（`PI_RUN_TIMEOUT_SECONDS`，默认 600） |
| 限流 | 每用户固定窗口（`PI_RATE_LIMIT_RUNS_PER_MIN`，默认 20），429 + Retry-After |
| 审计 | 追加式按天滚动 JSONL：工具调用、策略决定、压缩，以及**每次注册/登录尝试**（带客户端 IP 和 User-Agent，绝不记密码）。所有字段截断——失败登录路径是攻击者可控的，而 `LoginIn.username` 不限长。现在含 IP 已属个人数据，请给它定保留期 |
| 反向代理 | `PI_FORWARDED_ALLOW_IPS`（默认 `127.0.0.1`）列出可信的 `X-Forwarded-For` 来源；两个 compose 都设为 `172.16.0.0/12` 以覆盖 Caddy 容器。配错**静默失败**——所有客户端被记成代理 IP，基于 IP 的限流塌成一个大桶。永远别设 `*`：uvicorn 会返回客户端伪造的最左条目 |
| 流式 | SSE（`text/event-stream`）：start / text_delta / toolcall_start / toolcall_end / compaction / turn_end / error / done |
| 存储 | SQLAlchemy 2.0 async + MySQL（aiomysql）或 PostgreSQL（asyncpg）；`PI_DATABASE_URL` 必填，schema 由 Alembic 管理。原始 run 轨迹追加写 `PI_TRAJECTORY_PATH`（另有 `runs` 表供结构化查询）。会话工作区在回合结束归档为 tar.gz + 差异元数据（`PI_ARCHIVE_DIR`，可选 MinIO 经 `PI_ARCHIVE_S3_*`，`PI_ARCHIVE=0` 关）——持久化失败只记日志，绝不让 run 失败 |
| 工作区 | 每用户沙箱目录 `PI_WORKSPACE_ROOT/<user>/` |
| 可观测 | 带 request id 与延迟的 JSON 访问日志；`/healthz`、`/readyz`；全量审计链路 |

API（OpenAPI 见 `/docs`）：`POST /v1/auth/register|login|logout`、`GET /v1/me`、
`GET/POST /v1/sessions`、`GET /v1/sessions/{id}`、`DELETE /v1/sessions/{id}`、
`GET /v1/sessions/{id}/messages`、
`GET /v1/sessions/{id}/trajectory`（最近一次原始 run，属主校验）、
`GET /v1/trajectory/{run_id}`（回放单次 run）与 `GET /v1/admin/trajectory/{run_id}`
（管理员跨用户回放）、
`POST /v1/sessions/{id}/runs`（SSE）、`GET /v1/usage`、`GET /v1/admin/users`、
`PATCH /v1/admin/users/{u}`、`POST /v1/admin/users/{u}/revoke`（`PI_ALLOW_REGISTER=0` 关开放注册）、
`GET /v1/admin/audit`（`?day=YYYY-MM-DD&user=&tool=&event=`，DB 为主 jsonl 兜底）、
`GET /v1/admin/stats`（今日聚合 + 规模）、`GET /v1/admin/usage`（每用户月度）、
`POST/GET/DELETE /v1/files` + `/v1/files/commit` + `/v1/files/{id}/url`（MinIO 预签名文件管线，sha256 用户级去重）。
官方异步 SDK：`from pi.client import PiClient`（认证/会话/消息/轨迹/用量 + SSE run 流，`trust_env=False`）。
零构建 Web UI（单文件 vanilla JS）：用户端 `GET /ui/app.html`
（assistant markdown 渲染、按会话切换模型、思考过程展示、文件上传下载面板、
注册 tab 可经 `PI_ALLOW_REGISTER=0` 隐藏）、轨迹查看器 `GET /ui/trajectory.html?session=<id>`、
管理控制台 `GET /ui/admin.html`（概览/用户+配额/审计）。

## 可观测、计量与韧性

| 关注点 | 实现 |
|---|---|
| 追踪 | `PI_TRACER=jsonl`（默认，内置 span 文件）或 `otel`（OpenTelemetry 桥接，装 `pi-py[observability]`）；span：`agent.run` / `llm.call` / `tool.call`，带耗时与状态 |
| Prometheus 指标 | `GET /metrics`（text 格式 0.0.4，`prometheus-client` 经 `pi-py[observability]`）：runs/llm/tool/memory/HTTP RED 计数器 + 直方图 + `pi_runs_in_flight` gauge；词法记忆降级与模型链回退的降级计数器，另有沙箱健康序列（`pi_sandbox_create_failures_total` / `pi_sandbox_command_timeouts_total` / `pi_sandbox_close_failures_total` / `pi_sandbox_create_duration_seconds`）。每个族都从既有记录点投影（tracer span 关闭 / 轨迹 / RunManager / 中间件）——标签有界（model/tool/status/route，绝不含 user 或 session）。`PI_METRICS_TOKEN` 门控（错 token → 404）；`PI_METRICS=0` 关闭。抓取配置：`deploy/prometheus.yml` |
| 成本计量 | 每个完成的 run 记录 token + 估算成本（按模型价格表，`PI_PRICES_FILE` 可覆盖）；`GET /v1/usage` 返回按模型月度拆分 |
| 配额 | 每用户月度 token 配额（`PI_DEFAULT_QUOTA_TOKENS`，默认 100 万）；耗尽 → HTTP 402 |
| 模型回退 | `PI_FALLBACK_CHAIN="openai/qwen3.8-max,openai/qwen3.8-flash,openai/qwen3.6-flash"`；瞬时错误（连接/超时/429/5xx）指数退避重试（2x，0.5s 基数）再降级；流播中途失败绝不重放；非瞬时错误直接上抛 |
| 韧性 | 计量/审计/回调失败只记日志，绝不让 run 失败 |

## 部署加固：Redis、沙箱、Docker

| 关注点 | 实现 |
|---|---|
| 分布式限流 + 会话锁 | `PI_REDIS_URL=redis://...` 把两者切到 Redis（INCR+EXPIRE 窗口 / SET NX EX 锁）；不配则退化为进程内实现，仅在单实例下语义正确 |
| 令牌撤销 | `POST /v1/auth/logout` 把该 token 的 jti 黑名单至过期；`POST /v1/admin/users/{u}/revoke` 提升用户纪元使其全部 token 失效（1s 保守窗口）；禁用账号在登录与每次请求都拒绝 |
| MySQL | `PI_DATABASE_URL=mysql+aiomysql://user:pass@host:3306/pi_py`（装 `pi-py[mysql]`）——引擎自动加 `charset=utf8mb4` 与连接回收 |
| PostgreSQL | `PI_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/pi`（装 `pi-py[postgres]`）——同一 schema 与 Alembic 路径 |
| 工具沙箱 | `PI_SANDBOX=docker` 在容器里跑 bash（温池、`--network none`、cgroup 限额）。`PI_SANDBOX=cubesandbox` 走 **CubeSandbox microVM**（E2B 兼容 SDK，`PI_CUBE_API_KEY`，宿主需 KVM/嵌套虚拟化）——会话 workspace 在 VM 内，命令包 GNU `timeout`（124 → `timed_out`，零残留），非零退出码透传，>10MB workspace 装载拒绝并给出可操作报错。VM 生命周期：惰性创建（纯聊天回合零 VM）、每会话复用池（`pool_hits`）、LRU + 空闲 TTL + 宿主内存压力自适应驱逐（`PI_SANDBOX_POOL_*`）。其他任何值**启动即报错**——曾经会静默落到"在应用进程内执行命令"，命令继承 `PI_JWT_SECRET` 和 `PI_DATABASE_URL`。设计/手册：`docs/cube-sandbox-design-notes.md` / `docs/production-deployment.md` |
| 温容器池 | docker 模式默认按 workspace 建池，**惰性创建**（一个回合的首次 bash 调用才建/复用容器，纯聊天回合一个都不建），命令经 `docker exec` 进入（没有每次调用的容器生命周期开销），闲置条目按 `PI_SANDBOX_IDLE_TTL`（默认 600s）回收，达到 `PI_SANDBOX_POOL_MAX`（默认 16）按 LRU 驱逐（全忙时允许临时超额），容器意外消失透明重建一次。`PI_SANDBOX_POOL=0` 恢复旧的一次一容器行为。温容器按 `PI_SANDBOX_WARM_LIFETIME`（默认 2h）自毁，应用崩溃也不会留下永久孤儿容器 |
| 容器资源限额 | `PI_SANDBOX_MEMORY`（1g）/ `PI_SANDBOX_PIDS`（256）/ `PI_SANDBOX_CPUS`（1.0）。Docker 默认**完全不限额**（`Memory=0`、`NanoCpus=0`、无 `PidsLimit`），而注册是开放的——无上限容器让任何账号一条命令打爆宿主，`--network none` 管不住资源耗尽。四条建容器路径（冷 CLI、冷 Engine API、温 CLI、温 API）统一生效；`--memory-swap` 与 `--memory` 等值（docker 默认允许 2 倍 swap）。让 `PI_SANDBOX_MEMORY × PI_MAX_CONCURRENT_RUNS` 远低于物理内存（随仓配置：1g × 8 = 16 GiB 里的 8 GiB）。`PI_SANDBOX_USER` 默认取**应用自身 uid:gid**，bind mount 写出的文件应用始终可读可删——裸金属 `0:0`、compose `10001:10001`，无需配置 |
| 配置错误响亮失败 | 两个"配错会静默去隔离"的设置改为拒绝启动或自纠正：无法识别的 `PI_SANDBOX` 在 `create_app` 抛错（合法的本地路径会打一条写明后果的 warning）；`server_policy()` 强制 `path_sandbox`/`redact`，`PI_POLICY` 文件只能加规则不能减 |
| 容器镜像 | 多阶段 `Dockerfile`（非 root uid 10001、HEALTHCHECK `/healthz`、迁移打进 `/opt/pi-py`）；`docker-compose.local.yml`（MySQL+Redis+Milvus 基础设施）与 `docker-compose.cloud.yml`（生产 app+caddy，DB 云端托管） |
| 压测 *(历史，继承)* | `tools/loadtest.py`：前任维护者跑过——自托管 PG+Redis 上 50 用户 × 2 轮，100/100 成功，p95 ~2.4s，p99 ~3.1s（含 PBKDF2 注册）；同会话并发被锁正确串行化。**未在托管 MySQL+Redis 部署上重测。** 工具把分位数打到 stdout，不写结果文件 |
| 沙箱容量 *(本部署实测)* | `tools/sandbox_bench.py`，4 vCPU / 16 GiB ECS，`PI_SANDBOX=docker` 温池，`python:3.12-slim`，断网。吞吐在 **N=4 并发 workspace 达到 ~52 exec/s** 并保持到 N=64（宿主 CPU 96-100%），p50 延迟即纯排队深度：44ms @ N=1、154 @ 8、292 @ 16、567 @ 32、1162 @ 64——**每档零失败**。重命令（`python -c`）降到 ~42/s。内存不是瓶颈（每温用户 ~26 MiB）；同时冷启 64 容器需 3.0s。Engine API 传输仅比 `docker exec` 快 1.3 倍/次——上限在 dockerd/runc 而非 CLI 传输。注意 `PI_SANDBOX_POOL_MAX=16` **不拦**并发洪峰——它记"allowing temporary overshoot"并建满 64 个。随仓配置 `PI_MAX_CONCURRENT_RUNS=8` 先截住并发回合，沙箱实际 ~154ms p50，6 倍余量。**该基准在每容器限额存在前测得**——尤其宿主 CPU 饱和数据早于 `PI_SANDBOX_CPUS=1.0`，收紧限额后请重跑 |
| 多实例验证 *(历史，继承)* | 两个实例共享一个 Redis/DB：跨实例会话锁确认（观察到带预期 TTL 的 Redis 锁键），compose 加 `replicas` 旋钮 |
| TLS | `docker compose --profile tls up -d` 用 Caddy 前置应用（自动 HTTPS，SSE 友好）；见 `deploy/Caddyfile` |

```bash
# 全栈
docker compose up -d
docker compose exec app pi-py migrate

# 或手动：自己跑 redis + 数据库
export PI_DATABASE_URL="postgresql+asyncpg://pi:pass@localhost:5432/pi"
export PI_REDIS_URL="redis://localhost:6379/0"
export PI_SANDBOX=docker
pi-py migrate && pi-py serve --host 0.0.0.0 --port 8300
```

`pyproject.toml` 里的生产 extras：`pip install pi-py[production]`
（asyncpg + aiomysql + redis + alembic + opentelemetry）。

## 云端部署（火山引擎）

应用跑在 4 vCPU / 16 GiB ECS，MySQL 8.0.43 + Redis 7.0.15 为托管实例，经 VPC 私网访问
（宿主上没有数据库容器）：

```bash
cp deploy/env.cloud.example .env   # 填 MYSQL_PASSWORD / REDIS_PASSWORD / PI_JWT_SECRET / 模型 key
docker compose -f docker-compose.cloud.yml up -d --build
```

- `docker-compose.cloud.yml`：一次性 `migrate` 服务（alembic，幂等）先于 `app` 启动
  （`service_completed_successfully` 门槛）；可选 Caddy TLS 前端走 `tls` profile
  （`deploy/Caddyfile.cloud`，域名经 `DOMAIN` 环境变量）。**沙箱默认 `cubesandbox`**——
  需 `PI_SANDBOX_TEMPLATE` + `PI_CUBE_API_KEY`，配一半 = fail-closed，绝不静默回退。
- 镜像把 `alembic.ini` + `migrations/` 打进 `/opt/pi-py`（`PI_ALEMBIC_DIR`），没有仓库
  checkout 也能 `pi-py migrate`；运行层随带 `aiomysql` 与 `asyncpg`。
- MySQL 用最小权限 `pi` 账号（仅 `pi_py.*` 的 DML+DDL）；4 vCPU 的规模参数
  （`PI_MAX_CONCURRENT_RUNS=16`，沙箱注意事项）与完整手册：**`deploy/cloud-deploy.md`**。
- 托管 Redis 常禁用 `KEYS`——用 `SCAN` 排查（`redis-cli --scan --pattern 'prod:*'`）。

## 目录结构

```
src/pi/
  models.py                 基于 block 的消息模型（pydantic）
  prompt.py                 系统提示词
  llm/                      base, registry, openai, anthropic, fake, fallback, think_filter
  agent/                    loop.py + events.py + compaction.py
  tools/                    base + bash/read/write/edit/grep/find/ls + files/memory/mcp/skill/subagent/rag + registry + sandbox
  rag/                      企业知识库：parser/chunker/ingest/retriever/eval/integration
  security/                 policy.py + audit.py + redact.py
  observability/            tracing.py + metering.py + prices.py
  server/                   config, db, auth, cache, ratelimit, runner, app (FastAPI)
  cli.py                    argparse 入口：serve / migrate
migrations/                 Alembic 版本（0001_initial … 0008_rag）
tests/                      pytest 套件：529 例，跑在真实 MySQL + Redis 栈（LLM/embedding/Milvus 用替身）
tools/loadtest.py           SSE 压测
tools/seed_testdb.py        给 *_test 库灌可复用种子（幂等，拒绝生产库）
tools/sandbox_bench.py      docker 池容量扫描（并发用户数 → exec 延迟）
deploy/                     Caddyfiles、云端 runbook、.env 模板
```

## 测试

```bash
pip install -e ".[dev]"
python -m pytest -q          # 521 passed, 8 skipped
```

套件跑在真实本地栈上（MySQL `pi_py_test` + Redis db1，2026-09-27 起与生产同栈——
见 `deploy/docker-compose.local.yml` 或 CI 的 service containers）。`tests/conftest.py` 在
import `pi` 之前钉死 `PI_SANDBOX` / `PI_POLICY` 为空、`PI_TRACER=noop`，仓库根的生产 `.env`
不会泄漏进测试进程。LLM / embedding / Milvus 用替身（FakeProvider 等），少量用例直测一次性
SQLite 文件，异步测试用 `asyncio.run()` 包裹（无 pytest-asyncio 依赖）。

### 共享测试库

对真实 MySQL + Redis 手工联调时，`.env.test` 把一切指向独立库与命名空间（`PI_DATABASE_URL` →
`pi_py_test`、`PI_REDIS_NS=test`、`PI_WORKSPACE_ROOT` → `~/.pi-py/workspaces-test`、
`PI_AUDIT_PATH` → `~/.pi-py/audit-test.jsonl`）。`pi/__init__.py` 只自动加载 `.env`，
测试覆盖需要显式 source——顺序很重要，因为已存在的环境变量永远赢：

```bash
set -a; . ./.env; . ./.env.test; set +a
pi-py migrate                    # 只对 pi_py_test 建表/升级
python tools/seed_testdb.py      # 幂等；--reset 先清空，--users N 定池子大小
pi-py serve --port 8398          # 8300 留给生产
```

种子账号（密码来自 `--password`，默认 `pi-test-123`——**仅测试库，生产没有默认密码**）：
`admin`（经 `UserRepo.set_admin` 授予，唯一提权路径）、`alice`/`bob`/`carol` 普通用户
（各带一个已填充和一个空会话 + 一条用量记录）、`overquota`（`quota_tokens=1000` 对 1540
已用 token → 402）、`disabled`（`is_active=0` → 401）。脚本在 `PI_DATABASE_URL` 不含 `*_test`
库名时拒绝运行，忘 source 也不会伤到真实数据。注意用量是月度窗口，种子记录的时间戳是当天。

## 安全须知

`PI_SANDBOX` 未设置时，工具以服务进程自身的权限执行**并继承其环境**——包括
`PI_JWT_SECRET` 和 `PI_DATABASE_URL`。策略引擎是安全网，不是沙箱：它管路径收敛和命令黑名单，
但拦不住读环境的命令（`env`、`printenv`、`/proc/self/environ`、任何解释器一行流）。
审批闸门挡的是失误；只有系统边界挡得住恶意。

因此随仓 `.env` 设 `PI_SANDBOX=docker`（断网、每容器内存/pids/cpu 上限、以应用自身 uid 运行）。
已知的残留缺口：**冷 CLI** 路径（`PI_SANDBOX_POOL=0`）下超时只杀了本地 `docker run` 客户端——
SIGKILL 无法转发进容器，`--rm` 只在容器**自己退出**时生效，所以容器会继续跑完自己的命令。
（读代码确认，未实测复现；默认温池走 `docker rm -f`，Engine API 路径 POST `/kill`，两者都能清掉。）
面对恶意多租户，把工具执行放进 microVM——云端 compose 现在**默认** `PI_SANDBOX=cubesandbox`
（配一半 = fail-closed，绝不静默回退；见 `deploy/cloud-deploy.md` §4）。永远不要提交 `.env`
（已 gitignore）；生产环境谨慎轮换 `PI_JWT_SECRET`。

## License

[MIT](LICENSE).
