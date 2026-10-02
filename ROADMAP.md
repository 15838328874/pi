# pi-py 状态与路线图（ROADMAP）

> 项目现状、未完成事项、后续阶段开发计划的唯一入口。更新日期：2026-10-01。
>
> **文档地图**（三个文档各管一段，知识点不重复）：
>
> | 文档 | 定位 | 什么问题看它 |
|---|---|---|
| `README.md` | 门面 | 这是什么、怎么装、怎么跑（快速上手入口） |
| `ARCHITECTURE.md` | 技术手册 + 叙事 | 每个模块每个函数、配置全表（§13）、坑清单（§17）、差距清单（§19）；设计取舍、测试样例、术语表、实测数据（原 PROJECT_GUIDE 已并入） |
| `ROADMAP.md` | 状态与路线图 | 什么做完了、什么没做、下一步做什么（含环境区分表） |
| `docs/`（专项文档） | 专项文档 | 沙箱四件（设计笔记/文件管线设计/生产部署/就绪审计）+ run 持久化与交互式 run + 媒体输出（图片/视频/知识库检索）设计存档——不重复核心四文档内容 |


## 1. 当前能力（已完成、已验证）

| 能力 | 位置 | 验证 |
|---|---|---|
| Agent loop + 12 内置工具（+ rag_search 可选）+ 错误回喂自纠正 | `src/pi/agent/` `src/pi/tools/` | 528 单测 |
| **能力授权（P0-1）**：allow/deny_capabilities 策略键（allow 子集语义、未声明能力的 MCP/skill 工具 fail-closed） | `security/policy.py` `tools/base.py` | 7 单测（TestCapabilities） |
| **幂等重放（P0-2）**：checkpoint 带 completed_tools 账本，resume 重放已完成工具而非重执行副作用 | `agent/loop.py` | 2 单测（test_durable） |
| LLM 接入层（openai/anthropic/fake）+ 降级链 + 退避重试 | `src/pi/llm/` | 单测 + 真实模型（qwen3.8-flash/max） |
| 上下文压缩（摘要 + 保留尾部，非破坏落库） | `agent/compaction.py` | 单测 |
| Docker 沙箱（本地形态）+ cgroup 限额 + 预热池 + 断网 | `tools/sandbox.py` | 压测 ~52 exec/s |
| 多租户（JWT、配额、限流、审计、会话隔离、令牌撤销） | `server/` | 单测 |
| SSE 流式 + think_filter | `server/runner.py` | 单测 + 真实浏览器 |
| 可观测（tracing jsonl/otel + Prometheus + 计量成本） | `observability/` | 单测 |
| 多实例（Redis 锁/限流/撤销） | `server/cache.py` | 本地 Redis 实测 |
| Multi-Agent（递归子代理 + max_depth） | `tools/subagent.py` | 单测 |
| P1 统一轨迹（canonical 事件日志 + ts 墙钟） | `agent/trajectory.py` | 单测 |
| P2 durable execution（checkpoint + resume） | `agent/loop.py` | 单测 |
| P3 记忆：episodic（压缩落库）+ semantic（memories 表 + 向量检索 Milvus/embedding，词法兜底） | `server/db.py` `server/vectorstore.py` `llm/embedding.py` | 单测 + 本地 Milvus 实测 |
| P4 eval harness（任务集/判分/报告/A-B/CLI） | `evals/` | 单测 |
| **RL 数据飞轮**（rollout → reward → filter → SFT/RLVR JSONL 导出） | `evals/rollout.py` 等 + `pi-py eval rollout` | 单测 |
| **MCP 工具源**（stdio client + fail-soft + 生命周期） | `tools/mcp.py` `tools/registry.py` | 单测（假 MCP server） |
| **Skills**（SKILL.md 加载 + 索引注入 prompt + use_skill + 脚本走沙箱） | `tools/skill.py` | 单测 |
| **轨迹落盘 + 轨迹视图**（jsonl 按天滚动 + 查询端点 + 单文件时序图前端） | `server/trajectory_store.py` `/ui/trajectory.html` | 单测 + 真实浏览器 |
| **Web 前端**（零构建三件套；用户端含 markdown 渲染/模型选择器/思考过程折叠/文件上传面板） | `server/static/*.html` | Playwright 无头浏览器实测 |
| **管理 API**（用户管理/配额/禁用/强制下线/审计过滤/stats/usage） | `server/app.py` | 单测 + 冒烟 |
| **沙箱生产化**（CubeSandbox microVM + GNU timeout + 退出码透传 + 10MB 装载上限 + 三层 VM 泄漏防线 + **懒加载/复用池/内存自适应回收生命周期**） | `tools/sandbox.py` `server/runner.py` | 真机故障注入探针 + 企业 eval 5/5 |
| **会话闭环归档**（turn 基线快照 + 结束 tar.gz + 差异元数据 + MinIO 惰性接口） | `server/archive.py` | 9 turns 实测 diff 精确 |
| **沙箱健康指标**（创建失败/命令超时/close 失败/创建耗时 4 系列） | `observability/metrics.py` | 真实任务实测 |
| **文件管线 P0/P1/P2**（MinIO 预签名直连 + sha256 用户级去重 + files 表 + list_files/fetch_file 工具 + 沙箱能力镜像） | `server/storage.py` `server/db.py` `tools/files.py` | 528 单测 |
| **轨迹结构化落库**（runs 表 + `/v1/trajectory/{run_id}` 回放 + `/v1/admin/trajectory/{run_id}` 跨用户） | `server/db.py` `server/app.py` | 单测 + 实测 |
| **审计结构化查询**（audit_events 表双写，jsonl 仍是合规底稿） | `server/db.py` `security/audit.py` | 单测 + 实测 |
| **官方 SDK**（异步客户端：SSE 流式解析、PiError 语义、trust_env=False） | `src/pi/client.py` | 单测 + 真实模型实测 |
| **企业 RAG 知识库**（零耦合内核 + 解析/语义切块 + 向量×BM25→RRF→rerank 三级降级 + 引用溯源 + user 级 ACL + `rag_search` 工具 + `pi-py rag` CLI） | `src/pi/rag/` `src/pi/tools/rag.py` `src/pi/rag/integration.py` | 260 RAG 单测 + 真栈 integration（真实云 embedding+rerank） |

**环境（2026-09-27 起统一，不再有 demo 环境）**：
- 基础设施三件套：**MySQL 8 + Redis + Milvus**（本地 Docker/native，生产云端托管），测试与生产同构。
- 本地测试环境：`deploy/docker-compose.local.yml` + `deploy/env.local.example` + `deploy/local-dev.md`。
- 所有验证数据都是真实数据（真实模型 qwen3.8-flash/max + 云 embedding），不跑 fake/demo。
- 单元测试的 DB/缓存也走本地 MySQL/Redis（`pi_py_test` 库）；外部服务（LLM、embedding、Milvus）
  在单测中用测试替身，真实链路由 `integration/` 验证——这是测试分层，不是 demo 环境。

## 2. 教训速查（完整故事见 `ARCHITECTURE.md` §17）

| # | 一句话教训 |
|---|---|
| 1 | API 返回形状写进测试；前端异步错误必须可见，禁止静默 catch（blocks 形状 bug，三天悬案） |
| 2 | 静态 HTML 必须 `no-cache`，否则修复被浏览器缓存吞掉 |
| 3 | 前端 JS 一个 `await` 都不能省；协议层 curl 全通 ≠ 页面没问题，页面要 Playwright 验证 |
| 4 | 生产 `.env` 与本地严格隔离；本地用 `.env.local` |
| 5 | UI 布局用计算样式程序化断言，别靠肉眼文字描述迭代 |
| 6 | LLM 会对模糊输入自作主张调工具——提示词已约束，闲聊不跑命令 |
| 7 | 文档会过时代码不会：状态以代码为准；每知识点只在一个文档详述（四个文档各有分工） |
| 8 | 真实模型花钱：UI 调试别用真实模型反复回归 |
| 9 | 跨分支 API 变更合流后跑**全套**：两边各跑子集全绿，合流才暴露 ToolContext 断裂（4 例挂） |

## 3. 未完成事项

| 项 | 说明 | 优先级 |
|---|---|---|
| 管理员会话浏览器 | 管理员查看任意用户会话/轨迹（需一批 admin_* 端点 + 管理台页面） | 中 |
| eval 补全 | flywheel 自动抽取任务、regress、badcase 自动归因 | 中 |
| run 持久化生产级改造 | write-ahead + 逐轮落库 + checkpoint 接 server + runs 状态机 + resume 端点 + 多实例锁心跳（TTL 120s 续期）。**第一、二步已落地（2026-10-02：write-ahead + 逐轮落库，硬崩溃丢失窗口 ≤ 当前轮，用户说"继续"即可续跑）**；剩余（checkpoint/resume 端点、状态机）按主流产品形态降级为**可选增强**，暂缓——设计与取舍记录见 [`docs/run-durability-design.md`](docs/run-durability-design.md) | 低 |
| 交互式 run（中途提问确认 + 计划目录产品化） | 模型中途发 questions 事件挂起、用户经 answer 端点回复后从断点续跑（对齐 Claude Code 式协作体验）；plan 文件渲染计划卡。**依赖** run 持久化改造（checkpoint/resume 是前置，挂起=暂停的 run）。设计见 [`docs/run-durability-design.md`](docs/run-durability-design.md) §11，暂缓实施 | 低 |
| 媒体输出（图片/视频生成 + 知识库视频检索） | media payload 通道（toolcall_end 加 images 字段 + 前端 fetch→blob 渲染）；视频生成走异步 job 模式（提交即返回 + 后台轮询）；知识库视频走文本代理向量化（ASR 转写分段 + show_media 工具）。设计见 [`docs/media-output-design.md`](docs/media-output-design.md)，暂缓实施。实施顺序：共用 payload 通道 → 视频生成 → 知识库检索（摄取管线最重） | 低 |
| 沙箱平台告警规则 + 推送通道 | `/readyz` 已带 `sandbox` 硬检查（平台挂 → 503，已完成），但**没有任何东西在消费它**：机器上未部署 Prometheus/Alertmanager，`deploy/prometheus.yml` 只是示例且 targets 写的是 compose 网络里的 `app:8300`（本机部署对不上），也**没有任何告警规则文件**。要补：① 起 Prometheus 抓 `/metrics`（targets 按实际部署改）+ 告警规则（如 `rate(pi_sandbox_create_failures_total[5m]) > 0`、`up == 0`、`readyz != 200`）；② Alertmanager 接推送（微信/钉钉/邮件）。指标已现成：`pi_sandbox_create_failures_total` / `_command_timeouts_total` / `_close_failures_total` / `_create_duration_seconds` | 中 |
| 管理台平台健康卡片 | `/v1/admin/stats` 目前只返回 `today/users/sessions`，运维看不到平台状态。补：cubelet 是否在线、模板是否 READY、近期建沙箱失败数、`/readyz` 各项，做成 `admin.html` 上的状态卡片 | 中 |
| `/metrics` 加访问 token | 当前 `PI_METRICS_TOKEN` 为空 → `/metrics` 完全开放（启动日志已明确告警："Fine behind a private network, a leak on a published port"）。本机只绑 127.0.0.1 暂时无碍，但一旦挂反代就可能泄漏用量/并发等运行数据。同时改 `prometheus.yml` 的 `authorization.credentials_file` 配套 | 中 |
| 记忆向量重建命令 | 换 embedding 模型后 RAG 侧有 `pi-py rag rebuild-index --user <id>`，但**记忆向量 `pi_memories` 没有重建入口**——`vectorstore.py` 注释说它是"可重建索引"，却无对应 CLI。换模型时旧记忆向量全部失效且无报错（静默降级到词法）。补一个 `pi-py memory rebuild`（或复用 RAG 的重建范式：从 SQL 真相源 re-embed 到 Milvus）| 中 |

## 4. 后续阶段开发

### 4.1 RAG（企业知识库）—— ✅ 已交付（2026-10-01）

> 需求来源 [`RAG外派对接文档.md`](../RAG外派对接文档.md)。铁律「**先建评测，再调检索**」全程遵守。
> 内核 `src/pi/rag/` **零依赖 `pi.*`**（可整目录抽走独立用）；与宿主的耦合只有两个文件：
> `adapters.py`（kernel Protocol ↔ pi 客户端）与 `integration.py`（ToolProvider + runtime 发布）。
> 因此 `pi/tools/__init__.py`、`pi/tools/base.py`、`pi/server/runner.py` **一行未改**。

**交付记录**

| 里程碑 | 内容 | 验证 |
|---|---|---|
| M0 | 内核骨架 + 6 个 Protocol + config + defaults + 评测 harness | 单测 |
| M1 | 解析层（7 后端 + 质量门）+ contextual 切块 + 跨章节合并 | 真实语料验收 |
| M2 | ingest 管道 + rebuild-index + **生产真后端**（MySQL 真相源 / Milvus 可重建投影） | 真栈 integration |
| M3 | 检索：向量 + BM25 → RRF(k=60) → rerank；四级降级 hybrid→bm25→sql_like，每级发噪音不静默 | recall@5 0.833 / mrr 0.900，+rerank 拉到 1.000 |
| M4 | 真栈 A/B 归因（390 chunk / 60 golden / 13 档） | BM25 唯一覆盖 0/60 → 推翻了「BM25 必要」假设；**cross-encoder rerank 才是决定性增益** |
| M5 | pi 对接：`adapters.py` + `RagTool` + `rag/cli.py` + server 接线（`PI_RAG_ENABLED`、rerank 独立 model tag 计量） | 260 RAG 单测 + 真栈 integration 2 passed |

**对接形态（与"设计浓缩"不同之处）**：`rag_search` 不是塞进 `all_tools()`，而是作为**独立
`ToolProvider`**（`pi.rag.integration.RagToolProvider`）注册——`ToolRegistry` 会合并去重所有
provider，工具照样走 policy / audit / tracing / 配额，但宿主工具层保持零改动，
上游合并本分支时工具集无冲突。runtime 由 `integration.install()` 建好后发布到进程单例，
`RagTool` 直接取用，因此 `ToolContext` 也不需要新增字段。

**关键家规（血泪）**：① 通道对称性——embed / 索引 / **rerank** 三阶段文本都必须带 `title_path`，
否则重排看到的信息少于产生候选的阶段，会用更少信息推翻更好的候选（M4 edge_case 回归根因）；
② retriever 与 ingest 必须共享同一个 lexical index 实例；③ **换 embedding 模型必须先重建 Milvus 投影**，
否则静默空间漂移（`tools/rebuild_eval_index.py --check/--apply` 自检索余弦判据）。

**未做/后续**：
- **上传即入库已落地**（2026-10-02 后）：`POST /v1/rag/ingest`（multipart 异步）+ `GET/DELETE /v1/rag/docs` + 前端「知识库」面板，取代原先只能 `--path` 的 CLI 灌库。对接文档 §4.7 的 `--file-id`（经 `FileRepo` + `ObjectStore` 从文件管线取原料）仍是**可选的第二入口**，未做。
- **扫描件/图片 OCR 重解析已接入** PaddleOCR 线上 API（`PI_RAG_HEAVY_PARSER=*`，走 `HeavyParser.parse(path)->str` 协议）；MinerU 自托管待有 ≥16G 内存的机器，按同一协议迁移即可。多模态/GraphRAG 属 v2。
- **借鉴 RAGFlow DeepDoc 的解析思路已落地**（2026-10-02）：复杂 PDF（双栏/扫描/表格）默认路由 PaddleOCR-VL 做版面级解析（pdfplumber 仅作单栏纯文字的快速路径）。路由用三个**通用、与文档无关**的信号——text density（扫描件）、garbled 占比（乱码）、行内最大字符间隙中位数（双栏/表格中缝）；PaddleOCR 输出经 `clean_ocr_markdown` 做 vendor 中立清洗（行内 LaTeX→文本、HTML 表格→pipe 表格、标签剥除），MinerU 换配置即可复用。**A/B 实测**（corpus v2，37 个双栏 case，`evals/reports/ab_20261002_144034.md`）：rerank 后 `recall@1` **0.429 → 0.824**、`hit@5` 0.883 → 0.946、`mrr` 0.672 → 0.878——增益集中在 top1 精度，正是"双栏交错打散答案片段"的修复方向（定性验证：答案片段在 pdfplumber 索引连续命中 0/4，PaddleOCR 索引 4/4）。⚠️ 两组 case 集不同（37 双栏 vs 142 全量），非严格对照，量级待旧解析器同 case 复测。详见 `ARCHITECTURE.md` §21.6「解析层改进的评测验证」。
- 换 embedding 模型的**记忆向量重建命令**仍未补（见 §3）。

**原设计浓缩（保留备查）**

- **顺序铁律：先建评测，再调检索**（golden set + RAGAS 类指标），否则切块/embedding/rerank
  全是盲调。
- **v1 及格线五件套**：解析层（PDF/Word/CSV，轻量后端 pdfplumber/python-docx/openpyxl；
  重后端 MinerU/PaddleOCR 做成独立服务，不进 app 进程——**PaddleOCR 线上 API 已接入**，
  MinerU 待自托管机器）→ 语义切块 + contextual retrieval →
  **混合检索（向量 + BM25，RRF 融合）+ rerank**（纯向量 top-k 不够用）→ 引用溯源 → 权限 ACL
  （复用多租户 user 维度）。
- **形态**：`pi/rag/` 独立内核（parser/chunker/embedder/vectorstore/retriever）+
  `RagTool`（实现 Tool 接口自动享受 policy/审计/配额）+ `pi-py rag ingest` CLI。
- **复用**：embedder + vectorstore 与语义记忆共用一套 Milvus + 云 embedding，别各写各的。
- **v2 才考虑**：多模态（VLM+OCR）、图搜图、GraphRAG。
- 文件管线已落地（MinIO 预签名直连 + sha256 去重 + files 表 + list_files/fetch_file 工具）；
  剩余：磁盘配额（每用户/单文件上限 + 解压炸弹防护）、闲置清理策略、与 RAG 解析层共用 parser。
- **文件产物交付**（模型产出 → 用户下载）：实时下载（workspace 直读）+ workspace 100MB 配额
  已落地；MinIO 持久化、闲置清理、生命周期列为后续方向，完整设计见
  `docs/artifact-delivery-design.md`。

### 4.2 产品化

- SSO/RBAC（现在只有 JWT + admin 开关，缺 OIDC/SAML/LDAP 和分级权限）。
- 前端增强：会话重命名、消息重发/编辑、多会话对比。
- CubeSandbox 已落地（`PI_SANDBOX=cubesandbox`，见 `docs/cube-sandbox-design-notes.md`）；生产部署的
  KVM/嵌套虚拟化前置与 runbook 见 `docs/production-deployment.md`。

## 5. 环境速查

| | 生产 | 本地测试 |
|---|---|---|
| 数据库 | 云端托管 MySQL | Docker mysql:8，`127.0.0.1:3306`（pi/pi_py_local），测试库 `pi_py_test` |
| 缓存 | 云端托管 Redis | 本机 Redis，`127.0.0.1:6379`（测试用 db1） |
| 向量 | 云端 Milvus | Docker milvus standalone，`http://127.0.0.1:19531` |
| 模型 | openai/qwen3.8-flash | 同一云 API |
| 配置 | `.env` | `.env.local`（模板 `deploy/env.local.example`） |
| 编排 | `docker-compose.cloud.yml` | `docker-compose.local.yml` |

启动见 `deploy/local-dev.md`；测试：`.venv/bin/python -m pytest`（自动连本地 MySQL/Redis）；
真实栈集成测试：`PI_INTEGRATION=1 pytest integration/`（见 integration/conftest.py）。
