# CubeSandbox 沙箱层重构设计笔记：从"Docker 补丁"到"按能力设计"

> 本文记录 pi-py 沙箱执行层的一次完整重构：为什么改、怎么改、遇到的每一个坑、
> 每个坑背后的设计原则。适合作为系统设计/防御式编程的面试素材 —— 每节末尾
> 附「面试怎么讲」，是 30 秒能讲清楚的核心。
>
> 相关代码：`src/pi/tools/sandbox.py`（CubeSandboxRunner / SandboxFS）、
> `src/pi/tools/base.py`（WorkspaceFS 抽象 / LocalFS）、五个文件工具、
> `src/pi/server/runner.py`（run_turn 生命周期）。

---

## 一、背景：为什么"纠正现有代码"

**旧设计的来历**：pi-py 最初的沙箱基于 Docker。Docker `docker run` 冷启动大约 1 秒，
且容器不能带状态、文件系统在宿主 —— 于是代码里长出了一堆为 Docker 妥协的机制：
warm pool 常驻容器池、prewarm 预热、idle TTL 驱逐、每次 bash 调用前后全量 tar
工作区上下同步、read/write/edit 直接操作宿主路径。

**新条件**：迁移到 CubeSandbox（腾讯云开源，E2B SDK 兼容，RustVMM 微虚拟机）：

| 能力 | 实测数据 |
|---|---|
| 创建沙箱 | 71ms（0.09s 级） |
| 热连接 | 8ms |
| 快照（create_snapshot）| 平台原生支持 |
| VM 级回滚（rollback）| 平台原生支持，可恢复文件状态 |
| 文件 API | files.read / files.write |

**结论**：冷启动 71ms，常驻池的前提（启动慢）不存在了；沙箱可以带状态，工作区
可以真身住在沙箱里。于是把为 Docker 打的补丁全部拆掉，按 CubeSandbox 的能力
重新设计。这就是「按能力设计，而不是按习惯设计」—— 面试时这是第一个可以讲
的点：**技术选型变化后，旧代码里"绕路"的机制要连根拔掉，而不是继续背着它**。

---

## 二、核心架构决策（ADR 风格）

### 决策 1：不建常驻池 —— 每轮任务一个沙箱，用完即毁

- **背景**：Docker 时代建池是因为启动慢（1s）；池带来一堆复杂度：LRU、TTL、淘汰、
  状态同步、池内残留。
- **选择**：每次 run_turn 创建新沙箱（0.09s），turn 结束在 `finally` 里 close
  （工作区回传宿主 + kill VM），不留任何池。
- **理由**：创建成本已低于一次 LLM 往返的零头；无池 = 无状态残留 = 无限并发安全
  （并发上限只由内存规格决定，不由池容量决定）。内存模型从「会话数 × 256Mi」
  变成「并发活跃轮数 × 256Mi」。
- **关键实现细节**：close 必须放在 `finally`，否则每轮泄漏一个 VM，几轮就能把
  平台配额吃满（见「故障实录 1」—— 这是真实发生的生产事故）。
- **面试怎么讲**：「池化是给慢启动支付的成本。当冷启动降到 0.1 秒量级，池的
  复杂度就不值得了 —— 我删掉了 pool/prewarm/TTL 三层机制，把生命周期收敛到
  turn 的 finally 里，并用平台 VM 计数验证了零残留。」

### 决策 2：工作区真身在沙箱，宿主只是归档态

- **旧**：工作区 bind mount 在宿主，每次 bash 调用 tar 上、tar 下 —— 每轮工具
  调用 ~6 次 tar，且模型看到的 `/ws` 与 read/write 工具的宿主路径**分裂**，
  模型写 `/ws/fetch_report.py` 就会被拦（真实的用户可见事故）。
- **新**：会话开始把宿主工作区 tar 进沙箱一次（`_load_workspace`），期间 bash
  和文件工具操作**同一份沙箱文件系统**；turn 结束 close 时 tar 回宿主一次
  （`_save_workspace`）。全程最多 2 次同步，与工具调用次数无关。
- **一致性收益**：模型在 bash 里 `pwd` 是 `/workspace`，write/read/edit 看到的
  也是 `/workspace` —— 路径认知与执行完全一致，摩擦消失。
- **面试怎么讲**：「架构里有一个核心不变量：**执行视图与路径视图必须一致**。
  之前 bash 在容器、文件工具在宿主，两边不一样，模型就分裂。我把工作区整体
  搬进沙箱后，这个不变量被修复，顺带把每轮 6 次 tar 降到 2 次。」

### 决策 3：文件系统抽象层（WorkspaceFS）

- 所有文件工具（read/write/edit/ls/find/grep）不直接碰 `os`/`Path`，而是操作
  一个 `WorkspaceFS` 协议：默认 `LocalFS`（宿主，历史行为不变），沙箱模式下
  注入 `SandboxFS`（files API + 沙箱内 shell）。
- `ToolContext.fs` 由 `AgentLoop(runner=...)` 构造注入，`server/runner.py` 装配
  时把沙箱根（session cwd）bind 给 fs（`set_host_root`）。
- **收益**：工具层零沙箱知识；local / docker / cubesandbox 三种模式共享一套
  工具实现；测试可以对同一套工具分别打 LocalFS（快）和 SandboxFS（真）两遍。
- **面试怎么讲**：「我用接口隔离了『文件系统在哪』这个问题 —— 工具只认
  WorkspaceFS 协议。本地模式是 LocalFS，沙箱模式是 SandboxFS，加新沙箱后端
  不用改任何工具代码。」

---

## 三、安全设计：越界，到底怎么防的

这是本次最值得讲的部分 —— **越界防护是分层的，单一防线是危险的**。

### 3.1 两道防线，职责不同

```
模型请求 write /ws/fetch_report.py
        │
        ▼
① Policy 层（server/security）   path_sandbox 检查
        │  宿主绝对路径是否在工作区内；不在 -> 拒绝，审计记录 allowed=False
        ▼
② FS 层（SandboxFS._rel）        relpath 越界检查
        │  os.path.relpath(path, root) 以 "../" 开头 -> 抛 ValueError
        ▼
③ 工具层 try/except             ValueError -> ToolResult(is_error=True)
```

- **防线①（Policy）**在高层做声明式管控，产出审计记录（面试点：安全事件要有
  可追踪性 —— 那次拦截在审计里是 `allowed=False + reason` 一条清晰记录）。
- **防线②（FS）**在数据通路里兜底，即使绕过 policy 也写不出去。
- **第一版只有①**，模型写 `/ws/xxx` 被拦了（真实事件）。但**只靠上层拦截的
  问题**：模型的自愈路径（写相对路径）每次都要靠模型试错，体验差。方案 B 让
  write 进沙箱后，模型写相对路径天然一致；绝对越界路径（如 `/etc/passwd`）
  被②拦 —— 双层防护互不依赖。
- **面试怎么讲**：「越界防护我做两层：policy 层负责声明式管控和审计，fs 层
  在数据通路兜底。两层职责不同 —— 一层是『我觉得你不该』，一层是『你物理上
  做不到』。只做一层，绕过它只是时间问题。」

### 3.2 安全降级：探测与写入的行为差异（关键设计）

越界路径出现时，**读类探测和写类操作的反应不一样**：

| 操作 | 越界时行为 | 理由 |
|---|---|---|
| exists / is_dir / list_dir / walk / file_size / read_bytes | 返回安全默认值（False / 空 / None），**不抛** | 这些是"问问看"，查询失败不应炸掉整个工具调用 |
| write_bytes | **抛 ValueError → 工具返回 Error** | 写入是危险动作，必须显式失败，掩盖失败比拒绝更糟 |
| 工具层 | write 的 Error 带原因"path escapes workspace: ..." | 模型能看到原因并自愈（实测模型看到错误后改相对路径重试成功）|

实现上用一个 `_ws_for(path)` 转换器：内部 try `_rel()`，越界返回 None，探测方法
`if p is None: return False/[]/None`；写方法保留异常给工具层捕获。

- **面试怎么讲**：「安全降级不是一刀切 —— 查询类操作对越界静默降级（安全默认
  值），写操作显式失败。因为静默失败在写路径上等于数据丢失，比越界本身更危险。
  这个『读写不对称』原则我在设计文件系统抽象时定死的。」

### 3.3 探测命令必须保证 exit 0（一个隐蔽的坑）

e2b SDK 的 `commands.run` 对非零退出码**抛异常**（`CommandExitException`）。
于是 `test -d /不存在 && echo yes` 这种朴素写法，在路径不存在时直接抛异常，
把"文件不存在"变成"系统故障"。

```python
# 错：路径不存在 -> exit 1 -> SDK 抛异常 -> 误判为系统错误
f"test -d {p} && echo yes"
# 对：无论结果如何 exit 0，用输出来区分
f"test -d {p} && echo yes || echo no"
```

- **面试怎么讲**：「远程执行器对非零退出码的语义是把异常当硬错误。探测类命令
  必须写成『必然 exit 0，结果编码在 stdout』，否则文件不存在会被误报成平台故障，
  排查时非常误导。这种坑只有被炸过一次才记得住 —— 我把它写进了团队的编码
  约定。」

---

## 四、异常处理哲学

### 4.1 工具边界：输出要么是结果，要么是结构化错误，绝不裸抛

- 每个工具 `execute` 的返回值永远是 `ToolResult(content, is_error)`，异常在
  工具内部被捕获转成 `is_error=True` 的结果。
- 这样 agent 循环、事件流、审计对"失败"有统一视图；模型拿到的是可读错误文本
  （它还靠这个自愈），不是堆栈。
- **面试怎么讲**：「我把工具设计成**永不裸抛**：一切失败都翻译成
  ToolResult(is_error=True)。堆栈是给工程师看的，模型需要的是能 self-heal
  的错误语义 —— 这在 MCP/工具类系统里是基本盘。」

### 4.2 执行边界：CommandRunner 一把梭兜底

```python
async def run(self, command, cwd, timeout) -> CommandResult:
    try:
        ...
    except Exception as exc:  # noqa: BLE001 - one result either way
        return CommandResult(output=f"Error: cube sandbox failed ({name}): {exc}",
                             exit_code=-1, timed_out=...)
```

- 无论 SDK 抛什么（超时、连接、参数、平台 5xx），都归一成一个
  `CommandResult(exit_code=-1)`。调用方（模型）只需要看 exit_code 和 output。
- 显式标注 `noqa: BLE001` 并写明理由 —— 宽捕获在这里**是**正确选择，因为
  这是一个对接外部系统的边界，边界上不允许异常外泄。
- **面试怎么讲**：「外部系统边界上的异常必须在边界内消化 —— 我的 runner 把
  一切异常归一到 CommandResult，调用方永远拿得到一个『结果』而不是一个
  `except` 分支。宽 except 不是偷懒，是边界语义：外面的人不该知道里面有多少
  种失败方式。」

### 4.3 清理边界：close 永远 best-effort

```python
def close(self):
    try:
        self._save_workspace()   # 回传失败只记日志，不影响销毁
    except Exception:
        log.warning(...)
    try:
        self._sbx.kill()
    except Exception:
        pass
    self._sbx = None
```

- **原则：清理代码不允许抛出** —— 清理失败不能掩盖业务结果；但**必须留日志**，
  否则"静默失败"会让你在排障时抓瞎（见故障实录 1 —— 正是 close 缺失导致的
  连环事故）。
- 服务端 `finally` 里还包了一层 `asyncio.to_thread(runner.close)` + try/except
  + debug 日志：**异步服务里，同步的清理必须丢线程池，不能阻塞事件循环**。
- **面试怎么讲**：「清理路径的三条铁律：不抛异常、必须记日志、不能阻塞主路径。
  而且 close 要放在 finally —— 任何 return/异常/客户端断连都逃不掉它。我们
  沙箱泄漏事故就是没有 finally 的代价，加了之后平台 VM 计数归零。」

### 4.4 线程边界：asyncio + 同步 SDK 的集中转换

e2b SDK 是同步的，工具是 async 的。所有 SDK 调用统一
`await asyncio.to_thread(sync_fn)`，把线程切换集中出现在沙箱代码里，工具事件
循环不被阻塞。

- **面试怎么讲**：「异步代码里混同步阻塞库，最干净的做法是**在适配层集中
  to_thread**，而不是让每个调用点自己决定 —— 这样线程池的行为、超时、异常
  归一都只有一个地方管。」

---

## 五、一致性设计：两个"呕心沥血"的坑

### 5.1 坑 A：先写后装载 —— 首次 load 会把刚写的文件清掉

**事故**：文件工具先执行（write 经 files API 写进沙箱），然后模型第一次跑
bash → `run()` 触发 `_load_workspace` → 里面是
`rm -rf /workspace && tar -xzf ...` → **刚写的文件被 rm 清掉**。表现是
"write 成功但 bash 看不到"。

**修复**：所有 fs 操作前先确保 workspace 已装载 —— `SandboxFS._sbx()` 统一做
`runner._load_workspace(host_root)`，而 `_load_workspace` 是**幂等**的
（已装载且根相同 → 直接返回）。于是"先写后跑"和"先跑后写"两种顺序都安全。

- **面试怎么讲**：「这是个顺序耦合 bug：装载操作会清空目标目录，所以任何先于
  装载的写入都会丢。修法是让装载幂等 + 所有文件操作前置装载 —— 把『顺序必须
  正确』的隐式约定变成『任何顺序都正确』的系统性质。幂等是消除顺序耦合的
  第一工具。」

### 5.2 坑 B：to_thread 接协程 —— 函数体永远不执行

```python
# 错：把 async 协程函数传给 to_thread —— 协程从未被 await，函数体永不执行
async def _write():
    path.write_bytes(data)
await asyncio.to_thread(_write)   # RuntimeWarning: coroutine never awaited

# 对：to_thread 收普通函数
def _write():
    path.write_bytes(data)
await asyncio.to_thread(_write)

# 另一个同族错：先同步求值再传 int
await asyncio.to_thread(path.stat().st_size)   # to_thread 收到一个 int！
```

- **教训**：`asyncio.to_thread` 接收的是**可调用对象**，不是值也不是协程。
  这类 bug 不报错、不崩溃，只有 `RuntimeWarning: coroutine was never awaited`，
  文件静默不写 —— 是最阴险的静默失败。
- **面试怎么讲**：「我在 review 里抓到过自己犯的 to_thread 坑：把 async 闭包
  传给线程池，函数体永不执行，只有一条 RuntimeWarning。这类"不炸但也不干活"
  的 bug 比异常更难查 —— 所以我的 LocalFS/SandboxFS 里所有线程包装全用纯同步
  闭包，并且回归测试从文件落盘断言（写没写进去是测试能直接看出来的）。」

---

## 六、故障排查实录（真实事故复盘）

### 实录 1：平台 500 "no more resource" —— 沙箱泄漏连环事故

**症状**：模型任务突然所有工具调用失败；服务日志里 POST /sandboxes 连续 500。

**排查链**（体现"先分层，再定位"）：
1. 服务日志看到 500，但 500 是平台的 —— 先**隔离问题层**：直接用 SDK 建沙箱
   复现 → `SandboxException 500: no more resource` —— 问题不在 pi-py，在平台。
2. `cubemastercli list` → 发现 **7 个 running VM 堆积** —— 一堆旧轮次的沙箱
   没被销毁。
3. 核对时间戳：7 个 VM 全部创建于老服务运行期间；新服务（已加 close）运行后
   VM 数不再增长 —— **证明修复生效，泄漏源自旧代码没有 close**。
4. 清理：`DELETE /sandboxes/{id}` × 7 → 内存回落、创建恢复 71ms。

**复用价值**：这个排查链的每一步都可迁移 —— 先复现、再隔离、再核对时间线
（文件 mtime vs 进程启动时间 判断"服务到底加载了哪版代码"，这是判断热部署
是否生效的经典手法）。

### 实录 2：模型"感知到环境重置" —— 用症状反推架构缺陷

**症状**：第二轮任务时，真实模型在输出里说"建议释放/恢复沙箱资源后重跑" ——
**模型自己发现上一轮写的文件不见了**。

**反推**：轮次间文件保持依赖 close 回传 + 下次 load；文件丢了 = 回传路径断了。
进而定位到：服务端 run_turn 结束时从不调 close → 工作区从未回传宿主 → 每轮
面对空工作区。**模型是很好的端到端探针** —— 它读不到文件就会如实说出来。

- **面试怎么讲**：「我用人话复述那次排障：模型的输出本身变成了探针 —— 它说
  『环境被重置』，我顺着『为什么文件会丢』这条链反推，十分钟定位到 close 缺失。
  端到端验证里，真实模型的反馈比任何 mock 都早暴露架构缺口。」

### 实录 3："看起来成功"的陷阱 —— 模型叙事性输出 ≠ 执行成功

**教训**：第一次重跑时，模型输出了完整的代码升级描述（像是成功了），但宿主
工作区空无一物。因为当时平台资源被泄漏的 VM 占满，**所有工具实际都失败了**，
模型只是在"描述它打算做什么"。

**修正**：验证**永远以物证为准** —— 检查宿主工作区的文件内容和平台 VM 计数，
而不是看模型说了什么。这也是为什么端到端脚本最后一步是
`cat summary.txt` + `ls 宿主工作区`。

- **面试怎么讲**：「agent 的文本输出是叙事，不是事实。我验证沙箱闭环只看两样
  硬证据：产物文件是否落盘、平台资源计数是否归零。『叙事性成功』是我这次最
  深刻的教训 —— 也解释了为什么我的验证脚本断言文件内容而不是断言文本。」

---

## 七、验证分层：从单元到真实模型，每一层都要能独立跑

1. **LocalFS 工具回归**（13 例）：快、无外部依赖 —— 保证五种模式共用工具代码
   不被沙箱改造破坏。
2. **SandboxFS 组合测试**（11 例）：真沙箱上 write→bash 可见、read 回读、
   edit 沙箱内、越界拒绝、close 回传 —— 验证抽象在真后端的语义。
3. **standalone runner 测试**：会话级工作区 + 快照/rollback 回滚点。
4. **集成脚本**（fake 模型 + 全栈）：审计、轨迹、消息序、回传。
5. **真实模型端到端**：qwen3.8-flash × 批准流 × 两轮任务 × 宿主物证。

**分层理由**：每层失败都对应一个明确的排障范围 —— 第 3 层挂 = 沙箱封装问题，
第 1 层挂 = 工具逻辑问题，第 5 层挂 = 链路/模型问题。**不允许跨层跳**：
fake 没过就跑真实模型，会被"叙事性成功"骗。

---

## 八、面试速查（30 秒版）

| 话题 | 一句话 |
|---|---|
| 为什么拆池 | 冷启动 0.1s 后，池化的复杂度不再值得；生命周期收敛进 finally |
| 为什么工作区进沙箱 | 执行视图与路径视图必须一致，否则模型路径认知分裂 |
| 越界怎么防 | 两层：policy 声明式管控+审计；fs 数据通路兜底。互不依赖 |
| 安全降级 | 查询静默降级（安全默认值），写入显式失败 —— 读写不对称 |
| 探测命令 | 必然 exit 0，结果编码在 stdout，否则 SDK 抛异常误报平台故障 |
| 异常归一 | 外部边界一把梭转 CommandResult；工具永不裸抛；清理永不抛 |
| 异步+同步 SDK | 适配层集中 to_thread，别让每个调用点自己决定 |
| 幂等装载 | 装载是破坏性操作（清目录），幂等化后任何调用顺序都安全 |
| to_thread 坑 | 传协程/传值都是错，只收普通可调用对象；靠文件落盘断言测试 |
| 事故复盘能力 | 先复现、再隔离层、核对时间线；物证验证，不听模型叙事 |

---

## 九、会话闭环归档：让"agent 干了什么"可审计、可还原

沙箱层做完后补的最后一块：**会话闭环存档**（代码 `src/pi/server/archive.py`）。

### 9.1 为什么需要归档

沙箱销毁后，工作区回传宿主只是"文件还在"，但**"这一个会话改了什么"**没有记录：
哪些文件是本会话新建的？哪些被修改？这既是合规诉求（企业审计），也是排查诉求
（怀疑 agent 改了东西时能快速还原现场）。归档 = 给沙箱会话补上"提交记录"。

### 9.2 设计：基线 + 差异，而不是只存结束态

因为工作区是用户级共享目录（多会话同目录），只存结束态无法回答"本会话改了什么"。
于是：

```
turn 开始         snapshot_files(cwd)   -> 基线清单 [(rel, size, sha1), ...]
turn 结束 finally archive_workspace(...) -> tar.gz 全量 + diff(基线, 结束态)
diff = {added: [...], modified: [...], deleted: [...], unchanged: n}
```

- 用 **sha1 内容哈希**判定 modified（size 会骗人：同大小不同内容）。
- 元数据 json 与 tar.gz 同名存放（archive_root/`<session>-<utc-ts>.{tar.gz,json}`），
  文件名自带时间戳，天然按时间可回放。
- tar 用 `filter=_exclude` 排除构建目录（与沙箱装载同一套 SKIP 规则，保持一致）。

### 9.3 三个工程决策（面试点）

1. **归档必须在 finally，且必须 best-effort**：`archive_workspace` 内部全 try/except，
   失败只记 warning，绝不改变 turn 的结果 —— 归档是增强，不是主路径。
2. **归档开启策略：默认开，一行关**（`PI_ARCHIVE=0`）。对企业系统，审计默认开启
   是安全姿态；但必须有一行关闭的逃生门（磁盘或合规场景）。
3. **对象存储（MinIO/S3）接口惰性预留**：`_upload_s3` 在 `PI_ARCHIVE_S3_ENDPOINT`
   未配置时直接返回 `(False, None)`，零依赖零开销；配置了才 `import boto3`（惰性
   import，避免给没 S3 的部署强加依赖）。**接口存在、行为透明、当前 no-op** ——
   这是"为将来留能力但不预支复杂度"的标准写法。

### 9.4 实测证据（真实模型任务）

```
eval 任务 turn 结束自动生成:
  451ce466f7b6-...Z.tar.gz   (753B, 含 sales.csv + summary.md)
  .json: {"diff": {"added": ["summary.md"], "unchanged": 1}, ...}
```
"本会话新增了 summary.md、seed 文件未变" —— 一次 turn 的全部痕迹，一个 tar 包。

- **面试怎么讲**：「归档我做了三层：全量 tar 保还原、sha1 基线算差异、
  finally 挂载保不丢。默认开启、可一行关闭；S3 走惰性 boto3 —— 没配 MinIO
  的时候整条链路零额外依赖。『审计默认开』是我对安全产品的姿态。」

---

## 十、会话级沙箱生命周期：从"用完即毁"到"懒加载 + 复用 + 自适应回收"

### 10.1 演进动机（真实场景驱动的决策）

第一版方案 B 是"每轮任务一个沙箱、用完即毁"——零常驻、无泄漏，但暴露三个现实问题：

1. **纯聊天用户也被喂一个 VM**：每个回合 71ms 冷启 + 256MiB 峰值 + 归档回传，
   只为说几句无关工具的话 —— 资源白烧；
2. **回合间状态无法延续**：沙箱销毁 = VM 内的下载文件、中间产物、处理进度全丢。
   典型场景：MinIO 大文件拉进沙箱处理到一半，3 分钟后新回合又要重拉重算；
3. **常驻解耦**：每次回合都关 VM，等待方差大（close 的 tar 回传 90s 上限）。

结论：保留"每回合一个 VM 的思路"（隔离性不变），但把"创建"从**回合前**挪到
**第一次工具调用时**（懒加载），把"销毁"从**回合后**挪到**空闲超时/池满/压力**时
（复用 + 回收）。

### 10.2 设计：三个机制

**A. 懒加载（聊天零沙箱）**

- 回合以本地模式起步（`ctx.runner = None`，`ctx.ensure_runner = 惰性钩子`）；
- 模型第一次调用 `bash` 时钩子触发：现场创建/复用 VM，并把 runner/fs/metrics
  装配进工具上下文（与方案 B 的装配完全一致，只是时机后移）；
- 纯聊天回合永不触发：不建 VM、不装载工作区、不归档 —— 逐字节零开销。

**B. 会话级复用池（同会话跨回合延续状态）**

- 池按 `session_id` 绑定，**绝不跨会话共享**（数据隔离第一）；
- 回合结束不销毁：`save_workspace()` 把 VM 内 /workspace 回传宿主（归档需要、
  复用延续需要）→ 归还池（记 `last_used`）；
- 下一回合首调工具：池命中且 `is_alive()`（原始 SDK 命令通道探活，绕开
  工作区装载）→ 直接复用 —— **冷启 71ms 消失、VM 内文件/环境天然延续**，
  MinIO 拉过的大文件就在那，3 分钟后接着干；
- 僵尸处理：平台可能已回收长闲置 VM —— 复用前探活，死了就地销毁并静默重建。

**C. 自适应空闲回收（内存压力感知，两级 TTL）**

- 后台 sweeper 每 30s 扫描一次池：
  - `idle > 生效TTL` → close 销毁（联网沙箱 close = 回传 + 杀）;
  - 池容量 > `pool_size`（默认 4）→ LRU 淘汰最久未用的；
  - 宿主可用内存 < 低压阈值（默认 512MiB）→ **立即整体清空空闲 VM**；
- 生效 TTL 随宿主内存压力自适应（读 /proc/meminfo MemAvailable，无第三方依赖）：
  - 宽裕（>1.5GiB）→ `PI_SANDBOX_POOL_TTL`（默认 900s / 15min）
  - 紧张（<=1.5GiB）→ `PI_SANDBOX_POOL_TTL_TIGHT`（默认 300s / 5min）
  - 极紧（<=512MiB）→ 0 = 立刻回收全部
- 服务关闭时 `shutdown_pool()` 清池（应用 lifespan 收尾）；孤儿由平台自身
  TTL 兜底，不会泄漏。

### 10.3 配置一览

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `PI_SANDBOX_POOL_TTL` | 900 | 内存宽裕时空闲 VM 回收阈值（秒） |
| `PI_SANDBOX_POOL_TTL_TIGHT` | 300 | 内存紧张时回收阈值（秒） |
| `PI_SANDBOX_POOL_SIZE` | 4 | 常驻 VM 上限（LRU 淘汰超出部分） |
| `PI_SANDBOX_POOL_PRESSURE_HIGH` | 1572864000 (1.5GiB) | 触发收紧档的宿主可用内存 |
| `PI_SANDBOX_POOL_PRESSURE_LOW` | 536870912 (512MiB) | 触发"立即清空"的宿主可用内存 |

新增指标：`pi_sandbox_pool_hits_total`（复用命中）、`pi_sandbox_pool_evictions_total`
（容量/压力回收）。

### 10.4 实测证据

- 聊天回合（真实模型）：工具 0 次、平台 VM 保持 0 —— 零沙箱 ✓
- 工具回合：回合结束平台 VM = 1（**在池，未被销毁**）；同会话第二回
  create 计数不增、`pool_hits_total = 1` —— 复用 ✓
- 池满淘汰：`pool_size=1` 双会话轮流 → 先入池者被 LRU 淘汰（平台 VM 恒 1）✓
- TTL 回收：`TTL=TIGHT=10s`，回合归还后 45s（sweeper 30s + 裕量）平台
  VM → 0 ✓
- 压力自适应：宿主可用 1.27GiB < 1.5GiB → 生效 TTL 自动落到 300s 收紧档
  （单测三档判定全过）✓
- `shutdown_pool`：服务 SIGTERM 后平台 VM → 0 ✓
- 回归：真实模型 eval（web_fetch / data_clean）全 PASS，归档链路正常 ✓

---

## 附：当前验证状态

- ✅ LocalFS 工具回归 13/13
- ✅ SandboxFS 真实沙箱组合 11/11（越界拒绝、close 回传、写后 bash 可见）
- ✅ standalone：会话级工作区 + snapshot/rollback（VM 级文件状态恢复）
- ✅ 集成 e2e（fake 模型全栈）
- ✅ 真实模型端到端：analyze.py R1 创建 → R2 升级 median → 宿主回传
  `mean: 30.00 / median: 30.00`，平台残留 VM = 0
- ✅ 会话闭环归档：turn 结束自动 tar.gz+json（sha1 差异基线；MinIO 惰性接口）
- ✅ 企业 eval（真实模型 qwen3.8-flash）：**5/5 任务 PASS** —— 数据分析 4 工具/27s、
  日志解析 5 工具/19s、开源情报抓取 20 工具/200s（公网）、产品文档摘要 5 工具/29s、
  数据清洗 9 工具/50s；每任务独立用户隔离工作区，物证断言全过；9 个 turn 全部
  自动归档（diff 精确到"本 turn 新增了哪些文件"）
- ✅ 会话级沙箱生命周期（第十章）：聊天零沙箱 / 同会话复用（create 不增、
  pool_hits≥1）/ 池满 LRU 淘汰 / TTL 空闲回收 / 内存压力自适应档位 /
  shutdown_pool 服务关闭清池 —— 全部端到端实测 ✓

> 排障教训（附）：平台 `cubemastercli list` 与 3000 端口 API 的数据视图不同步，
> 曾据 CLI 误判"沙箱泄漏"—— 实际 API 层沙箱早已正常 close（DELETE 204 在服务
> 日志）。排查资源类问题要以**真实执行路径的观测点**（API/日志）为准。顺带修复
> 一个真缺陷：`close()` 里 kill 失败原本静默 pass（违反"清理必留日志"原则），
> 已改为 warning 日志。