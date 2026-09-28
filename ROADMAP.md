# pi-py 状态与路线图（ROADMAP）

> 项目现状、未完成事项、后续阶段开发计划的唯一入口。更新日期：2026-09-27。
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


## 1. 当前能力（已完成、已验证）

| 能力 | 位置 | 验证 |
|---|---|---|
| Agent loop + 12 内置工具 + 错误回喂自纠正 | `src/pi/agent/` `src/pi/tools/` | 254 单测 |
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
| **Web 前端**（零构建单文件三件套：用户端 / 轨迹视图 / 管理台） | `server/static/*.html` | Playwright 无头浏览器实测 |
| **管理 API**（用户管理/配额/禁用/强制下线/审计过滤/stats/usage） | `server/app.py` | 单测 + 冒烟 |
| **沙箱生产化**（CubeSandbox microVM + GNU timeout + 退出码透传 + 10MB 装载上限 + 三层 VM 泄漏防线 + **懒加载/复用池/内存自适应回收生命周期**） | `tools/sandbox.py` `server/runner.py` | 真机故障注入探针 + 企业 eval 5/5 |
| **会话闭环归档**（turn 基线快照 + 结束 tar.gz + 差异元数据 + MinIO 惰性接口） | `server/archive.py` | 9 turns 实测 diff 精确 |
| **沙箱健康指标**（创建失败/命令超时/close 失败/创建耗时 4 系列） | `observability/metrics.py` | 真实任务实测 |
| **文件管线 P0/P1/P2**（MinIO 预签名直连 + sha256 用户级去重 + files 表 + list_files/fetch_file 工具 + 沙箱能力镜像） | `server/storage.py` `server/db.py` `tools/files.py` | 254 单测 |
| **轨迹结构化落库**（runs 表 + `/v1/trajectory/{run_id}` 回放 + `/v1/admin/trajectory/{run_id}` 跨用户） | `server/db.py` `server/app.py` | 单测 + 实测 |
| **审计结构化查询**（audit_events 表双写，jsonl 仍是合规底稿） | `server/db.py` `security/audit.py` | 单测 + 实测 |
| **官方 SDK**（异步客户端：SSE 流式解析、PiError 语义、trust_env=False） | `src/pi/client.py` | 单测 + 真实模型实测 |

**环境（2026-09-27 起统一，不再有 demo 环境）**：
- 基础设施三件套：**MySQL 8 + Redis + Milvus**（本地 Docker/native，生产云端托管），测试与生产同构。
- 本地测试环境：`deploy/docker-compose.local.yml` + `deploy/env.local.example` + `deploy/local-dev.md`。
- 所有验证数据都是真实数据（真实模型 qwen3.8-flash/max + 云 embedding），不跑 fake/demo。
- 单元测试的 DB/缓存也走本地 MySQL/Redis（`pi_py_test` 库）；外部服务（LLM、embedding、Milvus）
  在单测中用测试替身，真实链路由 `integration/` 验证——这是测试分层，不是 demo 环境。

## 2. 教训速查（完整故事见 `PROJECT_GUIDE.md` 第四部分）

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
| checkpoint 接 server | 超时/失败后从 checkpoint 恢复（loop 的 on_checkpoint 已就绪，server 未接线） | 低 |

## 4. 后续阶段开发

### 4.1 RAG（企业知识库）——设计浓缩

- **顺序铁律：先建评测，再调检索**（golden set + RAGAS 类指标），否则切块/embedding/rerank
  全是盲调。
- **v1 及格线五件套**：解析层（PDF/Word/CSV，轻量后端 pdfplumber/python-docx/openpyxl；
  重后端 MinerU/PaddleOCR 做成独立服务，不进 app 进程）→ 语义切块 + contextual retrieval →
  **混合检索（向量 + BM25，RRF 融合）+ rerank**（纯向量 top-k 不够用）→ 引用溯源 → 权限 ACL
  （复用多租户 user 维度）。
- **形态**：`pi/rag/` 独立内核（parser/chunker/embedder/vectorstore/retriever）+
  `RagTool`（实现 Tool 接口自动享受 policy/审计/配额）+ `pi-py rag ingest` CLI。
- **复用**：embedder + vectorstore 与语义记忆共用一套 Milvus + 云 embedding，别各写各的。
- **v2 才考虑**：多模态（VLM+OCR）、图搜图、GraphRAG。
- 文件管线已落地（MinIO 预签名直连 + sha256 去重 + files 表 + list_files/fetch_file 工具）；
  剩余：磁盘配额（每用户/单文件上限 + 解压炸弹防护）、闲置清理策略、与 RAG 解析层共用 parser。

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
