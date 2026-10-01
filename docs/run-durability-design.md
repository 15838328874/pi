# run 持久化生产级改造——设计存档（未实施）

> 状态：**设计完成、暂缓实施**（2026-10-01）。改动面较大（9 步、跨 server/agent/前端），
> 留作后续优化。实施时以本文档为蓝图，逐 step 独立提交、可独立回滚。
> 前置依赖已就绪：loop 侧 checkpoint/resume 已完整（`agent/loop.py`），P0-2
> completed_tools 幂等账本已落地并有测试——本方案只需接 server 侧，不动 resume 逻辑。

## 1. 背景与口径（问题从哪来）

当前 `RunManager.run_turn`（`server/runner.py:133`）在 run **结束后**从内存 buffer 一次性
`append_many` 落库（runner.py:332-342）。设计文档曾表述为"一次 run 是原子单位：要么整轮
对话成功后一次性持久化，要么失败不留半截状态"。由此引出的质疑：

> **会话到一半意外结束了，下次来难道就没记忆了？**

这是把两个正交的概念混在了一起：

- **原子性（一致性）**：落库的内容必须是一段自洽的对话，不能出现"库里有个执行到一半的
  工具调用"这种用户从没见过的东西
- **持久性（不丢数据）**：中途断了之后，下次还能接上——这靠 checkpoint/resume，与原子性无关

"失败不留半截状态"听起来像"失败 = 全丢"，但失败时用户 prompt 和已流出的消息恰恰是
"发生过"的，必须留。由此定稿口径：

> **「用户见过的必落库，用户没见过的绝不落库」**
>
> 三个机制各管一段：
> - **原子事务**管一致性——库里绝无半截内部状态
> - **逐轮落库**管失败不丢——已定稿的消息在轮边界就落，干净失败零丢失
> - **checkpoint + resume**管崩溃恢复——硬崩溃后从断点续跑，而不是重来

对应三类失败场景：干净失败（超时/异常）靠逐轮落库解决、硬崩溃靠 checkpoint/resume 解决、
"库里有用户没见过的状态"靠原子事务杜绝。三个机制缺一不可、各司其职。

## 2. 设计讨论与取舍记录（为什么是现在这个方案）

### 2.1 失败到底留不留状态？

| 选项 | 结论 |
|---|---|
| 失败全丢（字面的"不留半截状态"） | ❌ 用户的话、已流出的回复全没了，下次来"没记忆"——正是质疑的问题 |
| 失败全留（照单全收） | ❌ 库里出现用户没见过的半截状态，下一轮模型基于假历史继续，比丢数据更糟 |
| **已投递的留、未投递的不留**（定稿） | ✅ 已投递 = 用户 prompt + 已定稿流出的消息；未投递 = 没执行完的工具、没发出的消息 |

注意：半流式的消息（用户看到一半）按标准做法落**整条**——模型确实产出了它，主流聊天
产品（OpenAI 等）都这么处理，客户端断连不应让服务端删历史。

### 2.2 四个机制全做？性能影响

逐机制成本账（一次 run：T 轮、M 条消息，公网 MySQL RTT≈30ms）：

| 机制 | 新增写 | 新增读 | 往返 | 结论 |
|---|---|---|---|---|
| write-ahead | +1 消息行 +1 runs 行 | 0 | +1~2 | 不是新增——这条消息今天就在收尾批量里，只是提前。净成本≈0 |
| 逐轮落库 | 0 行（总行数不变），事务数 1→T | +T 次 COUNT | +T-1 | 数据量不变；COUNT 走索引 |
| 逐轮 checkpoint | +T 次 UPDATE，总字节 **O(T²)** | resume 才 +1 | +T | 唯一有"放大"的：第 k 轮 checkpoint 含 k 轮历史 |
| 状态机 | +1 UPDATE（状态流转） | sweeper 每周期 +1 | +2 | 净增 1 笔 |

量化：T=10 的普通 run 多 ~22 次往返 ≈ +0.6s，但 LLM 每轮 1~10s、run 至少 20s——
**开销 ≤3%，感知为零**；T=40 长 run（吃满 600s 超时）多 ~2.5s，<1%。checkpoint 字节量
T=40 累计约 1~3MB，对 MySQL 毛毛雨，要留意的是单次 UPDATE 的 payload（TEXT 列无碍）。

**并发会不会增加？不会。** 四步改动全部发生在"一次 run 的生命周期内部"——会话锁
（Redis SET NX）、全局信号量（PI_MAX_CONCURRENT_RUNS=8）、连接池的并发模型一个字都不动。
变化的是每次 run 内部的 DB 往返次数，不是并发度。反直觉的收益：逐轮 flush 让内存 buffer
峰值变小（不再攒整个 run），**内存占用下降**。

**结论：1+2+4 一起做，3 单独做并节制。** 前三个净成本接近零；checkpoint 的 O(T²) 是
唯一要节制的——解法：异步 + fail-soft（丢了最新一份只是恢复点退几轮）+ 本场景不节流
（每工具轮一次天然 ≈15s 间隔）。一个必须想清楚的坑：今天收尾 flush 失败是在 run
跑完之后失败（用户看完全程，历史没存上，最恶心）；逐轮落库后 DB 故障会中途冒出——
对消息这类核心数据，正确姿势是**立刻终止 run 并报错**（数据安全优先），不是 fail-soft。

### 2.3 checkpoint 按轮次还是按时间？主流实践

| 系统形态 | 代表 | 节奏 | 语义 |
|---|---|---|---|
| Durable workflow | Temporal/Cadence | 每个状态迁移**同步**写（事件溯源，写完才继续） | exactly-once，最重 |
| 流计算 | Flink/Spark | 按时间（barrier 对齐）+ 优雅停机强制写一次 | at-least-once |
| Agent 框架 | LangGraph 等 | 每个 node/turn 结束存 | 近似 exactly-once |
| 游戏/编辑器 autosave | 边界模糊 | 事件边界 + 时间兜底混合 | best-effort |

规律：**有自然边界（turn/node/状态迁移）的系统都用边界触发，只有没有边界的系统才用
时间。** 时间触发在 agent 循环里有个致命别扭：定时器到点正好在工具执行中或 LLM 流播到
一半，无法安全快照——要么打断执行（危险），要么等 turn 结束再存（退化成"按轮次+最小
间隔"）。

本系统的结论：**轮次边界是唯一安全的快照时刻，时间只是叠加其上的节流阀。** turn 平均
10~15s（LLM 主导），"每轮存一次"本就约等于"每 15s 存一次"，再加定时器是多余复杂度。
时间真正该出场的地方是**优雅停机时强制存一次**（Flink 标准做法，部署重启/WSL 重启都是
优雅停机）——作为实施时的补充触发器。

**为什么不需要 Temporal 那么重**：Temporal 每步同步写是为了 exactly-once（worker 任何
一步挂掉、activity 有外部副作用需补偿）。本场景：工具副作用 = 读写文件（天然幂等）+
沙箱内 bash（隔离+超时）；挂掉的代价 = 最多重执行最近 1 轮的未完成工具（P0-2 账本把
已完成的重放掉了）；语义上 at-least-once 就够，exactly-once 是过度设计。取 **LangGraph
的边界触发 + Flink 的停机快照**，避 Temporal 之重。

### 2.4 多实例：队列与锁（v1 → v2 的修订过程）

质疑："队列用多消费者，要是起了好几个服务呢？" 核对结论：**队列是"每个 run 一个
消费者"（进程内，生命周期 = run），不是全局多消费者；跨实例互斥的唯一机制是 Redis
会话锁（多实例必配，README 已约定）。** 但深入核对发现 v1 锁设计有两个真实缺陷：

1. **sweeper 用 `cache.get` 探锁是错的**——RedisBackend 的 `get` 读 `ns:kv:{key}`
   （cache.py:127-129），锁在 `ns:lock:{key}`（cache.py:106-115），根本探不到。
2. **锁 TTL=timeout+60（最长 660s）且无续期**——实例 A 崩溃后锁最多再占 11 分钟，
   期间 resume 一直被"another turn is already running"挡住，这是崩溃恢复的最大障碍。

| 选项 | 结论 |
|---|---|
| 长 TTL 无续期（现状） | ❌ 崩溃后 resume 被挡最多 660s |
| 短 TTL + 心跳续期 + 失锁即中止（定稿） | ✅ TTL 120s、30s 续期；崩溃后 ≤120s 锁过期、sweeper 30s 内标 crashed、resume 最坏等 ≈150s |
| fencing steal（resume 强抢锁） | ❌ 需要锁令牌 + 更复杂的竞态处理；短 TTL 已把等待压到可接受，steal 是过度设计 |

定稿语义：锁存在 ⇔ 持有者最近 120s 内续过期 ⇔ 活着。续期失败 = 失去互斥 = **立即中止
run**（ErrorEvent "lost session lock"，finish status=failed）——这是分布式锁的标准纪律。
进程被暂停 >120s 的极端边界：本实例中止、sweeper 标 crashed，比静默双写安全，文档化即可。

### 2.5 其他小取舍

| 决策点 | 选项 | 选了谁 | 为什么 |
|---|---|---|---|
| checkpoint 写时机 | 同步阻塞 turn / 异步 fire-and-forget | **异步 + fail-soft** | 丢最新一份只是恢复点退几轮；同步写阻塞 turn 不值得（Temporal 才需要） |
| checkpoint 存储 | Redis / MySQL 列 / 独立表 | **runs 表 JSON 列** | MySQL 是唯一事实源（仓库惯例）；checkpoint 本质 latest-wins，列即可；独立表多余 |
| resume 触发 | 自动续跑 / 显式"继续"按钮 | **显式按钮** | 自动续跑可能产生用户没要求的副作用；用户决定是否继续 |
| 按钮位置 | 独立入口 / error 横幅 | **error 横幅** | 错误发生时正是用户想续跑的时机；独立入口需额外 UI + 跨页状态同步 |
| 状态存量回填 | crashed / failed / completed | **completed** | 旧代码"结束才写"，无 running 行；completed 最安全（无 checkpoint 自然 409，sweeper 只动 running） |

## 3. 现状盘点（2026-10-01 核实）

| 事实 | 位置 |
|---|---|
| 消息 buffer 攒整轮，结束才 `append_many` | runner.py:156-160、332-342 |
| flush 用 `base_idx=count_for_session` 重算——多次 flush 会静默重复 idx（messages 表无 (session_id, idx) 唯一约束） | runner.py:333 |
| 干净失败（超时/异常）finally 仍落库，消息不丢 | runner.py:305-342 |
| 硬崩溃丢整个 run 的所有消息（buffer 在内存） | — |
| 超时机制 `asyncio.timeout(self.timeout)`，PI_RUN_TIMEOUT_SECONDS 默认 600 | runner.py:280-283、config.py:110 |
| run_status 只用于 metrics，不落库 | runner.py:277、310 |
| runs 表无 status/checkpoint 列；trajectory NOT NULL | db.py:352-368 |
| loop 侧 checkpoint/resume 已完整、未接线（on_checkpoint 同步回调，仅工具轮触发） | loop.py:73-106、127、203-228、377-383、484-492 |
| 会话锁 `session:{id}` SET NX，TTL=timeout+60，无续期 | runner.py:147-151、cache.py:106-111 |
| Redis 锁在 `ns:lock:` 前缀，`cache.get` 读 `ns:kv:` 前缀——**探不到锁** | cache.py:106-115、127-129 |
| 轨迹双写 jsonl + runs 表（fail-soft） | runner.py:370-388 |

## 4. 方案总览（四步）

1. **Write-ahead**：run 开始前（锁内、loop 前）先落用户消息 + runs 行（status=running）
2. **逐轮落库**：消息在轮边界增量 flush，不再攒到结束
3. **Checkpoint 接线**：`on_checkpoint` → runs 表 checkpoint 列（异步、fail-soft、每工具轮触发不节流）+ resume 端点 + 前端"继续"按钮
4. **状态机**：runs.status ∈ running/completed/failed/timeout/crashed + 后台 sweeper 把残留 running 标 crashed

## 5. 详细设计

### 5.1 Flush 队列（runner.py 内，核心机制）

单消费者 `asyncio.Queue` + 本地单调 idx 计数器 + 哨兵收尾：

- 队列元素：`Message` | `Checkpoint` | `_FLUSH` | `None`（哨兵）
- `on_message`（同步回调）→ `put_nowait(msg)`；`on_checkpoint` → `put_nowait(cp)`；
  TurnEndEvent → `put_nowait(_FLUSH)`；finally → `put_nowait(None)` + `await consumer_task`
- 消费者：Message 攒批 → 收到 Checkpoint **先 flush 消息（硬失败）再 save_checkpoint
  （fail-soft 只 log）** → 哨兵排空退出
- **write-ahead 不走队列**：锁内、创建 AgentLoop 前直接 `await run_repo.create(...)` +
  `await append_many(用户消息, idx=base)`；失败 → yield ErrorEvent + return（run 不开始）
- **idx 不竞争**：唯一写者是消费者，idx 来自本地计数器（write-ahead 时锚定一次），
  废弃 finally 里 `base_idx=count_for_session` 重算
- **消息落库失败 = 终止 run**：消费者置 `flush_failed` Event → producer 每事件边界检查
  → break + ErrorEvent + finish(status=failed)。（消息是核心数据，不做 fail-soft）
- **断连（GeneratorExit）**：drain 放 try/finally，剩余消息仍落库，行留 running 由 sweeper 兜底

多实例安全：队列活在持有会话锁的实例进程内，锁保证同 session 单写者 → idx 无并发竞争；
resume 换实例后由 checkpoint step-guard 兜乱序。

### 5.2 Checkpoint 持久化

- 复用同一队列（先 flush 消息、再存 checkpoint → resume 时 DB 历史与 checkpoint 一致）
- `RunRepo.save_checkpoint(run_id, checkpoint_json, step)`：条件 UPDATE
  `WHERE checkpoint_step IS NULL OR checkpoint_step < step`（防乱序覆盖，双保险）
- 存储：`runs.checkpoint` Text nullable + `runs.checkpoint_step` Integer nullable
- `on_checkpoint` 只在**工具轮**触发（loop.py:377-383），纯文本轮由 TurnEndEvent 覆盖——
  最坏 resume 重跑一个纯文本轮，无副作用
- 实施时补充触发器：优雅停机（SIGTERM）强制存一次最终 checkpoint（见 2.3）

### 5.3 状态机

**迁移 `migrations/versions/0008_run_status_checkpoint.py`**（`down_revision="0007_files"`）：
- `status String(16) NOT NULL server_default="completed"`（存量回填论证见 2.5）
- `checkpoint Text nullable`、`checkpoint_step Integer nullable`、
  `updated_at String(32) NOT NULL server_default=""`
- `trajectory` 改 **nullable**（write-ahead 时还没有轨迹）；downgrade 成对

**RunRepo 拆方法**（删 `save`，唯一调用点 runner.py:386 改走 finish）：
`create / set_status / save_checkpoint / finish(run_id, trajectory_json, status) /
stale_running(cutoff) / mark_crashed(run_id, cutoff)`；`latest_for_session` 加
`.where(RunRow.trajectory.isnot(None))`（修 bug：running 行 trajectory=NULL 会让
`GET /v1/sessions/{sid}/trajectory` 500）

**run_turn 状态写入点**：

| 时机 | 写入 | 失败语义 |
|---|---|---|
| write-ahead（锁内、loop 前） | resume：set_status(running)；新 run：create(running) + 用户消息 | 硬：ErrorEvent + return |
| 每工具轮（on_checkpoint → 消费者） | save_checkpoint（兼作 updated_at 心跳） | 软：log |
| 流结束 finally 后 | finish(status)：ok→completed / error→failed / timeout→timeout | 软：log，行留 running → sweeper |
| 消息 flush 失败（流中） | break + ErrorEvent → finish(failed) | 见 5.1 |
| 客户端断连 | 不写（无法 yield），行留 running | sweeper 兜底 |
| setup 阶段异常（现状 500） | best-effort set_status(failed) + ErrorEvent | 改善现状 |

**Sweeper**（照抄 `_pool_sweep` 模式 runner.py:489-496，但**无条件启动**）：周期 30s；
判定 `status='running' AND updated_at < now-(timeout+60)` **且 `lock_held(f"session:{session_id}")`
为 False**；`mark_crashed` 条件 UPDATE race-safe（N 实例各自 sweep 无害）。lifespan 里
`db.init()` 后 `runs.start_runs_sweeper()`，shutdown cancel。`RunManager.__init__` 加
`run_repo=None` 参数（None 时 sweep 直接返回，单测构造不炸）。

### 5.4 CacheBackend 协议新增两个方法（cache.py:17-40 + 两个实现）

- `lock_held(key) -> bool`：Redis `EXISTS ns:lock:{key}`；Memory `key in self._locks`
- `renew_lock(key, ttl_seconds) -> bool`：Redis `EXPIRE`（返回 bool）；Memory 恒 True

锁参数：TTL **120s**（常量 `SESSION_LOCK_TTL`）+ 心跳 **30s** 一次；续期失败 → 中止 run
（论证见 2.4）。

### 5.5 Resume 端点

- `POST /v1/runs/{run_id}/resume`，body `ResumeIn{model?}`，响应 SSE 帧序列同 run 端点
  （start 加 `run_id` + `resumed: true`）
- 校验顺序：`by_id` 且 `user_id==user.id` 否则 404（照抄 app.py:610-617）→
  `_owned_session` → `status not in (failed, timeout, crashed)` → 409（running=并发/仍在跑，
  completed=已结束，detail 区分）→ `checkpoint is None` → 409 → 限流 429 + 配额 402 照常
- `run_turn` 新签名：`+ run_id: str | None = None, resume_from: Checkpoint | None = None`；
  resume 时 prompt=""、**不 write-ahead**、`loop.run(user_text="", resume_from=resume_from, run_id=run_id)`
- 新 run 端点：stream() 前预生成 `run_id=uuid4().hex[:12]` → start 事件加 `"run_id"`（app.py:652）
- 锁：resume 走正常抢锁（TTL 120s + 心跳，崩溃后 ≤150s 可续跑；抢锁失败且 status=running
  → 409 "still running"；status 已 crashed/failed/timeout 而锁仍被占 → 409 罕见边界）

### 5.6 Trajectory / loop 最小改动（可选参数，向后兼容）

- `trajectory.py:98`：`__init__` 加 `run_id: str | None = None`；`self.run_id = run_id or uuid4().hex[:12]`
- `loop.py run()`（:203）：加 `run_id: str | None = None, user_message: Message | None = None`；
  非 resume 分支用 `user_message or Message(role=user, ...)`；Trajectory 构造透传 run_id；
  **resume 分支零改动**
- 用户消息去重：runner 的 `on_message` 里 `if msg is write_ahead_msg: return`（身份跳过，同一对象）

### 5.7 前端（app.html 单文件）

- `#banner` 加隐藏"继续"按钮；新增 `showResumeBanner(text)`；`currentRunId` 状态变量
  （start 事件记录，sendPrompt 重置）
- SSE 解析抽成 `streamRun(resp)` 供 sendPrompt/resumeRun 共用；error 分支：有
  currentRunId → showResumeBanner（否则普通横幅）
- `resumeRun()`：POST resume → 非 2xx showBanner + finishRun → 2xx 新建 live 容器 +
  streamRun + finishRun

## 6. 实施清单（9 步，每步独立提交可回滚）

1. **cache.py**：Protocol + Memory/Redis 两实现加 `lock_held` / `renew_lock`
2. **迁移**：0008 迁移文件 + `tests/conftest.py:70` 清表元组加 `"runs"`
3. **db.py**：RunRow 四列 + RunRepo 六方法（删 save，同步 runner 调用点）+ latest_for_session 过滤 + `from sqlalchemy import or_`
4. **trajectory.py + loop.py**：可选参数 + `tests/test_durable.py` 补 2 用例（run_id 注入、user_message 透传身份）
5. **runner.py（核心）**：RunManager 加 run_repo；run_turn 重构（write-ahead / 队列 /
   on_checkpoint 接线 / 锁心跳任务 + 失锁中止 / TTL 120s / finish / 外层 except）；
   sweeper 三方法（用 lock_held）；import uuid/datetime/Checkpoint；更新 docstring
6. **app.py**：run 端点 run_id + start 事件 + ResumeIn + resume 端点（含 409 分支）+
   lifespan 启停 sweeper + `app.state.runs`
7. **前端 app.html**：5.7 六处改动
8. **测试**：test_server.py 新增 8-9 用例 + 新文件 tests/test_sweeper.py 4 用例 +
   cache 锁方法单测
9. **文档**：ROADMAP 移除本待办项 + 已完表加行；ARCHITECTURE §8/§11/§6.1/§15 + 锁语义段；
   PROJECT_GUIDE 更新接线状态与待办；README 补 resume 端点、run 生命周期、锁心跳说明

## 7. 测试清单（实施时写）

**test_server.py**（`monkeypatch.setattr("pi.server.runner.resolve_chain", factory)` 注入
`_BoomProvider`（首轮抛错）/`_CrashProvider`（工具轮后抛错）/`_GatedProvider`（事件门控卡住））：
1. start 事件带 run_id，与 `/v1/trajectory/{run_id}` 轨迹一致
2. **write-ahead**：失败 run 后 `/messages` 仍有用户消息（核心保证）
3. 失败 run 后再跑正常 run，idx 连续无缝隙/重叠
4. resume 未知 run / 跨用户 → 404
5. resume 无 checkpoint → 409；6. 已 completed → 409
7. **resume 续跑**：工具轮+checkpoint→炸→resume→200，消息序列无重复用户消息、工具副作用未重执行
8. **逐轮落库**：gated provider 卡住时轮询 `/messages`，消息在 run 结束前可见
9. resume running → 409（可选）

**tests/test_sweeper.py**（独立构造 RunManager，直接调 `_sweep_runs_once`）：stale running
且锁空闲 → crashed；**锁被持有 → 跳过**；fresh running → 跳过；终态行不动。

**tests/test_cache.py**：`lock_held` 三态（Redis 前缀正确性：acquire 后 held=True、release
后 False、`get` 探不到锁）；`renew_lock` Redis 续期（短 TTL 下 renew 后仍 held、不 renew
则过期后 held=False）；MemoryBackend 对应行为。

## 8. 验证步骤（实施时跑）

1. `pi-py migrate` + `SHOW COLUMNS FROM runs` 确认四列
2. `pytest tests/test_durable.py tests/test_sweeper.py tests/test_server.py tests/test_cache.py -x`
3. 手工冒烟：`PI_MODEL=fake/demo pi-py serve` → curl 跑 run（start 带 run_id）→
   消息/轨迹一致性 → resume completed run → 409
4. 崩溃演练：慢速真实模型跑 run → `kill -9` → runs 行 running → 锁 ≤120s 过期 →
   sweeper 标 crashed → 重启 → resume → 续跑（≤150s 内完成）
5. 多实例演练（可选）：两个实例连同一 Redis，实例 A 跑 run 时 kill -9，实例 B 上 resume → 成功
6. 回滚：`python -m alembic downgrade 0007_files` + git revert 对应 step

## 9. 风险与边界（已论证）

- 消息 flush 失败从"finally 异常传播"变为"ErrorEvent 中止 run"——刻意收紧（数据安全优先）
- 存量行 status=completed 是近似（旧代码失败 run 也写行）——但无 checkpoint，resume 必 409，无行为风险
- 进程被暂停 >120s 会丢锁（心跳停）→ 本实例中止 run、sweeper 标 crashed——比静默双写安全，边界文档化
- resume 最坏重执行"最后 checkpoint 之后"的工具——loop 幂等账本已覆盖 checkpoint 内工具，
  文件工具天然幂等，bash 在沙箱内
- 锁心跳任务与 run 同生命周期，run 结束（含断连）必须 cancel——放 finally

## 10. 决策记录与实施触发条件

- **2026-10-01**：设计定稿（含多实例 v2 修订）；用户评估后决定**暂缓实施**（改动面大：
  9 步、跨 server/agent/前端/缓存/迁移），方案归档至本文档 + ROADMAP 未完成事项。
- **建议的实施触发条件**（满足其一即可考虑启动）：
  - 出现"崩溃/重启后对话丢失"的实际投诉或事故
  - 计划上线长任务场景（run 时间接近或超过 600s 超时成为常态）
  - 有空闲开发窗口，按 9 步清单逐 step 推进（每步独立提交、随时可停）
