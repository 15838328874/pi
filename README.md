# pi-py

A **self-hostable, extensible AI agent platform**: sandboxed tool execution, multi-tenant
governance (JWT/quotas/rate limits/audit), a canonical trajectory log, an eval harness with
scoring and A/B, an RL data flywheel (rollout→reward→SFT/RLVR JSONL), and protocol-level
tool extensibility via MCP + Skills. Coding is the first fully-developed scenario, not the
boundary. Python reimplementation of the [pi coding agent](https://github.com/earendil-works/pi)
harness, shipped as a multi-user HTTP service: streaming LLM layer, asyncio agent loop, coding
tools, and optional Docker sandboxing. Mirrors pi's package layering:

| pi (TypeScript)   | pi-py (Python)                                        |
| ----------------- | ----------------------------------------------------- |
| `pi-ai`           | `pi.llm` (openai / anthropic / fake)                  |
| `pi-agent-core`   | `pi.agent` (asyncio loop + events)                    |
| `pi-coding-agent` | `pi.tools` + `pi.prompt`                              |
| daemon / server   | `pi.server` (FastAPI + SSE), `pi.cli` (serve/migrate) |

> **文档地图**（四个文档各管一段，知识点不重复）：
>
> | 文档 | 定位 | 什么问题看它 |
> |---|---|---|
> | `README.md` | 门面 | 这是什么、怎么装、怎么跑（快速上手入口） |
> | `PROJECT_GUIDE.md` | 叙事与价值 | 为什么这么设计（取舍）、踩过什么坑（故事版）、测试样例与实测数据 |
> | `ARCHITECTURE.md` | 技术手册 | 每个模块每个函数、配置全表（§13）、坑清单（§17）、差距清单（§19） |
> | `ROADMAP.md` | 状态与路线图 | 什么做完了、什么没做、下一步做什么（含环境区分表） |
| `docs/`（三件） | CubeSandbox 专项 | 沙箱设计笔记 / 生产部署手册 / 生产就绪审计——专项文档，不重复核心四文档内容 |


Linux only. The former single-user CLI/TUI mode, the local SQLite session store, and
all Windows/WSL support have been removed — this package is the service and nothing else.

## Install

```bash
cd pi-python
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[production]"   # deployable server: asyncpg + aiomysql + redis + alembic + otel
pip install -e ".[dev]"          # test suite only: pytest + aiosqlite
```

## Configure

Environment variables, or a `.env` file (project root, `./.pi-py.env`, or `~/.pi-py/.env`;
the first file found wins, and existing environment variables always override it):

```bash
PI_DATABASE_URL=mysql+aiomysql://user:pass@host:3306/pi_py   # required
PI_REDIS_URL=redis://user:pass@host:6379/0                   # locks / limits / revocation
PI_REDIS_NS=prod                                             # key namespace, isolates environments
PI_JWT_SECRET=<openssl rand -hex 32>                         # required in production
PI_MODEL=openai/qwen3.8-max
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
# optional vector semantic memory: set ALL FOUR to enable (else lexical retrieval)
PI_EMBEDDING_URL=https://.../api/v1/services/embeddings/text-embedding/text-embedding
PI_EMBEDDING_API_KEY=sk-...
PI_EMBEDDING_MODEL=qwen3.7-text-embedding
PI_MILVUS_URI=http://milvus-host:19530
# optional extra tool sources (empty = off)
PI_MCP_SERVERS='[{"name":"filesystem","command":["npx","-y","@modelcontextprotocol/server-filesystem","/ws"]}]'
PI_SKILLS_DIR=/opt/pi-py/skills
```

Notes:

- `PI_DATABASE_URL` is **required**. MySQL (`mysql+aiomysql://`) or PostgreSQL
  (`postgresql+asyncpg://`); the server raises at startup without it. There is no
  local-file fallback — SQLite survives only as a test dependency.
- Any OpenAI-compatible endpoint works via `OPENAI_BASE_URL` (Aliyun MaaS verified:
  `token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`).
- `<think>...</think>` reasoning spans are stripped from the stream automatically (qwen/deepseek style).
- Vector semantic memory (`remember`/`recall` tools, auto-injection at turn start): the
  four `PI_EMBEDDING_*` / `PI_MILVUS_URI` vars enable Milvus vector search with the
  `memories` table as source of truth; unset (or any missing) = lexical retrieval only.
  Milvus outages degrade to lexical — retrieval quality, never correctness.
- MCP stdio servers are spawned as child processes of the app and inherit its environment:
  treat `PI_MCP_SERVERS` as admin-level config. MCP/skill tools pass the same policy gate,
  and path-like args on unknown tools are workspace-confined by the generic path sandbox.
- No API key? Set `PI_MODEL=fake/demo` — the scripted provider keeps every HTTP path
  exercisable end to end with canned replies.
- Never commit `.env` (git-ignored). Rotate `PI_JWT_SECRET` deliberately: changing it
  invalidates every issued token.

## Run

```bash
pi-py migrate                                  # alembic upgrade head against PI_DATABASE_URL
pi-py serve --host 0.0.0.0 --port 8300
```

**本地测试环境**（MySQL + Redis + Milvus 全套 Docker 化 + 真实模型，与生产严格区分）：
见 [`deploy/local-dev.md`](deploy/local-dev.md)，基础设施编排在 `deploy/docker-compose.local.yml`，
环境变量模板在 `deploy/env.local.example`（生产是云端托管 MySQL/Redis/Milvus + `docker-compose.cloud.yml`）。

Registration is open: every signup is a normal user. Admin is granted only by writing the
database directly — there is no route for it:

```sql
UPDATE users SET is_admin=1 WHERE username='alice';
```

Because signup is unauthenticated and unlimited, keep the port off the public internet
(security group / firewall). Exposing it means anyone can create accounts, each carrying a
monthly token quota and a workspace directory.

```bash
curl -X POST :8300/v1/auth/register -d '{"username":"alice","password":"..."}'
TOKEN=$(curl -s -X POST :8300/v1/auth/login -d '{"username":"alice","password":"..."}' | jq -r .access_token)
SID=$(curl -s -X POST :8300/v1/sessions -H "Authorization: Bearer $TOKEN" -d '{"title":"t"}' | jq -r .id)
curl -N -X POST :8300/v1/sessions/$SID/runs -H "Authorization: Bearer $TOKEN" -d '{"prompt":"hi"}'
```

## Tools

Twelve built-ins, registered in `tools/__init__.py::all_tools()`:

`bash` (timeout + exit code), `read` (line numbers, paging), `write`, `edit` (exact unique
match, replace_all, unified diff), `grep` (regex, skips VCS/build dirs), `find` (glob), `ls`,
`web_fetch`, `web_search`, `remember`, `recall`, `spawn_subagents`.

Two extra tool sources, merged by `ToolRegistry` (builtin first; name collisions keep the
builtin; one broken source never takes the others down):

- **MCP** (`PI_MCP_SERVERS`, JSON array of `{"name","command":[...]}` stdio or `{"name","url"}`
  HTTP servers, via the official `mcp` SDK): each server's tools become regular tools — they
  pass the same policy gate, path sandbox, audit and tracing. A dead server surfaces as
  per-call tool errors (v1: no auto-reconnect).
- **Skills** (`PI_SKILLS_DIR`, a directory of `<skill>/SKILL.md` packages, Anthropic Agent
  Skills style): a compact index is injected into the system prompt, `use_skill` loads a
  skill's full instructions on demand, and each `scripts/*` file becomes a
  `skill_<name>_<script>` tool executed inside the sandbox (the script is staged into the
  workspace first, since the sandbox only mounts the workspace).

Context compaction is automatic once history exceeds 80,000 chars: an LLM-written summary
replaces the old prefix and the most recent 8 messages are kept verbatim. Compaction applies
**in memory for the current run only** — `RunManager` does not wire `AgentLoop`'s `on_compact`
hook and `MessageRepo` can only append, so the database keeps the full raw history and each
subsequent turn reloads and re-compacts it.

## Enterprise security

`PI_POLICY` points at a JSON gate applied to **every** tool call. The `policy.json` in the repo
root is the **live** policy for both deployment modes (bare metal points `PI_POLICY` straight at
it; both compose files bind-mount the same file to `/etc/pi-py/policy.json`). Shape:

```json
{
  "deny_tools": ["web_fetch", "web_search"],
  "deny_command_patterns": [
    "(?:^|[;&|(]\\s*)sudo\\b",
    "\\brm\\s+(-{1,2}[a-z-]+\\s+)*/(\\s|$|\\*)"
  ],
  "path_sandbox": true,
  "redact": true
}
```

- **deny_tools** — tool blocklist, rejected before execution
- **deny_command_patterns** — bash command regex blocklist (case-insensitive, `re.search`)
- **path_sandbox** — file tools confined to the session's workspace subtree (`../../` cannot escape)
- **redact** — outbound masking (API keys, Aliyun/AWS/GitHub/Slack tokens, CN mobile numbers,
  ID numbers, private IPs); only the copy sent to the LLM is redacted, stored history stays intact

Patterns are anchored to command position (`(?:^|[;&|(]\s*)`) on purpose: a bare `"sudo"`
also blocks `grep -rn 'sudo' src/`, and **a false positive is harder to diagnose than a miss**
because it silently breaks ordinary agent work. `tests/test_security.py::TestShippedPolicy` pins
both directions against the shipped file — 22 commands that must be denied, 15 that must stay
allowed — so add cases to those lists before touching a regex.

A policy file can only **add** rules: `server_policy()` forces `path_sandbox` and `redact` on even
if the file omits them or sets them to `false` (`Policy.from_dict` defaults both to `False`, so a
deny-list-only file would otherwise switch off the workspace sandbox and redaction). Without any
policy file the server still runs with `path_sandbox + redact` on. Every tool call
(allowed **and** denied) is appended to the audit log `~/.pi-py/audit.jsonl`, rotated daily as
`audit-YYYY-MM-DD.jsonl` (JSONL: timestamp, user, session, tool, args, decision, outcome).

## Multi-user server

| Concern | Implementation |
|---|---|
| Auth | JWT (HS256, PyJWT), PBKDF2 password hashing (200k iters) offloaded via `asyncio.to_thread` so concurrent logins cannot freeze the event loop; open unauthenticated signup, every account is a normal user — admin is granted only by `UPDATE users SET is_admin=1 ...` |
| Session isolation | sessions/messages scoped per user; cross-user access returns 404 (no existence leak) |
| Concurrency | per-session lock (1 running turn per session) + global semaphore (`PI_MAX_CONCURRENT_RUNS`, default 8) + run timeout (`PI_RUN_TIMEOUT_SECONDS`, default 600) |
| Rate limiting | fixed-window per user (`PI_RATE_LIMIT_RUNS_PER_MIN`, default 20), 429 + Retry-After |
| Audit | append-only daily-rotated JSONL: tool calls, policy decisions, compaction, and **every register/login attempt** with client IP + user agent (never the password). All fields truncated, because the failed-login path is attacker-controlled and `LoginIn.username` is unbounded. Now that it holds IPs it is personal data — give it a retention period |
| Reverse proxy | `PI_FORWARDED_ALLOW_IPS` (default `127.0.0.1`) lists whose `X-Forwarded-For` to trust; both compose files set it to `172.16.0.0/12` so the Caddy container qualifies. A wrong value **fails silently** — every client is logged as the proxy, and any IP-keyed limit collapses into one bucket. Never `*`: uvicorn then returns the leftmost, client-supplied entry |
| Streaming | SSE (`text/event-stream`): start / text_delta / toolcall_start / toolcall_end / compaction / turn_end / error / done |
| Storage | SQLAlchemy 2.0 async over MySQL (aiomysql) or PostgreSQL (asyncpg); `PI_DATABASE_URL` required, schema managed by Alembic. Raw run trajectories append to `PI_TRAJECTORY_PATH` (plus the `runs` table for structured lookup). Session workspaces archive at turn end as tar.gz + diff metadata (`PI_ARCHIVE_DIR`, optional MinIO via `PI_ARCHIVE_S3_*`, `PI_ARCHIVE=0` off) (default `~/.pi-py/trajectories.jsonl`, daily rotation; `""` = off) — persistence failures are logged, never allowed to fail a run |
| Workspaces | per-user sandbox dir `PI_WORKSPACE_ROOT/<user>/` |
| Observability | JSON access log with request id + latency; `/healthz`, `/readyz`; full audit trail |

API (see `/docs` for OpenAPI): `POST /v1/auth/register|login|logout`, `GET /v1/me`,
`GET/POST /v1/sessions`, `GET /v1/sessions/{id}`, `DELETE /v1/sessions/{id}`,
`GET /v1/sessions/{id}/messages`,
`GET /v1/sessions/{id}/trajectory` (latest raw run, ownership-checked),
`GET /v1/trajectory/{run_id}` (replay one run) and `GET /v1/admin/trajectory/{run_id}`
(admin cross-user replay),
`POST /v1/sessions/{id}/runs` (SSE), `GET /v1/usage`, `GET /v1/admin/users`,
`PATCH /v1/admin/users/{u}`, `POST /v1/admin/users/{u}/revoke`, `GET /v1/admin/audit`
(`?day=YYYY-MM-DD&user=&tool=&event=`, DB-backed with jsonl fallback),
`GET /v1/admin/stats` (today's aggregates + fleet sizes), `GET /v1/admin/usage` (monthly per-user).
Official async SDK: `from pi.client import PiClient` (auth/sessions/messages/trajectories/usage + SSE run stream, `trust_env=False`). Zero-build web UI (single-file vanilla JS pages): user app at `GET /ui/app.html`
(login/sessions/chat with full tool-call trace + monthly usage), the trajectory
viewer at `GET /ui/trajectory.html?session=<id>`, and the admin console at
`GET /ui/admin.html` (overview/users+quota/audit).

## Observability, metering, resilience

| Concern | Implementation |
|---|---|
| Tracing | `PI_TRACER=jsonl` (default, built-in span file) or `otel` (OpenTelemetry bridge, install `pi-py[observability]`); spans: `agent.run` / `llm.call` / `tool.call` with duration + status |
| Prometheus metrics | `GET /metrics` (text format 0.0.4, `prometheus-client` via `pi-py[observability]`): runs/llm/tool/memory/HTTP RED counters + histograms + `pi_runs_in_flight` gauge; degradation counters for lexical memory fallback and model-chain fallbacks, plus sandbox health series (`pi_sandbox_create_failures_total` / `pi_sandbox_command_timeouts_total` / `pi_sandbox_close_failures_total` / `pi_sandbox_create_duration_seconds`). Every family projects from one existing recording point (tracer span close / trajectory / RunManager / middleware) - labels stay bounded (model/tool/status/route, never user or session). Gate with `PI_METRICS_TOKEN` (wrong token -> 404); `PI_METRICS=0` disables. Scrape config: `deploy/prometheus.yml` |
| Cost accounting | every completed run records tokens + est. cost (per-model price table, `PI_PRICES_FILE` override); `GET /v1/usage` returns monthly per-model breakdown |
| Quotas | per-user monthly token quota (`PI_DEFAULT_QUOTA_TOKENS`, default 1M); exhausted -> HTTP 402 |
| Model fallback | `PI_FALLBACK_CHAIN="openai/qwen3.8-max,openai/qwen3.8-flash,openai/qwen3.6-flash"`; transient errors (connection/timeout/429/5xx) retry with exponential backoff (2x, 0.5s base), then degrade; mid-stream failures never replay; non-transient errors propagate |
| Resilience | metering/audit/callback failures are logged, never allowed to fail a run |

## Deployment hardening: Redis, sandbox, Docker

| Concern | Implementation |
|---|---|
| Distributed rate limit + session locks | `PI_REDIS_URL=redis://...` switches both to Redis (INCR+EXPIRE window / SET NX EX lock); without it they degrade to process-local, which is only correct for a single instance |
| Token revocation | `POST /v1/auth/logout` blacklists the token's jti for its remaining life; `POST /v1/admin/users/{u}/revoke` bumps a per-user epoch invalidating all their tokens (1s conservative window); disabled accounts are rejected at login and on every request |
| MySQL | `PI_DATABASE_URL=mysql+aiomysql://user:pass@host:3306/pi_py` (install `pi-py[mysql]`) — the engine adds `charset=utf8mb4` + connection recycling automatically |
| PostgreSQL | `PI_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/pi` (install `pi-py[postgres]`) — same schema and Alembic path |
| Tool sandbox | `PI_SANDBOX=docker` runs bash commands in containers (warm pool, `--network none`, cgroup limits). `PI_SANDBOX=cubesandbox` runs in **CubeSandbox microVMs** via the E2B-compatible SDK (`PI_CUBE_API_KEY`, host KVM/nested virtualization required) — session workspace lives inside the VM, commands wrapped in GNU `timeout` (124 → `timed_out`, zero residue), non-zero exit codes pass through, >10MB workspace load rejected with an actionable error. Any other value **fails at startup** — it used to fall through to running commands inside the app process, where they inherit `PI_JWT_SECRET` and `PI_DATABASE_URL`. Design/runbook: `docs/cube-sandbox-design-notes.md` / `docs/production-deployment.md` |
| Warm container pool | docker mode defaults to a per-workspace warm pool: containers are preheated at turn start (concurrent with the first LLM response), commands run via `docker exec` (no per-call container lifecycle), idle entries are recycled (`PI_SANDBOX_IDLE_TTL`, default 600s), LRU-evicted at capacity (`PI_SANDBOX_POOL_MAX`, default 16) with soft overshoot when all are busy, and vanished containers are rebuilt transparently. `PI_SANDBOX_POOL=0` restores the legacy fresh-container-per-call behaviour. Warm containers self-terminate after `PI_SANDBOX_WARM_LIFETIME` (default 2h) so a crashed app cannot orphan them forever |
| Container resource limits | `PI_SANDBOX_MEMORY` (1g) / `PI_SANDBOX_PIDS` (256) / `PI_SANDBOX_CPUS` (1.0). Docker's own defaults are **no limit at all** (`Memory=0`, `NanoCpus=0`, no `PidsLimit`), and registration is open, so uncapped containers let any account exhaust the host with one command — `--network none` does not cover resource exhaustion. Applied at **all four** container-creation paths (cold CLI, cold Engine API, warm CLI, warm API); `--memory-swap` is set equal to `--memory` because docker otherwise allows 2x via swap. Keep `PI_SANDBOX_MEMORY × PI_MAX_CONCURRENT_RUNS` well under physical RAM (shipped: 1g × 8 = 8 GiB of 16 GiB). `PI_SANDBOX_USER` defaults to the **app's own uid:gid** so files written through the bind mount stay readable and deletable by it — `0:0` bare metal, `10001:10001` under compose, no config change needed |
| Fail-loud config validation | Two settings whose wrong value silently removed isolation now refuse to start or self-correct: an unrecognised `PI_SANDBOX` raises in `create_app` (and the legitimate local path logs a warning spelling out the consequence), and `server_policy()` forces `path_sandbox`/`redact` on so a `PI_POLICY` file can add rules but never subtract them |
| Container image | multi-stage `Dockerfile` (non-root uid 10001, HEALTHCHECK `/healthz`, migrations baked into `/opt/pi-py`); `docker-compose.local.yml` (MySQL+Redis+Milvus 基础设施) 与 `docker-compose.cloud.yml`（生产 app+caddy，DB 云端托管） |
| Load tested *(historical, inherited)* | `tools/loadtest.py`: previous maintainer's run — 50 users x 2 rounds on self-hosted PG+Redis, 100/100 success, p95 ~2.4s, p99 ~3.1s (includes PBKDF2 registration); same-session concurrency correctly serialized by the lock. **Not re-measured on the managed MySQL+Redis deployment.** The tool prints percentiles to stdout and writes no result file |
| Sandbox capacity *(measured on this deployment)* | `tools/sandbox_bench.py`, 4 vCPU / 16 GiB ECS, `PI_SANDBOX=docker` warm pool, `python:3.12-slim`, network off. Throughput saturates at **~52 exec/s by N=4 concurrent workspaces** and stays flat to N=64 with host CPU at 96-100%, so p50 latency is pure queue depth: 44ms @ N=1, 154 @ 8, 292 @ 16, 567 @ 32, 1162 @ 64 — **zero failures at every level**. A heavier command (`python -c`) drops the ceiling to ~42/s. Memory is not the constraint (~26 MiB per warm user); cold-starting 64 containers at once takes 3.0s. The Engine API transport is only 1.3x faster per call than `docker exec`, so the ceiling is dockerd/runc, not the CLI transport. Note `PI_SANDBOX_POOL_MAX=16` does **not** cap a simultaneous arrival storm — it logs "allowing temporary overshoot" and creates all 64. In the shipped config `PI_MAX_CONCURRENT_RUNS=8` caps concurrent turns first, so the sandbox runs at ~154ms p50 with 6x headroom. **Measured before the per-container ceilings existed** — the host-CPU-saturation figures in particular predate `PI_SANDBOX_CPUS=1.0`, so re-run the bench if you tighten the caps |
| Multi-instance verified *(historical, inherited)* | two instances sharing one Redis/DB: cross-instance session lock confirmed (Redis lock key observed with expected TTL), compose `replicas` knob added |
| TLS | `docker compose --profile tls up -d` fronts the app with Caddy (automatic HTTPS, SSE-friendly); see `deploy/Caddyfile` |

```bash
# full stack
docker compose up -d
docker compose exec app pi-py migrate

# or manual: you run redis + the database yourself
export PI_DATABASE_URL="postgresql+asyncpg://pi:pass@localhost:5432/pi"
export PI_REDIS_URL="redis://localhost:6379/0"
export PI_SANDBOX=docker
pi-py migrate && pi-py serve --host 0.0.0.0 --port 8300
```

Production extras in `pyproject.toml`: `pip install pi-py[production]`
(asyncpg + aiomysql + redis + alembic + opentelemetry).

## Cloud deployment (Volcano Engine)

App on a 4 vCPU / 16 GiB ECS, MySQL 8.0.43 + Redis 7.0.15 as managed instances reached over
the VPC private network (no DB containers on the host):

```bash
cp deploy/env.cloud.example .env   # fill in MYSQL_PASSWORD / REDIS_PASSWORD / PI_JWT_SECRET / model keys
docker compose -f docker-compose.cloud.yml up -d --build
```

- `docker-compose.cloud.yml`: one-shot `migrate` service (alembic, idempotent) runs before
  `app` starts; optional Caddy TLS front behind the `tls` profile (`deploy/Caddyfile.cloud`,
  domain via `DOMAIN` env).
- The image bakes `alembic.ini` + `migrations/` into `/opt/pi-py` (`PI_ALEMBIC_DIR`), so
  `pi-py migrate` works without the repo checkout; the runtime layer ships `aiomysql`
  alongside `asyncpg`.
- MySQL runs with the least-privilege `pi` account (DML+DDL on `pi_py.*` only); sizing for
  4 vCPU (`PI_MAX_CONCURRENT_RUNS=16`, sandbox caveats) and the full runbook:
  **`deploy/cloud-deploy.md`**.
- Managed Redis commonly disables `KEYS` — inspect it with `SCAN` (`redis-cli --scan --pattern 'prod:*'`).

## Layout

```
src/pi/
  models.py                 block-based message model (pydantic)
  prompt.py                 system prompt
  llm/                      base, registry, openai, anthropic, fake, fallback, think_filter
  agent/                    loop.py + events.py + compaction.py
  tools/                    base + bash/read/write/edit/grep/find/ls/web + sandbox
  security/                 policy.py + audit.py + redact.py
  observability/            tracing.py + metering.py + prices.py
  server/                   config, db, auth, cache, ratelimit, runner, app (FastAPI)
  cli.py                    argparse entrypoints: serve / migrate
migrations/                 Alembic versions (0001 schema, 0002 user_active)
tests/                      pytest suite: 132 tests, no network/DB/model needed
tools/loadtest.py           SSE load test
tools/seed_testdb.py        seed a reusable *_test database (idempotent, refuses production)
tools/sandbox_bench.py      docker warm-pool capacity sweep (concurrent users -> exec latency)
deploy/                     Caddyfiles, cloud runbook, .env template
```

## Testing

```bash
pip install -e ".[dev]"
python -m pytest -q          # 254 passed
```

The suite needs no database, Redis, Docker, or API key: `tests/conftest.py` pins
`PI_REDIS_URL` / `PI_SANDBOX` / `PI_POLICY` empty and `PI_TRACER=noop` before `pi` is
imported, so a production `.env` in the repo root cannot leak into a test run. Tests use
`FakeProvider` plus throwaway SQLite files, and async tests are wrapped in `asyncio.run()`
(no pytest-asyncio dependency).

### Shared test database

For manual checks against real MySQL + Redis, `.env.test` points everything at a separate
schema and namespace (`PI_DATABASE_URL` → `pi_py_test`, `PI_REDIS_NS=test`,
`PI_WORKSPACE_ROOT` → `~/.pi-py/workspaces-test`, `PI_AUDIT_PATH` → `~/.pi-py/audit-test.jsonl`).
`pi/__init__.py` only auto-loads `.env`, so test overrides need an explicit source — and the
order matters, because existing environment variables always win:

```bash
set -a; . ./.env; . ./.env.test; set +a
pi-py migrate                    # creates/upgrades pi_py_test only
python tools/seed_testdb.py      # idempotent; --reset wipes first, --users N sets the pool
pi-py serve --port 8398          # leave 8300 for production
```

Seeded fixtures (password from `--password`, default `pi-test-123`): `admin` (promoted via
`UserRepo.set_admin`, the only path to admin), `alice`/`bob`/`carol` normal users with one
populated and one empty session plus a usage record, `overquota` (`quota_tokens=1000` against
1540 tokens used → 402), and `disabled` (`is_active=0` → 401). The script refuses to run unless
`PI_DATABASE_URL` names a `*_test` schema, so forgetting the source cannot damage real data.
Note that usage is a monthly window and the seeded records are stamped today.

## Security note

With `PI_SANDBOX` unset, tools execute with the server process's own privileges **and inherit its
environment** — `PI_JWT_SECRET` and `PI_DATABASE_URL` included. The policy engine is a safety net,
not a sandbox: it enforces path confinement and command blocklists, but it cannot stop a command
from reading the environment (`env`, `printenv`, `/proc/self/environ`, any interpreter one-liner).
Approval gates address mistakes; only a system boundary addresses malice.

The shipped `.env` therefore sets `PI_SANDBOX=docker` (network disabled, per-container memory /
pids / cpu ceilings, running as the app's own uid). The two known gaps that remain: `web_fetch` /
`web_search` run **inside the app process**, not in the sandbox, and do no URL validation — they are
disabled by `deny_tools` in `policy.json` until that is fixed; and on the **cold CLI** path
(`PI_SANDBOX_POOL=0`) a timeout kills only the local `docker run` client — a SIGKILL cannot be
forwarded to the container and `--rm` fires on container *exit*, so the container keeps running
until its own command finishes. (Established by reading the code, not reproduced live; the default
warm pool calls `docker rm -f` and the Engine API path POSTs `/kill`, so both do tear it down.)
For hostile multi-tenant workloads, run tool execution inside microVMs. Never commit `.env`
(git-ignored); rotate `PI_JWT_SECRET` in production.
