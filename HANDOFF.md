# pi-py 交接文档（HANDOFF）

> 写给接手的模型/开发者：本仓库是一个多用户编码智能体服务（Python 版 pi coding agent）。
> 完整架构见 `ARCHITECTURE.md`（1041 行，是权威参考），本文只讲**本会话新增了什么、为什么、以及新环境下一步做什么**。
> 阅读顺序建议：本文 → README.md → ARCHITECTURE.md。

---

## 1. 目标环境（重要差异）

| 维度 | 开发时用的 | 你现在要迁移到的 |
|---|---|---|
| 数据库 | SQLite（仅测试）+ 代码支持 MySQL/PG | **PostgreSQL**（`postgresql+asyncpg://`） |
| 缓存/锁 | 内存后端 | **Redis** |
| 向量库 | **无（词法检索）** | **Milvus** |
| 沙箱 | 无（LocalRunner） | **Docker** |
| 模型 | `fake/demo` + 阿里云 MaaS `openai/qwen3.8-max` | **云模型**（同左，已配置） |
| embedding | **无** | **云 embedding** |

**核心机会**：环境里有 Milvus + 云 embedding，而本会话的语义记忆（semantic memory）用的是**零依赖词法检索**——这是有意留的升级点，接口已经留好（见 §5.3）。

---

## 2. 本会话完成的功能（含文件清单）

按依赖顺序：

| 功能 | 说明 | 文件 |
|---|---|---|
| **Multi-Agent** | 递归子代理委派（复用自身 AgentLoop + `max_depth` 上限，Codex/ZCode 式） | `tools/subagent.py`（新增） |
| **P1 统一轨迹** | canonical 事件日志（RunStarted/LlmCall/ToolCall/Compaction/RunError/RunFinished），eval/replay/debug 的唯一事实源 | `agent/trajectory.py`（新增）+ `agent/loop.py` |
| **P2 durable execution** | Checkpoint 数据模型 + `on_checkpoint` 钩子 + `run(resume_from=)` 断点续传 | `agent/loop.py` |
| **P3a episodic 记忆** | 压缩摘要落库（`compactions` 独立表，非破坏），下一轮复用摘要不再重复总结 | `server/db.py` + `runner.py` + 迁移 0003 |
| **P3b semantic 记忆** | 跨会话长期记忆：`memories` 表 + `remember`/`recall` 工具 + 每轮自动召回注入 | `server/db.py` + `tools/memory.py`（新增）+ `runner.py` + 迁移 0004 |
| **P4 eval harness** | 任务集 + 判分（file/command/tests/judge）+ 报告 + A/B + CLI | `evals/`（整包新增）+ `cli.py` |

**改动文件汇总**：
- 新增：`tools/subagent.py`、`tools/memory.py`、`agent/trajectory.py`、`evals/`（7 个文件）、`migrations/versions/0003_compactions.py`、`0004_memories.py`、`tests/test_{subagent,trajectory,durable,episodic,evals,memory}.py`（6 个）
- 修改：`agent/loop.py`、`server/{db,runner,app}.py`、`tools/{base,__init__}.py`、`cli.py`

---

## 3. 设计原则（为什么这样做——**别推翻**）

这五条是本会话所有决策的底层逻辑，接手的模型请先理解再动手：

1. **loop 只产出状态/事件，持久化交给调用方。** 三个钩子 `on_message`（消息）/ `on_compact`（压缩）/ `on_checkpoint`（断点）把状态吐给 server，loop 保持"傻而纯"。eval、服务、测试各自决定存哪。

2. **非破坏优先。** episodic 记忆用独立 `compactions` 表（`messages` 只增不删），因为删/重写会有 idx 撞车 + 违反"run 原子性" + 丢审计底稿。摘要表是派生数据，`TRUNCATE` 随时可重建。

3. **能不依赖就不依赖。** 测试全用 `FakeProvider` + SQLite（零 API 成本）；语义记忆 v1 用词法检索（零依赖）。**但接口留好了，等有 embedding 时升级。**

4. **改动面最小化。** 比如 P2 的 resume 用 `self._resume_usage`/`self._resume_turns` 种子变量，而不是把 `total`/`turns` 全改成实例状态。

5. **eval 是轨迹的消费者，不是 loop 的功能。** `evals/runner.py` 直接跑 `AgentLoop` 抓轨迹 → 判分，loop 完全不知道 eval 存在。

---

## 4. 当前状态：已验证 vs 未验证

**已验证（Windows + FakeProvider + SQLite 本地 sanity check 全过）**：
- multi-agent：真实模型（qwen3.8-max）跑通过任务拆解/并行/聚合
- P1/P2/P3/P4 的所有逻辑单测（本地用工作区内目录 + FakeProvider 验证过）
- 所有模块 import 干净

**未验证（这就是你迁移后要做的）**：
- ❌ 完整测试套件在真实 Linux + Postgres 下跑（开发时测试用 SQLite，生产是 PG）
- ❌ Redis 锁/限流/撤销
- ❌ Docker 沙箱（`PI_SANDBOX=docker` + cgroup 限额）
- ❌ Milvus / 云 embedding（语义记忆的向量升级）
- ❌ `pi-py migrate` 在 Postgres 上（0001~0004）
- ❌ eval 用真实模型跑 CLI

---

## 5. 新环境下的下一步（按优先级）

### 5.1 装依赖 + 跑迁移 + 跑测试（先确认无回归）

```bash
pip install -e ".[production]"     # asyncpg + aiomysql + redis + alembic + otel
export PI_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/pi_py"
pi-py migrate                      # 0001 ~ 0004
python -m pytest -q                # 预期 ~150 个测试，无回归
```

**重点盯 `test_server.py`**——runner 加载路径改了（每次 run 都调 `latest_compaction`），这是最可能回归的地方。

### 5.2 端到端验证（Postgres + Redis + Docker + 真实模型）

```bash
export PI_REDIS_URL="redis://..."
export PI_SANDBOX=docker          # 关键：多租户文件隔离的前提
export PI_MODEL=openai/qwen3.8-max
export OPENAI_API_KEY=... OPENAI_BASE_URL=...
pi-py serve --port 8300
# curl 走一遍 register → login → session → run（见 README）
```

### 5.3 ⭐ 语义记忆升级到 Milvus + 云 embedding（最该做的新功能）

我留的升级口子在 `server/db.py` 的 `MemoryRepo.search()`——**接口是 `search(user_id, query, k)`，换向量后端只需改这一个方法**，工具和 runner 都不用动。做法：

1. `MemoryRepo.add()` 时：调云 embedding 得到向量，文本存 `memories` 表（源事实），向量 + `memory_id` 存 Milvus（索引）。
2. `MemoryRepo.search()` 时：query 转向量 → Milvus 相似度检索 → 拿回对应文本。
3. `MemoryRow` 表保留为 source of truth；Milvus 只是索引（可重建）。

新增配置项（仿照现有 `PI_*` 环境变量风格）：embedding 端点、模型名、API key、Milvus 连接串。

### 5.4 用真实模型跑 eval

```bash
mkdir -p evals/tasks
# 写一个 task json（见 evals/schema.py 的 Task/ScorerSpec 结构）
python -m pi.cli eval run --tasks evals/tasks --model openai/qwen3.8-max
```

---

## 6. 关键技术细节与坑（交接者必读）

1. **`compact_threshold=0` 是"禁用压缩"**，不是"强制压缩"。loop 里是 `if self.compact_threshold > 0` 才触发。想强制触发给个小的正值（如 1）。

2. **`message_idx` 并行数组（P3a 最微妙处）**。压缩在 AgentLoop 内按**列表下标**（`messages[:-keep_last]`）算，但落库按 **DB idx**。所以 loop 维护 `self.message_idx: list[int | None]`（`_append` 追加 None，压缩时同步截断），才能报出"摘要覆盖到 idx K"。改压缩逻辑时别漏掉这个数组。

3. **`user_db_id`（int）≠ `user_id`（username 字符串）**。`ctx.user_id` 是审计标签用的用户名；DB 外键需要 int `user.id`，所以单独加了 `ctx.user_db_id`。别混用。

4. **`ctx.memory` 由 runner 注入**（和 `ctx.runner`/`ctx.provider` 一个套路，见 `runner.py` 里 `agent.ctx.memory = memory_repo`）。**eval 路径直接构造 AgentLoop，所以 eval 里 `ctx.memory` 是 None**，`remember`/`recall` 会返回"memory not configured"——这是有意的（eval 不该碰真实用户记忆）。

5. **checkpoint 只在有工具调用的 step 后触发**。最后一步（无工具、run 即将结束）不发 checkpoint——这是对的，RunFinished 已经记录了。

6. **`PI_SANDBOX=docker` 是多租户文件隔离的前提**。不设就是 LocalRunner（bash 以应用进程身份跑，能读别的用户目录）。README 的 Security note 和 ARCHITECTURE §17 有完整论述。

7. **`.env` 里有生产 Redis 真实密码**（`redis://zhu:...@...ivolces.com`），已 gitignored，**别提交**。本地 `.env` 里 `PI_WORKSPACE_ROOT=/root/...` 是 Linux 路径（服务器拷贝过来的）。

8. **轨迹（P1）是原始记录，不脱敏**；审计是脱敏投影。这个边界别搞混——eval/replay 要原始数据，审计要脱敏。

---

## 7. 配置要点

`.env`（gitignored）关键项：
- `PI_DATABASE_URL`：新环境改 `postgresql+asyncpg://...`
- `PI_REDIS_URL` / `PI_REDIS_NS`
- `PI_MODEL=openai/qwen3.8-max` + `OPENAI_BASE_URL`（阿里云 MaaS 端点已验证）
- `PI_SANDBOX=docker`（compose 下注意 ARCHITECTURE §17.7 的坑：容器里要能访问宿主 docker）
- 新增（待定）：embedding 端点/模型/key、Milvus 连接串

---

## 8. 验证清单（迁移完成后逐项勾）

- [ ] `pip install -e ".[production]"` 成功
- [ ] `pi-py migrate` 在 Postgres 建出 4 张迁移表 + 无报错
- [ ] `pytest -q` 全绿（重点：`test_server.py`、`test_subagent.py`、`test_episodic.py`）
- [ ] `pi-py serve` 起得来，`/healthz`、`/readyz` 正常
- [ ] 端到端：注册→登录→建会话→SSE run→消息落库
- [ ] 沙箱：`PI_SANDBOX=docker` 下 bash 工具在容器内执行
- [ ] （可选）语义记忆升级 Milvus + embedding 后，跨会话 recall 命中
- [ ] （可选）真实模型 eval run 跑通

---

*交接时的未决问题：语义记忆的向量升级（§5.3）是唯一真正需要"新代码"的项，其余都是验证 + 配置。*
