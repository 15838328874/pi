# 环境说明：生产（`pi_py`）与测试（`pi_py_test`）

> 本文回答一个问题：**"我现在连的到底是哪个环境，以及两个环境差在哪。"**
> 全部数据 2026-09-06 在本机实测核实，不是推断。相关文档：
> `deploy/cloud-deploy.md`（云上部署步骤）、`README.md` §Shared test database（简版流程）、
> `HANDOFF.platform.md` §1（环境坑）。
>
> **脱敏说明**：本文的公网 IP 一律写成 `<TEST_PUBLIC_IP>` 占位符，真实值只活在
> `.env.test`（git-ignored）里。占位符**不削弱任何一条结论**——L3/L5 讲的是"明文 HTTP +
> 公网 IP + 开放注册 + 弱默认密码"这个**形状**为什么危险，与具体是哪个 IP 无关。
> 播种工具的默认密码 `pi-test-123` 刻意**没有**打码：它是 `tools/seed_testdb.py:170`
> 的源码默认值，本来就随仓库公开，在文档里假装它是秘密属于安全表演。真正的修法是
> L5 末尾那两步（改密码 + 收窄监听），不是把它从文档里抹掉。

---

## 0. 一句话现状

**当前唯一在跑的实例是测试环境**，不是生产环境：

```
PID 809332  pi-py serve --host 0.0.0.0 --port 8398   （2026-09-05 19:46 启动）
  → MySQL schema  pi_py_test        （不是 .env 里的 pi_py）
  → Redis NS      test
  → workspace     /root/.pi-py/workspaces-test
  → audit         /root/.pi-py/audit-test.jsonl
  → Milvus NS     it
```

生产 schema `pi_py` **从未启用**：0 用户、0 会话、0 消息、0 用量记录，且落后 5 个迁移
（见地雷 **L1**）。`/root/.pi-py/` 下也没有 `audit-2026-09-06.jsonl`（只有 `audit-test-*`），
说明生产进程今天没跑过。

**别把"服务在跑"当成"生产在跑"。** 端口 8398 = 测试，8300 = 生产（约定见 README）。

---

## 1. 怎么确认一个跑着的进程属于哪个环境

`.env` 只是**候选之一**，进程的真实配置以它自己的环境变量为准。直接读 `/proc`：

```bash
PID=$(pgrep -f 'pi-py serve')
tr '\0' '\n' < /proc/$PID/environ | grep -E '^(PI_DATABASE_URL|PI_REDIS_NS|PI_WORKSPACE_ROOT|PI_AUDIT_PATH|PI_MILVUS_NS|PI_PUBLIC_BASE_URL)='
ls -l /proc/$PID/cwd          # .env 是按 cwd 加载的，cwd 不对就什么都没加载
```

看 `PI_DATABASE_URL` 结尾是 `pi_py` 还是 `pi_py_test`，一眼定论。
**不要**用 `cat .env` 来判断 —— 那只能说明"如果从零启动会连哪"，说明不了现在连着哪。

对库直接确认（只读）：

```bash
.venv/bin/python -c "
import asyncio, pi
from pi.server.config import ServerSettings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def m():
    e = create_async_engine(ServerSettings.from_env().database_url)
    async with e.connect() as c:
        for s in ('pi_py','pi_py_test'):
            v = (await c.execute(text(f'SELECT version_num FROM \`{s}\`.alembic_version'))).scalars().all()
            n = (await c.execute(text(f'SELECT COUNT(*) FROM \`{s}\`.users'))).scalar()
            print(f'{s}: alembic={v} users={n}')
    await e.dispose()
asyncio.run(m())"
```

---

## 2. 两个环境的完整对照

`.env` = 完整配置（32 个键）。`.env.test` = **只有 6 个键的覆盖层**，其余全部从 `.env` 继承。

| 键 | 生产 `.env` | 测试 `.env.test` | 隔离？ |
|---|---|---|---|
| `PI_DATABASE_URL` | `…:3306/pi_py` | `…:3306/pi_py_test` | ✅ 同实例不同 schema |
| `PI_REDIS_NS` | `prod` | `test` | ✅ 同实例不同前缀 |
| `PI_WORKSPACE_ROOT` | `/root/.pi-py/workspaces` | `/root/.pi-py/workspaces-test` | ✅ |
| `PI_AUDIT_PATH` | `/root/.pi-py/audit.jsonl` | `/root/.pi-py/audit-test.jsonl` | ✅ |
| `PI_MILVUS_NS` | `pi` | `it` | ✅ 同集群不同 collection |
| `PI_PUBLIC_BASE_URL` | **未设置** | `http://<TEST_PUBLIC_IP>:8398` | ⚠️ 见 **L3** |
| `PI_JWT_SECRET` | 有 | **未覆盖 → 与生产同一个** | ❌ 见 **L4** |
| `PI_REDIS_URL` | 有（含密码） | **未覆盖 → 同一个** | ❌ 仅靠 NS 分隔 |
| `PI_MILVUS_TOKEN` | 有 | **未覆盖 → 同一个** | ❌ 仅靠 NS 分隔 |
| `OPENAI_API_KEY` / `BASE_URL` | 有 | **未覆盖 → 同一个** | ❌ 共用网关与配额 |
| `PI_MODEL` / `PI_MEMORY_MODEL` / `PI_MEMORY_ARBITER_MODEL` | `openai/qwen-flash` ×2 + `qwen-plus` | **未覆盖 → 同一套** | ❌ 测试跑真模型 = 花真钱 |
| `PI_SANDBOX*` / `PI_POLICY` / `PI_TRACER` / 容量各项 | 见 `.env` | **未覆盖 → 同一套** | — 本就无需隔离 |

两个 env 文件权限都是 `600`，都被 `.gitignore` 忽略。

**结论：隔离的是"数据落点"，没隔离的是"凭据与密钥"。** 测试进程内存里握着全套生产凭据。
`PI_SANDBOX=docker` 挡住了工具执行路径；曾经跑在 app 进程内、绕过沙箱的
`web_fetch`/`web_search` **已被删除**（不是禁用），联网改由模型端点自己的 builtin tools
承担，见 ARCHITECTURE §7.2 与 §17 第 20 条。

---

## 3. 启动流程

### 3.1 为什么必须手动 source `.env.test`

`src/pi/__init__.py::_load_env_file()` 的候选列表**只有三个**，`.env.test` 不在其中：

```python
_ENV_FILE_CANDIDATES = (Path(".pi-py.env"), Path(".env"), Path.home()/".pi-py"/".env")
```

而且是 `if key not in os.environ` —— **已存在的环境变量永远优先，`.env` 只能填空**。
两条合起来的后果：

- 直接 `pi-py serve` → 只加载 `.env` → 连**生产** `pi_py`。
- 先 `set -a; . ./.env.test; set +a` 再 serve → 那 6 个键已在 environ 里，`.env` 填不进去 →
  连**测试** `pi_py_test`，但密钥仍从 `.env` 补齐。

`.pi-py.env` 和 `~/.pi-py/.env` 目前都不存在。若哪天创建了 `.pi-py.env`，它会**压过 `.env`**
（列表第一个命中即 `return`），排查配置时要记得这一层。

### 3.2 测试环境（当前在跑的这套）

```bash
cd /root/pi/pi-python
set -a; . ./.env; . ./.env.test; set +a     # 顺序不能反：后 source 的覆盖先 source 的
.venv/bin/pi-py migrate                      # 只升级 pi_py_test
.venv/bin/pi-py serve --host 0.0.0.0 --port 8398
```

播种测试数据（幂等；`--reset` 先清库，`--users N` 调用户池，`--password` 改密码）：

```bash
.venv/bin/python tools/seed_testdb.py
```

该脚本**拒绝**在 `PI_DATABASE_URL` 不指向 `*_test` schema 时运行 —— 忘 source 也炸不到生产数据。

### 3.3 生产环境

```bash
cd /root/pi/pi-python
.venv/bin/pi-py migrate                      # ⚠️ 先读地雷 L1，当前 prod 落后 5 个迁移
.venv/bin/pi-py serve --host 0.0.0.0 --port 8300
```

完整云上步骤、RDS/Redis 安全组、4 vCPU 调优见 `deploy/cloud-deploy.md`。

### 3.4 环境要点（沿用 `HANDOFF.md` §1，仍然全部有效）

| 事项 | 说明 |
|---|---|
| Python | `python` **不在 PATH**，用 `/root/pi/pi-python/.venv/bin/python`（3.12.3） |
| Node | 在 `/usr/local/node/bin`，每次 `export PATH=/usr/local/node/bin:$PATH` |
| cwd | `.env` 按 cwd 加载，命令必须在 `pi-python/` 下执行 |
| **git 仓库** | ✅ 2026-09-06 21:10 起有首个提交（`6eb79d7`，分支 `main`，remote = `github.com/15838328874/pi`）。`git diff`/`checkout`/`stash` 现在**真的能用**。但**改前仍建议 `cp` 备份**：可能有并发会话在改同一批文件，见 L8 |
| 浏览器 | 环境里没有浏览器，前端只能靠 `vue-tsc` + vitest + SSR 渲染测试 |

---

## 4. 已知地雷

### L1 · 生产 schema 落后 5 个迁移，而 `create_all` 只建表不补列 ⚠️ 最严重

实测状态：

| schema | alembic_version | 表 |
|---|---|---|
| `pi_py`（生产） | **`0002_user_active`** | 5 张：`users` `sessions` `messages` `usage_records` `alembic_version` |
| `pi_py_test`（测试） | `0007_trace_fidelity`（= head） | 9 张：另有 `agent_runs` `agent_steps` `audit_events` `user_memories` |

缺的迁移：`0003_session_plan`、`0004_user_memories`、`0005_audit_events`、`0006_agent_runs`、
`0007_trace_fidelity`。

**为什么这会静默地炸而不是启动就报错**：`src/pi/server/app.py:727` 的 lifespan 调
`db.init()`，而 `src/pi/server/db.py:250` 是 `Base.metadata.create_all`。SQLAlchemy 的
`create_all` **只创建不存在的表，绝不 ALTER 已存在的表**。所以拿现在的代码对 `pi_py` 启动会：

1. 静默补出 `agent_runs`/`agent_steps`/`audit_events`/`user_memories` 四张表（看着像成功了）；
   注意它是按**当前 ORM 元数据**建的，也就是 `0007` 的最终形状（`agent_runs.prompt`、
   `agent_steps.args` 等列一应俱全）；
2. `alembic_version` 仍停在 `0002`，与实际 schema 不再对应。之后再跑 `pi-py migrate` 会从
   `0003` 重放到 `0007`，而重放路径是**坏的**：`0003` 是 `add_column("sessions","plan")`
   → 成功（列确实缺）；`0004`/`0005`/`0006` 是 `create_table` → **撞已存在的表直接报错**；
   即便跳过，`0007` 的 `add_column("agent_runs","prompt")` 也会撞 create_all 已经建好的同名列。
   也就是说一旦让 `create_all` 先跑过，就再也无法用 alembic 干净地升到 head，
   只能手工修 `alembic_version` 或重建 schema；
3. **`sessions.plan` 列不会被加上**（`0003` 是 `ADD COLUMN`，不是建表，而 `sessions` 表已存在）。
   实测 `pi_py.sessions` 的列就是 `id, user_id, title, model, cwd, created_at` —— 没有 `plan`。

第 3 条是硬故障：`SessionRepo` 的 `for_user()` / `by_id()` / `list_for_user()` 全部是
`select(SessionRow)` 整行加载，映射里含 `plan`。列不存在 → MySQL 报
`Unknown column 'sessions.plan' in 'field list'` → **每一次会话读写都失败**，不只是
`submit_plan` 那条路径。

而且这个失败发生在启动**之后**的第一个请求，健康检查全是绿的：`/healthz` 不碰库，
`/readyz`（`app.py:843`）的 `db` 检查只做 `await users.count()`，即 `SELECT count(*) FROM users`
—— 只碰 `users` 表，而这张表在生产是存在的。所以 `readyz` 报 `db: ok`，
服务看着完全正常，直到有人调 `GET /v1/sessions` 才炸。

**正确顺序**：先 `pi-py migrate` 升到 `0007`，再启动。**不要**靠 `create_all` 兜底 ——
它会把 schema 带进一个 alembic 认不出来的中间态。

**好消息：这个窗口现在还开着。** 实测 `pi_py` 只有 5 张表（`users` `sessions` `messages`
`usage_records` `alembic_version`），说明**当前代码的 `create_all` 从未对生产跑过** ——
schema 仍然与 `0002` 自洽。现在执行 `pi-py migrate`（在只 source 了 `.env`、没 source
`.env.test` 的 shell 里）可以干净地一路升到 `0007`。**一旦有人不小心对生产启动过一次服务，
上面第 2 条就会生效，此后只能手工收拾。** 这也是为什么启动前要按 §1 先确认连的是哪个库。

> `deploy/cloud-deploy.md` §1 第 4 项已记录"schema 在 alembic 0002，数据为空"，
> 但没写出上面这层后果。本节是它的补充。

### L2 · 端口 8300 与 8398

**代码与配置里的默认值全部是 8300，无一例外**；8398 只是文档里约定的测试端口，
没有任何一处代码默认它。实测（2026-09-06 全仓 grep）：

| 位置 | 值 |
|---|---|
| `src/pi/cli.py:26` | `--port` 默认 **8300** |
| `docker-compose.yml` / `docker-compose.cloud.yml` | `8300:8300` |
| `web/vite.config.ts:8` | `PI_API ?? "http://127.0.0.1:8300"` |
| `web/package.json` `gen:api:live` | `http://127.0.0.1:8300/openapi.json` |
| `web/tests/live/api.live.ts:5,11,37` | `PI_LIVE_API ?? "http://127.0.0.1:8300"` |
| `tools/loadtest.py` 用法示例 | `--url http://127.0.0.1:8300` |
| README 快速开始 / curl 示例 / §Web UI | 8300 |
| `src/pi/server/app.py:888`、`web/openapi.json:2138` | 文档字符串里写死 "8300 must not be reachable from the internet" |
| **README:378** | `pi-py serve --port 8398  # leave 8300 for production` ← 仅约定 |
| **ARCHITECTURE.md:1027** | `pi-py serve --port 8398  # 别占用生产的 8300` ← 仅约定 |

后果：测试环境跑在 8398 上时，凡是**依赖默认值**的工具都会指向一个没人监听的端口 ——
`npm run dev`（vite :5173 代理到 :8300）、`npm run gen:api:live`、`npm run test:live`、
`tools/loadtest.py`。对着 8398 干活必须显式覆盖：

```bash
export PATH=/usr/local/node/bin:$PATH
PI_API=http://127.0.0.1:8398 npm run dev
PI_LIVE_API=http://127.0.0.1:8398 npm run test:live
.venv/bin/python tools/loadtest.py --url http://127.0.0.1:8398 --users 20 --rounds 3
.venv/bin/python tools/dump_openapi.py        # 不需要活服务，刷 openapi.json 优先用这个
```

`app.py:888` 那句面向用户的文档字符串把 8300 写进了 OpenAPI 契约（`web/openapi.json:2138`、
`web/src/api/schema.d.ts:265`），所以它不只是注释 —— 改端口口径会连带改契约，
要重新 `dump_openapi.py` + `npm run gen:api`。

### L3 · 文件附件特性：生产被关掉，且两份手册里一个字都没提

这是一个**端到端已交付**的特性，但 `README.md` 和 `ARCHITECTURE.md` 里 grep 不到
`attachment` / `upload` / `FileBlock` / `/files` 任何一个词。实际覆盖面：

| 层 | 位置 |
|---|---|
| 消息模型 | `src/pi/models.py:45 FileBlock`，已进 `AnyBlock` 判别联合（`:59`、`:62`） |
| 路由 | `POST /v1/sessions/{id}/files`（上传，`app.py:1141`）、`GET …/files`（列表，`:1170`）、`GET /files/{session_id}/{name}`（下载，`:1194`） |
| 契约 | 已在 `web/openapi.json` 里 |
| 前端 | `web/src/api/endpoints.ts:60,66,171,182` + `ChatView.vue` 的文件面板与待发 chips |
| 测试 | `tests/test_model_capabilities.py`（14 例，**整个文件没进 ARCHITECTURE §15 的表**）+ `test_server.py::TestSessionFiles` |
| 依赖 | `pyproject.toml` 为此把 `python-multipart` 提为**运行时**依赖 |
| 限额 | `PI_MAX_UPLOAD_BYTES`，默认 20 MiB（`config.py:46`） |

**生产环境这个特性是关闭的**：`PI_PUBLIC_BASE_URL` 只在 `.env.test` 里有，生产 `.env` 没设。
未设置时 `_file_blocks()`（`app.py:1119`）和上传路由（`:1151`）直接返回
400 `"file attachments/uploads are disabled: PI_PUBLIC_BASE_URL is not set"`。
如果这是有意的，`.env` 里该写一行注释说明；如果不是，得补上生产的对外地址。

测试环境的值是 `http://<TEST_PUBLIC_IP>:8398`：**明文 HTTP + 公网 IP + 测试端口**。它会被拼进
`FileBlock.file_url` 发给模型网关，也会返回给前端，所以浏览器和网关都直接去公网 IP 取文件。
生产若照抄这个形状，等于让附件走明文公网。生产应当走 `deploy/Caddyfile` 的 TLS 域名。

#### 下载路由是**故意无认证**的，安全性全押在 session id 上

`app.py:1194` 的 docstring 写得很清楚：模型网关必须无 token 抓取，所以
"不可猜的 session id + 文件名" 就是 bearer capability，每次命中都以会话属主的名义审计。
这是有意的设计，不是漏洞。但要把三件事一起看清楚：

1. **session id 只有 48 位熵**。`src/pi/server/db.py:360` 是 `uuid.uuid4().hex[:12]` ——
   取 uuid4 十六进制的**前 12 位**，即 48 个随机 bit（版本位在第 13 位，没被截进来）。
   列类型是 `String(16)`，所以还有余量。docstring 说的"不可猜"，实际强度是 2⁴⁸ ≈ 2.8×10¹⁴。
   对单个目标做在线爆破不现实，但这比字面意思弱得多，别把它当成 128 位。
2. **这条路由没有限流**。`RateLimiter` 在整个 `app.py` 里只挂在**一处** —— `POST /v1/sessions/{id}/runs`
   （`:676` 构造，`:1234`/`:1253` 返回 429）。无认证的下载路由、注册、登录都不在限流范围内。
   配合 **L5**（端口对公网开放），枚举尝试没有任何节流。
3. **capability URL 会离开这栋楼**。`FileBlock.file_url` 随消息持久化进 `messages.blocks`，
   并发给第三方网关（阿里云 MaaS）。任何能看到 transcript 的人 —— 包括网关侧日志 ——
   都能拿这个 URL 直接下载文件，不需要任何凭据。这是"让模型能读附件"的固有代价，
   但值得在暴露公网前想清楚。

路径穿越是防住的：`_clean_file_name()`（`app.py:64`）取 basename、拒绝 `.`/`..`/NUL、
用 `_FILENAME_RE` 卡字符集、限长度，不合规直接 400。且只暴露
`{cwd}/uploads/{session_id}/` 下的文件 —— 工作区里 agent 自己写的文件不在这条路由的可达范围内
（`_uploads_dir()` 的 docstring 明确了这一点）。

**要收紧的话，最低成本的两步**：把 session id 加长到 `hex[:16]`（64 位，列宽够），
以及给 `/files/` 加一个按 IP 的限流。两者都不改契约。

### L4 · JWT secret 与 Redis NS 的隔离不对称

`.env.test` **不覆盖 `PI_JWT_SECRET`**，所以两个环境用**同一个 HS256 密钥**签 token；
而撤销状态存在 Redis 里，Redis 是**按 NS 隔离**的（`prod:` / `test:`）。

`src/pi/server/app.py:801 current_user()` 的顺序是：解 token → 查 `revoked:{jti}` →
查 `epoch:{username}` → **再查库** `users.by_username()`。于是：

- 测试环境签发的 token 在生产环境**能通过密码学校验**（同密钥）；
- 但最终仍要求该 username 在**那个环境的库里存在且 active**，所以目前不会真的越权
  （`pi_py.users` 是空的）；
- **真正的缺口是撤销不互通**：在测试环境 `logout`（写 `test:revoked:{jti}`）或
  `admin/revoke`（写 `test:epoch:{user}`）之后，同一个 token 拿到生产环境去用，
  生产查的是 `prod:*`，查不到撤销记录 → **依然有效**。反过来也一样。

一旦生产 schema 被播种出与测试相同的用户名（`seed_testdb.py` 的默认名单是
`admin`/`alice`/`bob`/`carol`/…，很容易撞），"测试 token 在生产可用"就从理论变成事实。

**建议**（属于 #42 密钥轮换 / #43 云侧暴露面的范畴，未授权自动执行）：给 `.env.test` 补一个
独立的 `PI_JWT_SECRET`。这一行就能把两个环境的 token 彻底隔开，代价是零。

### L5 · 测试账号用的是公开的默认弱密码，而端口对公网开放

`tools/seed_testdb.py:170` 的 `--password` 默认值是 `pi-test-123`，**这个值写在 README 里**。
实测 `pi_py_test` 中 6 个播种账号（`admin`/`alice`/`bob`/`carol`/`overquota`/`disabled`）
的密码全部就是它 —— 包括 `is_admin=1` 的那个。

同时服务是 `--host 0.0.0.0 --port 8398`，`PI_PUBLIC_BASE_URL` 里的 `<TEST_PUBLIC_IP>` 是公网 IP，
而 README 自己就写明注册是**开放且无认证**的。三者叠加 = 一个用公开默认密码的管理员账号
挂在公网上。

`.env` 注释里已经记了 ECS 安全组应当限定来源 IP（`cloud-deploy.md` §1 第 5 项标着
"☐ 待你确认"）—— **这一项至今未确认**。若安全组确实没限流，建议至少：
换掉 `admin` 的密码（一条 `UPDATE users SET password_hash=… WHERE username='admin'`，
哈希用 `pi.server.auth.hash_password()` 生成），并考虑把 serve 绑到 `127.0.0.1`。

改密码后旧 token 不会立即失效（校验只看密钥 + 撤销表 + `is_active`，不比对密码），
要立刻踢下线得同时调 `POST /v1/admin/users/{u}/revoke` 抬 epoch。

### L6 · `smokeweb` 账号来源不明，密码不可恢复

`pi_py_test.users` 里 id=29 的 `smokeweb`（2026-09-05 创建，1 个 session）**不属于播种批次**
（创建时间与其余 6 个差两天），且在**整个仓库里 grep 不到这个名字** —— 不是脚本或测试建的，
是某次手工/浏览器冒烟注册留下的。

密码是 PBKDF2（200k 次迭代，`src/pi/server/auth.py`），**单向、无法反解**，候选字典也没命中。
要用就只能重置。这类残留账号建议随手清掉，否则 `pi_py_test` 会慢慢攒出一堆没人认得的账号。

### L7 · 文档里的计数大面积漂移（已于 2026-09-06 全部修正）

不是"三处"，是**十几处**，而且同一份文档内部互相矛盾（ARCHITECTURE 里同时存在
174 / 291 / 332 / 333 四个不同的测试总数）。全部实测值与修正位置：

| 位置 | 原值 | 实测 / 已改为 |
|---|---|---|
| `README.md` §Layout | `tests/ 174 tests` | **348** |
| `README.md` §Layout | `migrations/ 0001–0003` | **0001–0007**（head `0007_trace_fidelity`） |
| `README.md` §Layout | 无 `memory/`、无 `tools/rebuild_milvus.py` | 已补 |
| `README.md` §Testing | `173 passed, 1 skipped` | **347 passed, 1 skipped** |
| `README.md` §Testing 前端 | `43 tests` | **55** |
| `README.md` §Testing live | `7 opt-in tests` | **11** |
| `README.md` §Multi-user server | API 清单 11 条 | **23 条业务路由**（见 **L10**） |
| `ARCHITECTURE.md` 头部 | `8,600 行 Python` | **9,672** |
| `ARCHITECTURE.md` 头部 | `332 个离线测试（332 passed / 1 skipped）` | **348（347 passed / 1 skipped）** |
| `ARCHITECTURE.md` 头部 | 前端 `手写约 1,500 行 + 测试约 700 行` | **手写 3,231 行 + 测试 1,041 行** |
| `ARCHITECTURE.md` 头部 | `43 个单测 / 7 个联调用例` | **55 / 11** |
| `ARCHITECTURE.md` §3.3 目录结构 | `tests/ 174 个测试`、`migrations 0001–0003`、无 `memory/` | **348**、**0001–0007**、已补 `memory/` 七个模块 + `rebuild_milvus.py` |
| `ARCHITECTURE.md` §15 | `332 passed` / `共 333 例` / 尾注 `291 例` | **347 passed** / **共 348 例** |
| `ARCHITECTURE.md` §15 表 | `test_server.py` **92** | **76**（21 个测试类 / 70 个函数，参数化后 76；文档列的 9 个类**全部仍在**，没有丢覆盖率，纯粹是数字陈旧） |
| `ARCHITECTURE.md` §15 表 | `test_security.py` **30** | **31** |
| `ARCHITECTURE.md` §15 表 | **缺 `test_model_capabilities.py`** | 已补 **14** 例（模型原生能力 / 会话附件，见 **L3**）。补完后逐行相加 = **348**，与 `--collect-only` 一致 |
| `ARCHITECTURE.md` §18.5 | `api.live.ts`（7 例） | **11 例** |
| `ARCHITECTURE.md` §18.5 | `render.test.ts`(13) | **(25)**（`sse.test.ts` 9 与 `transcript.test.ts` 21 未变；13+9+21=43 正是旧总数的来源） |

核实过的**未漂移**项（不用改）：`test_security.py::TestShippedPolicy` 的
`MUST_DENY` = **22 条**、`MUST_ALLOW` = **15 条**，与 README 和 ARCHITECTURE §17.18 的
说法一致（实测 `len()` 确认）。

数字会随开发漂移，**别把它当验收标准**。要基线就跑：

```bash
.venv/bin/python -m pytest -q                                   # 2026-09-06 晚: 363 passed, 1 skipped, 25.3s（连跑 5 次一致；曾 flaky，已修，见 L11）
.venv/bin/python -m pytest --collect-only -q | tail -1           # 2026-09-06 晚: 364 tests collected（稳定）
export PATH=/usr/local/node/bin:$PATH && cd web && npm test      # 2026-09-06 晚: 56 passed, 1.1s（稳定）
```

> **2026-09-06 晚间又涨了一次**（上表是当天下午那轮修正后的值，已过期）：可观测层改造
> 加了 OTLP 导出 + span 父子树 + `/metrics` + 召回明细落轨迹，`test_observability` 14→**23**、
> `test_server` 76→**83**、前端 `render.test.ts` 25→**26**，总数 348→**364**。
> ARCHITECTURE.md（头部/§3.3/§15/§15 表/§18.5）与 README.md（§Layout/§Testing）里的数字
> 已同步；下面这张漂移表**保留当天下午的原样**，因为它是那次核实的记录，不是当前基线——
> 当前基线以上面三条命令的输出为准。

分文件计数（改测试后同步 ARCHITECTURE §15 那张表用）：

```bash
.venv/bin/python -m pytest --collect-only -q 2>/dev/null | grep -oE '^tests/[a-z_]+\.py' | sort | uniq -c
```

2026-09-06 晚间的结果：`test_memory` 125、`test_server` 83、`test_sandbox_pool` 41、
`test_security` 31、`test_observability` 23、`test_planning` 16、`test_model_capabilities` 14、
`test_deployment` 12、`test_launch` 7、`test_rebuild_milvus` 5、`test_mysql_compat` 4、
`test_smoke` 2、`test_compaction` 1 —— 合计 **364**，与 ARCHITECTURE §15 那张表逐行相加一致。

离线套件不需要网络/数据库/Redis/Docker/API key：`tests/conftest.py` 在 import `pi` **之前**
就把 `PI_REDIS_URL`/`PI_SANDBOX`/`PI_POLICY` 清空、`PI_TRACER=noop`、`PI_WEB_DIST` 指向不存在
的路径，所以仓库根的生产 `.env` 漏不进测试。

### L8 · git：从零 commit 的中间态到首个提交 ✅ 已解决（但并发写入这条没变）

> **状态更新（2026-09-06 21:10）**：首个提交 `6eb79d7` 已落地，分支 `main`，
> remote `https://github.com/15838328874/pi.git`（接在用户自己的 GitHub `Initial commit`
> `3fd5eba` 之后，非 force-push）。仓库级身份取自用户 GitHub 提交：
> `15838328874 <135090639+15838328874@users.noreply.github.com>`，**全局 git 配置未改动**。
> 提交前扫过索引：133 个文件，无 `.env`/`.venv`/`node_modules`/`__pycache__`/`dist`/`*.db`/
> 审计日志，无真实云实例域名或公网 IP，无硬编码凭据。
>
> **下面那段"零 commit 中间态"的分析保留**，因为它描述的是一个真实存在过、而且很容易骗人的状态；
> 但**现在 `git diff`/`checkout`/`stash` 都能用了**。
>
> ⚠️ **没有随之解决的是并发写入**：仍然可能有多个会话同时改这个目录（本节末尾那条纪律不变）。
> git 给你的是**回滚能力**，不是**互斥**。动手前照样核对 mtime。

⚠️ **状态在 2026-09-06 17:03:50 变了**：`/root/pi/pi-python/.git` 已经被创建（`git init`，
分支 `master`）。但截至 17:12，**一个 commit 都没有**，`git status` 显示所有文件都是 `??`
（untracked）。这个中间态最容易骗人：

- `git diff` 现在**有输出了，但没意义** —— 没有基线可比，改了什么看不出来；
- `git checkout -- <file>` / `git stash` **依然还原不了任何东西**，因为没有任何已提交版本可回；
- `git log` 直接报 `fatal: your current branch 'master' does not have any commits yet`。

**所以在第一个 commit 落地之前，"改动只能手工还原"这条实操结论完全没变** →
动手前照样先 `cp` 备份，做变异测试尤其如此。别因为看见 `.git` 就以为有了安全网。

- 现有备份：`/root/pi/pi-python-20260903.zip`、`/root/pi-python-backup-20260903.tar.gz`
  （都是 9-03 的，已落后于当前代码）。
- **强烈建议立刻做一次基线 commit**（这是 `HANDOFF.platform.md` §8 第 2 条一直在提的事）。
  有了它，上面三条才真正失效，多会话并行才有冲突检测。注意 `.gitignore` 已忽略 `.env`/`.env.test`，
  commit 前用 `git status` 确认它们没被带进去。
- `HANDOFF.platform.md` §3.1 的并发警告**已经应验**：2026-09-06 17:03–17:12 期间，
  另一个会话在同一仓库里 `git init` 并改动了 `policy.json`、`tests/test_security.py`、
  `deploy/cloud-deploy.md`、`README.md`、`ARCHITECTURE.md`，**以及本文档**（把 §2 结论里
  "`web_fetch`/`web_search` 被 `deny_tools` 禁用"那句改成了"已被删除，联网改走 builtin tools"，
  改得对）。零 commit + 多会话同时写 = 无法检测也无法合并冲突，这是当前最大的单点风险。

### L9 · Milvus namespace：`pi`（生产）与 `it`（测试）

collection 名是 `{PI_MILVUS_NS}_memories`，即 `pi_memories` 与 `it_memories`，同集群同 token。
`tools/rebuild_milvus.py:42` 定义 `PROD_NS = "pi"`，并在 `:119` 对它要求 `--yes` 才肯
drop-and-rebuild；**测试 NS `it` 没有这道守卫**，直接跑就会重建。这是有意的（测试环境重建无害），
但要清楚：在测试环境里这个脚本是"无确认即执行"的。

MySQL 才是记忆的真源（`user_memories` 表存了 packed float32 向量），
`rebuild_milvus.py` 可以零 API 调用重建索引 —— 所以误重建 `it_memories` 可恢复，
误重建 `pi_memories` 同样可恢复，但要花时间和 embedding 配额。

### L10 · README 的 API 清单漏了 9 个路由（三个完整特性域）

README §Multi-user server 的 "API (see `/docs` for OpenAPI)" 那行列了 11 个路由，
实测 app 上注册的是 **29 个**（去掉 `/docs` `/redoc` `/openapi.json` `/healthz` `/readyz`
`/docs/oauth2-redirect` 这 6 个基础设施路由，业务路由 23 个）。**漏掉的 9 个**：

| 路由 | 属于 | 说明 |
|---|---|---|
| `DELETE /v1/me` | 账号注销 | `app.py:977`，级联删七张表 + token 即死（`test_server.py::TestDeregister`） |
| `GET /v1/memories` | **长期记忆** | 列出当前用户的事实 |
| `DELETE /v1/memories` | 长期记忆 | 清空 |
| `DELETE /v1/memories/{fact_id}` | 长期记忆 | 删单条 |
| `GET /v1/admin/traces` | **执行轨迹** | 管理端查 run 轨迹（`agent_runs`/`agent_steps`） |
| `GET /v1/admin/traces/{run_id}` | 执行轨迹 | 单个 run 的步骤序列 |
| `POST /v1/sessions/{id}/files` | **文件附件** | 上传，见 **L3** |
| `GET /v1/sessions/{id}/files` | 文件附件 | 列表 |
| `GET /files/{session_id}/{name}` | 文件附件 | **无认证**下载，注意它在 `/v1` **之外** |

也就是说 **长期记忆、执行轨迹、文件附件三个特性域，加上账号注销，在 README 的 API 清单里
完全不存在**。`web/openapi.json` 是唯一完整的清单（它是从 app dump 出来的，见
`tools/dump_openapi.py`），所以查 API 请以它或 `/docs` 为准，**不要信 README 那一行**。

重新生成完整清单：

```bash
.venv/bin/python -c "
import os
os.environ.setdefault('PI_DATABASE_URL','sqlite+aiosqlite:////tmp/_probe.db')
os.environ.update(PI_TRACER='noop', PI_SANDBOX='', PI_POLICY='', PI_REDIS_URL='')
from pi.server.app import create_app
for r in sorted(create_app().routes, key=lambda r: getattr(r,'path','')):
    for v in sorted(getattr(r,'methods',{}) - {'HEAD','OPTIONS'}):
        print(f'{v:<7} {r.path}')
"; rm -f /tmp/_probe.db
```

`GET /files/...` 落在 `/v1` 之外这件事本身值得注意：它不受 `/v1` 那套认证依赖覆盖，
而前端挂载在 `/`（最后注册，不会遮蔽它）。改路由前缀时别顺手把它挪进 `/v1` ——
网关要无 token 抓取，挪进去就得同时改认证逻辑。

### L11 · 离线套件曾是 flaky 的：`TestDeregister` 与后台记忆抽取赛跑 ✅ 测试已修 / ⚠️ 产品竞态仍开着

> **状态更新（2026-09-06 晚）**：下面描述的抖动**已经修掉**——只改测试，
> 见本节末「对后来人的三条实际影响」第 3 点。保留以下全部原始分析，因为
> **第 2 点那个生产级竞态并没有被这次修复关掉**，而且这段分析本身是
> "如何从一条间歇性红灯倒推出真实语义缺口"的完整样本。

**实测（2026-09-06）：10 次全套件运行里 6 次红、4 次绿 —— 约 60% 失败率。**
（其中一段连续 5 次是 3 绿 2 红；此后又连续 4 次全红。看不出规律，就是时序掷骰子。）
红的时候永远是同一个用例、同一条断言：

```
tests/test_server.py::TestDeregister::test_the_cascade_wipes_every_trace_and_the_token
（断言在 tests/test_server.py:1048）
```

而 **`pytest tests/test_server.py` 单独跑 3/3 全绿（76 passed）**，
`pytest tests/test_server.py::TestDeregister` 单跑也绿。所以它只在**全套件上下文**里犯病，
是典型的时序敏感。

这条 flaky **与同期另一个会话的改动无关**：对方在 17:03–17:12 改了 `policy.json`、
`tests/test_security.py`、`README.md`、`ARCHITECTURE.md`、`deploy/cloud-deploy.md`
（删除本地 web 工具、改走 builtin tools），改完全套件仍然红在同一条上，
且 `tests/test_security.py` 单独跑 31 passed、`--collect-only` 仍是 348 —— 计数没变。

#### 机制（已从代码核实，不是猜测）

该用例的流程是：跑 2 次真 run → `_wait_audit_flushed(at_least=2)` → `before = _counts(...)`
快照 → `DELETE /v1/me` 注销 → 断言 `purged == {**before, "account": 1}`。

问题在于**每轮 run 结束都会触发一次后台事实抽取**，而抽取**无论成功失败都会记一条
`turns=0` 的 `usage_records`**：

- `src/pi/memory/service.py:732` —— `turns=0  # marks this row as memory overhead, not a conversation run`
- `src/pi/memory/extract.py:126 parse_facts()` —— "Never raises: bad output means no facts"，
  解析失败只 `log.warning` 然后返回 `[]`，**记账照走**

而 `_wait_audit_flushed()` **只同步 `audit_events` 一张表**，完全没有等抽取任务收尾。于是：

> 抽取的 `usage_records` 行如果落在 `_counts()` 快照**之后**、注销清库**之前**，
> 清库就会比快照多数掉一行 → `purged["usage_records"] == before["usage_records"] + 1` → 断言炸。

失败现场的证据完全对得上：

- 期望值（`before`）里 `usage_records: 3` —— 2 次 run 的 2 行 + **1 条抢在快照前落地的抽取行**；
  实际 `purged` 是 4（第 2 条抽取行落在快照后）。
- `memories` 两边都是 **2**（= `_seed` 种下的 2 条 alice 事实），**没有多出记忆行** ——
  因为捕获日志里有两条
  `WARNING pi.memory.extract:extract.py:131 fact extraction returned unparseable JSON (123 chars)`，
  抽取解析失败返回 `[]`，所以只留下记账行、没留下事实行。这两条 warning 就是
  "抽取确实在这个用例期间跑过"的直接物证。

为什么全套件才犯病：套件整体把事件循环和线程池压得更忙，抽取任务落地的时刻被推后，
更容易越过快照那一线。单独跑时它在快照前就完事了。

#### 对后来人的三条实际影响

1. ~~**不要把 `347 passed` 当成"必须全绿"的验收门**~~ —— **这条已作废**：flaky 已修
   （见下面第 3 点），现在 **`363 passed, 1 skipped` 就是验收门**，看到任何一条红都该当成
   真问题查，不要再先怀疑"是不是那条老 flaky"。修完后实测：单独跑 30/30 全过，
   全套件连跑 5 次结果完全一致。
2. **它是真 bug 的信号，不只是测试瑕疵**。生产语义上：用户点注销的瞬间如果有一次抽取正在飞，
   清库可能漏掉那条 `usage_records`（注销返回的 `purged` 回执也会少报一行）。
   对"可审计的数据删除"这种承诺来说，这是实打实的缺口 —— 回执说删了 N 行，实际删了 N+1，
   或者反过来漏删。**这条与 #54（Run 地基 / append-only 事件日志）的"可回溯"目标直接相关**，
   建议一并考虑：注销应当与在飞的抽取任务互斥，或等它们收尾。
3. **修法很便宜，而且已经修了**（2026-09-06 晚，只改测试、没碰产品代码）：
   新增 `TestDeregister._wait_usage_settled()`，在 `_counts()` 快照之前把在飞的抽取等干净。
   走的是上面说的"前者是正解"那条路，但实现上**不是**调 `MemoryService.drain()` ——
   memory service 是 `create_app()` 里的闭包局部变量，`app.state` 上只有 `settings` 和 `db`，
   测试够不着它。所以照同类里已有的 `_wait_audit_flushed` 同一模式**轮询数据库**，
   等 `usage_records` 计数**连续 5 次不变**（静默）而不是等某个固定行数：固定值会把这个用例
   耦合到 fake provider 恰好产出几条抽取上，等于把注销契约的测试变成了对夹具的断言。

   > 本文档原先写"只记录不动手"，理由是 `tests/test_server.py` 与 `src/pi/memory/` 正被
   > 并发会话改、且本仓库没有 git。动手时的实际前提是：那个会话已静默 2 小时以上、
   > 全套件已回到全绿、并且仓库已经 `git init` 并有了首个提交可回退。**这三条不满足时
   > 仍然应该只记录不动手。**

   ⚠️ **再说一遍：修的是测试的确定性，不是第 2 点那个生产竞态。** 第 2 点仍然开着。

---

## 5. 测试环境账号现状（2026-09-06 实测）

`pi_py_test.users`，7 行。密码哈希均为 `pbkdf2$200000$<32 hex salt>$<64 hex digest>`（111 字符），
盐各自独立（7 个账号 7 个不同哈希）。

| id | username | admin | active | quota | 创建时间 | sessions | 密码 |
|---|---|---|---|---|---|---|---|
| 8 | `admin` | ✅ | ✅ | 1,000,000 | 2026-09-03T06:56:10Z | 4 | `pi-test-123`（播种默认，见 **L5**） |
| 9 | `alice` | — | ✅ | 1,000,000 | 同上 | 7 | 同上 |
| 10 | `bob` | — | ✅ | 1,000,000 | 同上 | 2 | 同上 |
| 11 | `carol` | — | ✅ | 1,000,000 | 同上 | 2 | 同上 |
| 12 | `overquota` | — | ✅ | **1,000** | 同上 | 2 | 同上（配额夹具 → 402） |
| 13 | `disabled` | — | **❌** | 1,000,000 | 同上 | 0 | 同上（停用夹具 → 401） |
| 29 | `smokeweb` | — | ✅ | 1,000,000 | 2026-09-05T09:23:14Z | 1 | **未知，不可恢复**（见 **L6**） |

前 6 个由 `tools/seed_testdb.py` 播种（创建时间完全相同），`overquota`/`disabled` 是故意的
故障夹具，别"修好"它们。

生产 `pi_py.users` = **0 行**，没有任何账号。

登录验证（2026-09-06 实测通过）：

```bash
curl -s -X POST http://127.0.0.1:8398/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"pi-test-123"}'
# → HTTP 200 {"access_token":"…","token_type":"bearer","expires_in":43200,
#             "username":"admin","is_admin":true}
```

浏览器入口 `http://<host>:8398/`（`web/dist` 已构建于 2026-09-05 19:32，且没有比它更新的前端
源码，即 dist 是最新的）。注意 **不是** README 快速开始里写的 8300。

---

## 6. 基线快照（2026-09-06 实测）

| 项 | 值 |
|---|---|
| 服务进程 | PID 809332，`.venv/bin/pi-py serve --host 0.0.0.0 --port 8398`，2026-09-05 19:46 启动，cwd `/root/pi/pi-python` |
| `/healthz` | `{"status":"ok"}` |
| `/readyz` | `{"status":"ready","checks":{"db":"ok","cache":"ok","memory":"ok"}}` |
| Python 套件 | 364 collected → **363 passed, 1 skipped**，25.3s，74 warnings。~~⚠️ flaky~~ **✅ 已修**：曾 10 次里 6 次红在同一条 `TestDeregister`，修后单独跑 30/30、全套件连跑 5 次一致，见 L11 |
| 前端套件 | **56 passed**（3 files），1.1s，连跑稳定；`npm run typecheck`（`vue-tsc --noEmit`）亦通过 |
| venv | `.venv/bin/python` = Python 3.12.3；`aiomysql`/`pymysql`/`asyncpg`/`sqlalchemy` 均可用 |
| alembic head | `0007_trace_fidelity`（`migrations/versions/` 共 0001–0007） |
| Docker 残留 | 只有一个 `hello-world` 容器（3 天前 Exited 0），**无沙箱池孤儿容器** |
| 审计 | `audit-test-2026-09-06.jsonl` 5 条；无 `audit-2026-09-06.jsonl`（生产今天没跑） |
| env 文件权限 | `.env` 600、`.env.test` 600 |

66 条 warning 的实际构成（**不是**单一来源，别照着"全是 JWT key 太短"去理解）：

| 条数 | 类型 | 性质 |
|---|---|---|
| 64 | `InsecureKeyLengthWarning`（`jwt/api_jwt.py:147` 编码 33 + `:368` 解码 31） | **测试夹具问题**：用例里的 HMAC key 只有 15 字节，低于 RFC 7518 建议的 32。与生产无关 —— 实测生产 `PI_JWT_SECRET` 是 **64 字符 hex（32 字节熵）**，满足建议值 |
| 1 | `StarletteDeprecationWarning`（`fastapi/testclient.py:1`） | **依赖层的将来风险**：`Using httpx with starlette.testclient is deprecated; install httpx2 instead`。整个离线套件的 HTTP 驱动都走 `TestClient`，所以这条哪天变成硬错误会一次性打红 76 个 `test_server.py` 用例。升级 fastapi/starlette 前先确认这条 |
| 1 | `PytestUnraisableExceptionWarning`（表面挂在 `test_launch.py::TestLogout::test_logout_revokes_token`） | **良性，但值得知道**：`BaseSubprocessTransport.__del__` 在事件循环关闭后触发 `RuntimeError: Event loop is closed`。该用例本身不起子进程 —— 是别处（`LocalRunner` 的 `create_subprocess_exec`）留下的 transport 在这个用例期间被 GC 掉了。根因是 asyncio 的 teardown 顺序 + 本项目"每个用例 `asyncio.run()` 一个新循环、不用 pytest-asyncio"的约定：transport 没有确定性关闭，靠 finalizer 兜底。用例是 passed 的，不影响结果；但它说明**沙箱本地路径的子进程 transport 生命周期不是确定性收尾的**，与 README §Security note 提到的冷 CLI 路径"timeout 只杀本地 docker run 客户端"是同一类问题 |

要复现这份分类：

```bash
.venv/bin/python -m pytest -q 2>&1 | sed -n '/warnings summary/,/^-- Docs:/p'
```

---

## 7. 待办归属

本文只**记录**问题，不自动修。以下属于用户自己的运维范畴（`HANDOFF.md` §1 明确标注未授权）：

- **L4** 给 `.env.test` 补独立 `PI_JWT_SECRET` —— 一行配置，收益最大，建议优先。
- **L5** 换 `admin` 密码 / 确认 ECS 安全组（`cloud-deploy.md` §1 第 5 项至今 "☐ 待你确认"）。
- **L1** 生产上线前必须先 `pi-py migrate` 到 `0007`，**不要**靠 `create_all`。
- **#42**（密钥轮换）、**#43**（云侧暴露面）本就是用户的活。

以下属于代码/文档侧，可以直接做：

- **L2** 统一 8300/8398 的默认值口径（代码全是 8300，只有两行文档提 8398；注意
  `app.py:888` 的文档字符串已进 OpenAPI 契约，改它要重跑 codegen）。
- **L3** 在 `.env` 里补一行注释说明"生产故意不设 `PI_PUBLIC_BASE_URL`"，或者补上生产值；
  给 README/ARCHITECTURE 补文件附件特性的一节；若要收紧 capability URL，
  session id 加长到 `hex[:16]` + 给 `/files/` 加 IP 限流（都不改契约）。
- **L6** 清掉 `smokeweb` 这类无主残留账号。
- **L7** 已修正；后续改测试数时记得 README 与 ARCHITECTURE 两处都要动。
- **L10** 已把 README 的 API 清单补全（11 → 23 条业务路由）。以后加路由时**同步改那张表**，
  或者干脆把它换成"完整清单见 `/docs` 与 `web/openapi.json`"以免再次漂移。
- **L11** flaky 测试**已修**（2026-09-06 晚，只改 `tests/test_server.py`，没碰 `src/pi/memory/`）。
  当初记录而不动手的三个理由现在都不成立了：并发会话已静默、全套件回到全绿、
  而且仓库已经 `git init` 并有了首个提交（L8 那个"无法安全回滚"也随之解决）。
  实际修法就是本节建议的"正解"——在 `_counts()` 快照前等在飞的抽取收尾，
  照 `_wait_audit_flushed` 的样子写了个 `_wait_usage_settled()`，等**静默**而非等固定行数；
  **没有**用放宽断言的方式把红灯关掉。
  ⚠️ **但注销语义那个真缺口仍然开着**：测试现在会等抽取收尾，产品代码里注销和在飞抽取
  之间**依然没有互斥**。这一条与 #54（Run 地基 / 可回溯）同源，建议合并考虑。
