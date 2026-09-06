# 交接文档：pi-python 任务规划功能（#45）及后续

> 写给接手这个项目的新模型。读完这一份就能继续干活，不需要别的上下文。
> 生成时间：2026-09-04。项目根目录：`/root/pi/pi-python`。

---

## 0. 一句话现状

任务 **#45（P1 Phase 1 任务规划 / `submit_plan` 工具）的代码已全部写完并全部验证通过**（Python 173 passed + 1 skipped，前端 43 passed，`npm run build` 干净，裸机端到端 32/32 检查通过）。临时产物已清理、发现的 `runner.py:140` bug 已记进任务 #50，**只剩 README.md 和 ARCHITECTURE.md 两份文档要写，然后把 #45 标成 completed**，见 §6。做完就可以进 #46。

---

## 1. 环境要点（踩过的坑，务必先读）

| 事项 | 说明 |
|---|---|
| Python | `python` **不在 PATH**。用 `/root/pi/pi-python/.venv/bin/python` |
| Node | 在 `/usr/local/node/bin`，每次都要 `export PATH=/usr/local/node/bin:$PATH` |
| Bash 工具 cwd | 默认是 `/root/pi`，**不是** `/root/pi/pi-python`。每条命令都要显式传 `dir_path` |
| **不是 git 仓库** | `git diff` / `git checkout` / `git stash` 全是静默空操作。改动只能**手工还原**，做变异测试前一定先 `cp` 备份 |
| 浏览器 | **环境里没有浏览器**。前端只能靠 `vue-tsc` + vitest + Vue SSR 渲染测试，无法真正"点开页面看" |
| `.env` | 指向火山引擎云上 RDS（`pi_py`）+ Redis（`prod` 命名空间）。`PI_MODEL=fake/demo`，`PI_SANDBOX=docker` |
| `.env.test` | 指向**同一个云 RDS 实例**的 `pi_py_test` schema |
| 本机服务 | 没有本地 MySQL，8300 端口没有服务在跑 |

### ⛔ 不要碰的东西

- **`.env` 里的云 RDS / Redis**：任务 #42（密钥轮换）和 #43（云侧暴露面）是**用户自己的运维活，明确未授权给我**。任何需要真实数据库的验证，一律用临时 SQLite（`sqlite+aiosqlite:////tmp/xxx.db`），跑完删掉。
- `PI_SANDBOX=docker` 相关的容器操作要小心，别留下孤儿容器（这正是 #49 的 bug）。

---

## 2. 已锁定的设计决定（用户拍过板，**不要再提、不要再问**）

1. **规划轮由谁触发** = `submit_plan` 工具，**模型自决**。结构化计划直接来自工具的 JSON 参数，不需要额外解析。
   - 用户**明确接受**：因此"决定要不要规划"的那一轮**没有物理闸门**，只靠 system prompt 约束。
   - `tools=[]` 的 plan-only 模式**已被考虑并否决**，不要再拿出来。
2. **Phase 1 的计划效力** = **只记录不拦截**。Phase 1 只负责记录 / 落库 / 展示计划，**绝不**能阻止后续执行。真正的闸门推迟到 Phase 2（#46）。

### 全局架构约束（用户在更早的对话里定的，仍然有效）

- 前端在**同仓库 `web/` 下**（Vue3 + TS + Vite + Pinia + Naive UI）。
- 服务**保持裸跑**（`pi-py serve`）。用户原话："跑phase 0 服务暂时先用裸跑吧"、"我暂时还没切换compose"。不要主动改成 compose。
- 前后端契约按**大厂标准**：
  - OpenAPI 单一真源 + codegen（`tools/dump_openapi.py` → `web/openapi.json` → `npm run gen:api` → `web/src/api/schema.d.ts`）
  - REST 语义 + 正确的 HTTP 状态码
  - **不用** `{code, message, data}` 信封
  - 机器可读错误 + `X-Request-Id`
  - SSE 契约单独版本化
  - `POST /runs` 和 `/approve` 要带 `Idempotency-Key`（**尚未实现**，是 #53）
  - token 存储**不用 localStorage**（现在是 sessionStorage）

---

## 3. #45 已完成的改动（文件级清单）

### 3.1 后端

| 文件 | 改动 |
|---|---|
| `src/pi/models.py` | 新增 `Plan` 模型：`title` 1–200 字，`steps` 1–20 条、每条 1–300 字。上限是有意的：这是**不可信的模型输出**，要落 TEXT 列、要走 SSE 帧，约 6KB 封顶。steps 是纯字符串而非对象——**没有 status 字段**，因为 Phase 1 不追踪进度，现在冻结枚举值将来还得拆掉。 |
| `src/pi/tools/base.py` | `ToolResult` 加 `payload: Any = None`（结构化副产物，由 loop 转成事件）；`Tool` ABC 加 `terminal: bool = False`。**做成类属性而不是工具名字符串**，因为 `loop.py` 刻意零硬编码工具名，第二个终止工具不应该需要改 loop 的心脏。`ToolContext` 未动。 |
| `src/pi/tools/plan.py` | **新建**。`SubmitPlanTool`，`name = "submit_plan"`，`terminal = True`，手写 `input_schema`（镜像 Plan 的边界），`execute()` 用 `Plan.model_validate`；校验失败返回 `is_error=True` 且把 pydantic 详情**截断到 500 字**（否则 500 步的提交会往模型历史里灌好几 KB）。成功时的 content **刻意短于 loop 的 200 字 SSE 预览**，保证客户端能看到全文。**不碰文件系统、不碰 ctx**。 |
| `src/pi/tools/__init__.py` | 注册进 `all_tools()`，位置在 `LsTool()` 之后（第 8 个，共 **10** 个）。`__all__` 和模块 docstring 同步更新。`policy.json` **未动**。 |
| `src/pi/prompt.py` | 工具清单加一行；并把"2+ 文件 / 3+ 工具调用 / 重构或迁移 → **先且单独**调 submit_plan"作为**第一条**工作准则。 |
| `src/pi/agent/events.py` | 新增 `PlanEvent` dataclass，位置在 `CompactionEvent` 和 `TurnEndEvent` **之间**，并加进 `AgentEvent` 联合的同一位置。 |
| `src/pi/agent/loop.py` | **核心改动**，见 §4。 |
| `src/pi/server/db.py` | `SessionRow` 加 `plan: Mapped[str \| None] = mapped_column(Text)`——**全表唯一可空列**，原因见 §5.1。`SessionRepo.set_plan()`（该 repo 里第一个 UPDATE，仿 `UserRepo.set_active` 写的）。 |
| `migrations/versions/0003_session_plan.py` | **新建**。`revision = "0003_session_plan"`，`down_revision = "0002_user_active"`。upgrade 是 `op.add_column("sessions", sa.Column("plan", sa.Text(), nullable=True))`，downgrade 是 `op.drop_column`。 |
| `src/pi/server/runner.py` | `run_turn()` 新增**必填** kwarg `session_repo: SessionRepo`（唯一调用点是 `app.py:668`，已 grep 确认）。捕获 `PlanEvent` 存进局部 `plan`。**在 `append_many` 之后**才 `set_plan`——一次 run 是全或无落库，中途写 plan 会留下一行描述着从未落地的转录的记录。放在这里还免费继承了超时语义：超时的 run 两者都会 flush。`event_to_sse` 新增 `event: plan` 分支，**不加长度截断**（Plan 自己的字段边界已经把它限在 ~6KB）。 |
| `src/pi/server/app.py` | `SessionSummary` 加 `plan: Plan \| None = Field(default=None, ...)`；`SseToolCallEndData.ok` 的描述扩写以覆盖"被跳过"这种情况；新增 `class SsePlanData(Plan)`（**继承而非重声明** title/steps，边界只活在一个地方）；`SSE_DATA_MODELS["plan"]`；`SSE_DOC` 更新（事件枚举改成"then at most one `plan`, then `turn_end`"，并新增两段说明 plan 的位置/持久化时机、以及每个 `toolcall_start` 都必有 `toolcall_end`）；新增 `_plan_of(row)` 辅助函数（**没有损坏兜底**，理由同 `get_messages`：该列只由 `Plan.model_dump_json()` 写入，解析失败是 bug 要暴露，不是要糊过去的状态）；`list_sessions` 和 `get_session` **两个端点都返回 plan**（这样刷新页面看到的和列表一致）；`run()` 传 `session_repo=sessions`。 |

### 3.2 前端

| 文件 | 改动 |
|---|---|
| `web/openapi.json`<br>`web/src/api/schema.d.ts` | 已重新生成："15 paths, 41 component schemas"。`Plan` 在 :442，`SsePlanData` 在 :590，`SessionSummary.plan?: components["schemas"]["Plan"] \| null;` 在 :557。**`= None` 让 codegen 产出可选可空**，这是既有客户端 fixture 还能编译的原因。 |
| `web/src/api/types.ts` | 加 `export type Plan = Schemas["Plan"];`；`SseEventMap` 里在 compaction 和 turn_end 之间加 `plan: Schemas["SsePlanData"];` |
| `web/src/api/endpoints.ts` | `KNOWN_EVENTS` 加 `plan: true`。**漏了会直接编译失败**——那里用 `satisfies Record<SseEventName, true>` 做了双向约束。 |
| `web/src/stores/chat.ts` | `UiMessage` 加 `plan?: Plan`；painter 加 `case "plan"`（推一条独立的 assistant 消息，然后 `current = null`，和 compaction 同样处理）；新增模块级 `parsePlan(args: string): Plan \| null`（手写窄化，JSON.parse 失败 / 非对象 / title 非字符串 / steps 非字符串数组 / 空数组，全部返回 null）；`toUi()` 加 **`failed` 预扫描** + 折叠逻辑，见 §5.2。 |
| `web/src/views/ChatView.vue` | 模板里在文本气泡和工具行**之间**插入 `.plan` 块（`<strong>任务规划：{{title}}</strong>` + `<ol class="plan-steps"><li>`）。CSS 的 `.plan` / `.plan-steps` 放在 `.result` 之后，**保持 `.tool`/`.tool-head`/`.block`/`.result` 连续不被拆开**。`.plan` 镜像 `.tool` 的盒模型。 |

---

## 4. `loop.py` 的核心语义（最容易改坏的地方）

新增的模块级件：

```python
SKIPPED_RESULT = (
    "Error: skipped - {tool} ended this turn. This tool call did not execute "
    "and had no side effects."
)

def _args_or_empty(call: ToolCallBlock) -> dict[str, Any]: ...   # 给从未进 _run_tool 的调用做审计用

@dataclass
class _ToolOutcome:
    block: ToolResultBlock
    name: str
    terminal: bool = False
    payload: Any = None
```

`_run_inner` 的批处理循环：

- 顺序执行 `calls`。某个调用**成功**且 `tool.terminal` 为真 → 记为 `ended_by`。
- **终止工具只在成功时结束本轮**：被 policy 拒绝或参数畸形的 `submit_plan` 会把错误喂回去、本轮继续，这样模型能修参数，而不是让用户既没计划也没回答。
- `ended_by` 之后的调用**完全不进 `_run_tool`**（`tool.execute` 不被触达就是重点），**也不过 policy**（policy 管的是执行，没执行就没什么可管）。合成 `is_error=True` 的 `ToolResultBlock` + `SKIPPED_RESULT.format(tool=ended_by.name)`，并调 `_audit(..., ok=None, allowed=False, reason=f"skipped: {ended_by.name} ended the turn")`。
- **每个调用都 yield 一个 `ToolCallEndEvent`**，包括被跳过的。不发的话 UI 里那一行会永远显示"执行中"——因为 `ToolCallStartEvent` 对批内每个调用的每个参数增量都会发。
- 最后 `self._append(Message(role=Role.user, blocks=[o.block for o in outcomes]))`：**每个调用一个结果块，按调用顺序**。
- 然后 `if ended_by is not None:` → 若 payload 是 `Plan` 就 `yield PlanEvent(plan=...)`，然后 `break`。

`_run_tool` 的成功路径返回 `_ToolOutcome(block=block, name=call.name, terminal=tool.terminal, payload=result.payload)`。`terminal`/`payload` **只从这一条路径出去**：未知工具、参数坏、policy 拒绝、抛异常都提前 return 并带 dataclass 默认值，所以**失败的终止工具不可能结束本轮**。

`_audit()` 签名扩成 `(self, tool, args, ok: bool | None, preview, allowed: bool = True, reason: str = "")`，内部传 `decision_allowed=allowed, decision_reason=reason`。**注意 JSONL 里的键名是 `allowed` / `reason`，不是 `decision_allowed`。**

---

## 5. 三个非显而易见的技术不变量

### 5.1 MySQL TEXT 默认值夹缝（已实测）

- MySQL **拒绝** TEXT/BLOB/JSON 上的字面 `DEFAULT`（错误 1101；表达式默认值要 8.0.13+）
- PostgreSQL **拒绝**在已有数据的表上 `ADD COLUMN ... TEXT NOT NULL` 且不给默认值

**没有同时满足两者的形状，除了 NULL。** 所以 `SessionRow.plan` 必须是可空的。别"照着 0002 的风格加个 `server_default=""`"——那会在生产 MySQL 上炸 1101，而 `test_sessions_plan_column_is_portable` 就是为了让它**在本地就红**。

DDL 渲染差异（实测）：MySQL 和 PostgreSQL 渲染成 `plan TEXT,`，**SQLite 渲染成 `"plan" TEXT,`**（PLAN 在 SQLite 的关键字表里）。所以那个测试要先 `replace('"', "")` 再整行精确比对。

### 5.2 `tool_call` / `tool_result` 配对不变量

OpenAI 和 Anthropic 都会拒绝"assistant 消息里有 `tool_call` 但紧随的 user 消息里没有对应 `tool_result`"的请求——**而且是在下一轮才拒绝，不是产生它的那一轮**。这是整个功能最难的部分：跳过同批调用时**必须**合成结果块，否则历史存进去就再也读不出来了。

前端侧的推论：`toUi()` 里 `tool_result` 比它回答的 `tool_call` **晚一条消息**到达。所以折叠时看到 `submit_plan` 调用的那一刻，**无法知道校验有没有通过**。解法是**先对整个 history 做一遍预扫描**收集所有 `is_error` 的 `tool_use_id`，再在主循环里用 `!failed.has(b.id)` 做守卫。没有这个守卫，一个被工具拒绝的 `submit_plan` 会渲染成一张宣告着根本不存在的计划的卡片。

`parsePlan` 成功时，那个调用**仍然注册进 `calls` map**（这样配对的 `tool_result` 被吸收，不会掉进 orphan 分支渲染成一个凭空出现的 user 气泡），但**刻意不 push 进 `ui.tools`**——卡片已经展示了这次调用，下面再挂一行原始 JSON 是噪音。

### 5.3 painter 是一次性的，`toUi()` 才是真相

`send()` 在流结束后立刻调 `reload(sid)`，用持久化转录替换 painter 的输出。所以：

- **直播时**：painter 把 plan 推成一条**独立的 assistant 消息**，并且 `submit_plan` 的原始工具行**仍然可见**
- **reload 之后**：`toUi()` 去掉那行工具、把卡片挂到 assistant 消息上

这两者形状**不同**，几百毫秒内收敛。这符合仓库既有立场（painter 的注释就写着"deliberately throwaway"），但**必须在 ARCHITECTURE §18 里如实记录**，见 §6.3。

---

## 6. #45 剩余工作（**只剩 3 项**：§6.2 README、§6.3 ARCHITECTURE、§6.5 标记完成）

### 6.1 ~~清理临时产物~~ —— 已完成

上一个会话的裸机端到端脚本和临时库（`/tmp/e2e_plan.py`、`/tmp/e2e_plan.db`、`/tmp/e2e_audit-*.jsonl`、`/tmp/e2e_ws/`、`/tmp/e2e_leak.txt`、`/tmp/plan_mig.db`）**已确认全部删除**，无需再处理。

留一条知识备查：`AuditLogger` 会给配置的路径**加日期后缀**（`PI_AUDIT_PATH=/tmp/x.jsonl` → 实际写 `/tmp/x-2026-09-04.jsonl`），而且**跨 run 累积**，不覆盖。

### 6.2 `README.md`（**还没读过，先读**）

- 内置工具数 **Nine → Ten**
- 新增一段 "Task planning (Phase 1)"
- Streaming 那一行的事件列表加 `plan`
- 更新测试数

### 6.3 `ARCHITECTURE.md`（**还没读过，先读**）

需要改的地方：头部日期与计数、§3.3 目录树（9→10 工具）、§6.1、§6.2、§7.1、§7.2、§8、§11.3、§11.5、§15、§16 新增一行、§17 新增一条、§18.2 / §18.6。

已核实的章节结构（行号会随编辑漂移，**用标题定位**）：

```
§3.3 目录树 · §6.1 · §6.2 · §7.1 · §7.2 · §7.3 sandbox.py
§8  会话与消息持久化 db.py
§11.3 app.py 路由全表 · §11.3.1 契约：OpenAPI 是唯一真相源 · §11.5 runner.py · §11.6 db.py ORM 与仓储
§15 测试 · §16 常见扩展怎么做 · §17 注意事项与坑（前人踩过的）
§18 前端 web/
    §18.1 定位与刻意不做的事   §18.2 目录与数据流   §18.3 契约与托管
    §18.4 六个不踩一遍就想不到的坑   §18.5 测试：四层，以及每层证明不了什么   §18.6 已知缺口
附录：一次 run 的时序（文字版）
```

两个**必须新写**的点：

- **§17 新增第 24 条**：MySQL TEXT 默认值夹缝（§5.1 的全部内容），包括 SQLite 渲染 `"plan"` 带引号这个细节。
  **已核实**：§17 现在是 1–23 条，格式是有序列表 `N. **标题**：说明`，所以新条编号就是 `24.`。
- **§18.2 / §18.6**：plan 卡片的 live-vs-reloaded 形状差异（§5.3），以及**为什么卡片能在 reload 后活下来**——因为 `submit_plan` 的 `arguments` 是历史里唯一留存的计划副本，`toUi()` 靠解析它重建卡片，**没有专门的 plan block**。

⚠️ **§18.4 的标题把数量写死成"六个"**。如果你觉得 live-vs-reloaded 这条更适合放进 §18.4 而不是 §18.2/§18.6，**必须同时把标题改成"七个"**，否则标题和内容对不上。

### 6.4 ~~把 `runner.py:140` 的 NameError 记进 #50~~ —— 已完成

已写入任务 #50 的描述，**下一个会话不需要再做这件事**，只需要在真的动 #50 时按里面记的修法改。

留档：`src/pi/server/runner.py:140`（**是 140，不是 139**）

```python
log.debug("sandbox prewarm failed", exc_info=True)
```

`runner.py` **从未定义 `log`**（grep 确认：全文件只有这一处出现 `log`，没有任何 `log = ` 赋值）。这是个潜在 NameError，藏在 best-effort 的 `except Exception` 里，只有沙箱预热抛异常时才触发——也就是**把一个可恢复的预热失败变成一个真崩溃**。

它为什么能活下来：该文件第 11 行已经 `import logging`，第 199 行本来就写着 `logging.getLogger("pi.server").exception("usage recording failed")`。**同一个文件里两种写法并存**，所以少了一个模块级 `log` 没人注意到。建议的修法是加模块级 `log = logging.getLogger("pi.server")`（对齐 `app.py:39` 和 `cache.py:14`），然后 140 和 199 两处都用它。

**#45 没有修它**，因为超出范围。

### 6.5 把任务 #45 标为 completed

---

## 7. 验证证据（都真跑过，不是推断）

### 7.1 测试计数

- **Python**：`173 passed, 1 skipped`（改动前是 151+1，净增 22）
  - `tests/test_planning.py` **新建**，16 个测试
  - `tests/test_mysql_compat.py` +1
  - `tests/test_server.py` 新增 `TestPlanRun` 4 个 + `_tc` / `_frames` / `_scripted` / `_session` 四个辅助
  - `tests/conftest.py` 新增 **`StrictFakeProvider`**
- **前端**：`43 passed`（改动前 36，净增 7）= sse 9 + transcript 21 + render 13
  - `npm run typecheck` 干净
  - `npm run build`（= `vue-tsc --noEmit && vitest run && vite build`）干净，产物 307KB / gzip 96.77KB

### 7.2 `StrictFakeProvider`（关键测试基建）

在 `tests/conftest.py`。它继承 `FakeProvider`，在 `stream()` 开头强制校验 §5.2 的配对规则，不配对就 `raise ValueError`。

**为什么放 conftest**：让 loop 级测试和 server 级测试共用。

**为什么这件事重要**：原生 `FakeProvider` **完全忽略它的 `tools` 参数**，也**完全不检查配对**。`tests/test_server.py` 的 `_scripted()` 在脚本用尽后 fallback 到 provider——我最初写的是普通 `FakeProvider`，结果 `test_history_after_a_plan_run_feeds_the_next_run` **根本抓不到它声称要抓的 bug**。改成 strict fallback 之后重跑变异 1，红测试数从 4 涨到 6，两个 server 级测试都进来了——这才证明修复是真的。

### 7.3 变异测试：5 个变异、6 次运行（**不是 git 仓库，只能手改再手工还原**）

第 5 个变异跑了两个变体（5a 删整个折叠、5b 只删守卫），所以表里有 6 行。

| # | 变异 | 结果 |
|---|---|---|
| 1 | 去掉跳过结果的合成 | 4 个 loop 测试红，`ValueError: tool_calls without a tool_result: ['c2']`。server fallback 改 strict 后 → **6 红** |
| 2 | 无条件终止（不管 `is_error`） | **恰好 1 红**：`test_an_invalid_plan_does_not_end_the_turn`，显示 `turns=1` 且流出的文本为空——即用户既没拿到计划也没拿到回答 |
| 3 | 抑制被跳过调用的 `ToolCallEndEvent` | **恰好 1 红**：显示 `[('c1', True)]` 而不是三个 end 事件——即 c2/c3 会在 UI 里永远挂着"执行中" |
| 4 | 给 `SessionRow.plan` 加 `server_default=""` | `test_sessions_plan_column_is_portable` 红。这就是让"照抄 0002 风格"**在本地失败而不是在生产 MySQL 上报 1101** 的守卫 |
| 5a | 删掉整个 `toUi` 的 plan 折叠 | **2 红**：`folds submit_plan into a card instead of a tool row`、`keeps the batch-mate that submit_plan cut short, as an error` |
| 5b | 只删 `&& !failed.has(b.id)` 这个守卫 | **恰好 1 红**：`renders no card for a submit_plan the tool rejected`——证明 §5.2 的预扫描被精确地钉住了 |

全部已手工还原，还原后重跑：Python 173+1，前端 43，typecheck 干净。

### 7.4 迁移验证

- `alembic heads` → `0003_session_plan (head)`
- 临时 SQLite 上 `upgrade head` → `('plan', 'TEXT', 0)`（可空）；`downgrade -1` → plan 消失、版本回到 `0002_user_active`；再 `upgrade head` → plan 回来
- **又用真实的 `pi.cli migrate` 建了一次库**（因为测试 fixture 用 `create_all`，**迁移文件永远不会被执行**）：`pragma table_info(sessions)` 得到 `(6, 'plan', 'TEXT', 0, None, 0)`，`alembic_version = 0003_session_plan`

### 7.5 裸机端到端（32/32 通过）

真实 uvicorn + 真实 socket + 真实迁移过的 **临时 SQLite**（**没碰云 RDS**）。做法：临时脚本 `os.environ.update(...)` 设好所有 env（cwd 设成 `/tmp` 以避开 `.env` 自动加载），然后 `runner.resolve_chain = resolve` 注入脚本化的 `StrictFakeProvider`（因为 `fake/demo` 自己不会决定去调 `submit_plan`），`create_app(ServerSettings.from_env())`，`uvicorn.Server` 起在 `127.0.0.1:8399`，用 `httpx.AsyncClient` 走完整流程。

第一轮脚本：`TextBlock` + `submit_plan(c1)` + `bash(c2, "touch /tmp/e2e_leak.txt")`。

关键结果：

- 线上顺序：`start, text_delta, toolcall_start, toolcall_start, toolcall_end, toolcall_end, plan, turn_end, done`
- `content-type: text/event-stream; charset=utf-8`、`X-Request-Id` 都在
- plan 帧**恰好一个**、载荷逐字相符、位置在最后一个 `toolcall_end` 之后 `turn_end` 之前
- c1 `ok=True`；c2 `ok=False` 且 result 含 "did not execute"；**`/tmp/e2e_leak.txt` 不存在**——被跳过的 bash 真的没跑
- 两个调用都拿到了 start 帧
- `GET /v1/sessions/{id}` 和 `GET /v1/sessions` 返回**同一个** plan
- 持久化历史完全配对：`calls=['c1','c2'] results=['c1','c2']`；`submit_plan` 的 arguments 逐字保存
- **第二轮跑在同一会话上**：HTTP 200、无 error 帧、有 text_delta、以 done 结束、plan 仍在。（这一轮走的是 strict fallback provider，所以它实际验证的是**从数据库读回来的历史**是配对的）
- 审计日志：
  ```json
  {"event":"tool_call","tool":"bash","args":{"command":"touch /tmp/e2e_leak.txt"},
   "allowed":false,"reason":"skipped: submit_plan ended the turn","ok":null,
   "result_preview":"Error: skipped - submit_plan ended this turn. ..."}
  ```
  `submit_plan` 自身是 `allowed=true, ok=true`，args 是完整计划。

**跑这个脚本时踩到的两个坑**（如果你要重写它）：登录响应字段是 **`access_token`** 不是 `token`（`LoginOut` 的形状）；审计文件名有**日期后缀**。

---

## 8. 任务清单现状

| # | 状态 | 内容 |
|---|---|---|
| 42 | pending | P0 密钥轮换（`PI_JWT_SECRET` / MySQL / Redis）——**用户的运维活，未授权给我碰** |
| 43 | pending | P0 云侧暴露面：关 RDS 公网端点、把 `pi`@`%` 从 `*.*` 收窄到 `pi_py`.* + `pi_py_test`.*、限制安全组 8300 与 Caddy 443、刷新那个含密码和公网 IP 的 stale `.venv/…/pi_py-0.1.0.dist-info/METADATA` |
| 44 | **completed** | P1 前端最小页面 `web/`（Vue3+TS+Vite+Pinia+Naive UI） |
| 45 | **in_progress** | P1 Phase 1 任务规划（`submit_plan`，只记录不拦截）← **代码全绿，只剩 §6 的 3 项收尾（两份文档 + 标记完成）** |
| 46 | pending（blockedBy 45） | P1 Phase 2 人工确认：按批审批 + 落库暂停态 + `POST /v1/sessions/{id}/approve`（409/404 + `Idempotency-Key`）+ SSE `approval_required`。等待期间**必须释放** 660s 会话锁 / 全局信号量 / `asyncio.timeout(600)` / SSE 连接 |
| 47 | pending（blockedBy 46） | P1 Phase 3 断点恢复（消息历史即检查点） |
| 48 | **closed by deletion** | P2 `web_fetch` / `web_search` 的 SSRF——**不是修好的，是删掉的**：`tools/web.py` 已移除，联网改由模型端点自己的 builtin tools（`RunIn.builtin_tools`）承担，`policy.json` 的 `deny_tools` 随之清空，`tests/test_security.py::test_the_local_tool_surface_has_no_internet_tool` 钉住"注册工具不许 import HTTP 客户端"。详见 ARCHITECTURE §7.2 / §17 第 20 条 |
| 49 | pending | P2 冷路径（`PI_SANDBOX_POOL=0`）超时会漏一个容器 |
| 50 | pending | P2 小缺口打包：compaction 配对、注册/登录 IP 限流、username 长度、池子 sizing、死代码（`models.py:45-52` 那个死字符串字面量）**+ `runner.py:140` 的 NameError（已写进该任务的描述，含核实过的两种修法）** |
| 51 | **completed** | P1 后端补齐 OpenAPI 响应模型（前端 codegen 的前提） |
| 52 | pending | P1 中断 run 会丢掉整轮消息（工具已经跑了，记录却没落库） |
| 53 | pending | P1 契约四项未交付：`Idempotency-Key` / `error_code` / SSE 版本化与重放 / 断线重连 |

**下一步**：做完 §6 剩下的三项（README、ARCHITECTURE、标记完成）→ 进 #46。

---

## 9. 会约束你的既有测试（改任何 SSE 事件前必读）

三个契约测试会卡住任何新增/改名的 SSE 事件：

1. `test_every_documented_event_is_one_the_server_emits` —— 要求
   `set(SSE_DATA_MODELS) - {"start", "done"} == set(RUNNER_EVENTS)`
   （**我加 `SSE_DATA_MODELS["plan"]` 之后立刻红过一次**，因为 `RUNNER_EVENTS` 还没加对应的 `PlanEvent` 实例。两边必须同步。）
2. `test_no_response_body_is_left_untyped`
3. `test_live_stream_frames_match_the_documented_models`

前端侧的对应约束：`endpoints.ts` 的 `KNOWN_EVENTS` 用 `satisfies Record<SseEventName, true>` 做**双向**检查——重新生成的 schema 多了一个事件、或后端删了一个事件，**都会编译失败**。

Vitest 5.0.0 的默认 include 是 `**/*.{test,spec}.?(c|m)[jt]s?(x)`，**不匹配 `*.live.ts`**——这就是 opt-in 的 live 套件不会混进 `npm test` 的原因。

---

## 10. Vue / TS 的坑（ARCHITECTURE §18.4 已记录，容易再踩）

- 往 reactive 数组 `push` 存进去的是**原始对象**。必须回读 `arr[arr.length - 1]` 拿到 proxy，否则流式增量永远到不了 DOM。painter 里的 `assistant()` 和 `toolCall()` 都是这么写的，注释也解释了原因。
- `strict` **不包含** `noUncheckedIndexedAccess`，所以 `arr[0]` 的类型不是 `T | undefined`。
- 中文 IME 的 Enter 到达时 `key === "Enter"`，所以必须检查 `e.isComposing`。
- `NAlert` 的关闭状态是内部的，消息内容变化时需要 `:key` 才能重新显示。

---

## 11. 常用命令

```bash
# Python 测试
cd /root/pi/pi-python && .venv/bin/python -m pytest -q

# 前端（记得 PATH）
export PATH=/usr/local/node/bin:$PATH
cd /root/pi/pi-python/web && npm run typecheck && npm test && npm run build

# 重新生成契约（后端响应模型改了之后）
cd /root/pi/pi-python && .venv/bin/python tools/dump_openapi.py
cd /root/pi/pi-python/web && npm run gen:api

# 迁移
cd /root/pi/pi-python && PI_DATABASE_URL="sqlite+aiosqlite:////tmp/x.db" .venv/bin/python -m pi.cli migrate
```

---

## 12. 已批准的方案文档

`/root/.qoder-cn/plans/grand-boulder-gull.md`（378 行）——包含 Context、已锁定的两个决定、已核实的关键事实、核心语义 S1–S7 表、后端改动步骤 1–12、前端改动 1–4、测试、文档、验证（6 步含 5 个具名变异检查）、明确不做的事。**§6 的文档要求就是从这里来的**，动手前建议对照一遍。
