# pi-py

Python implementation of the [pi coding agent](https://github.com/earendil-works/pi) harness, shipped as a **multi-user HTTP service**: streaming LLM layer, asyncio agent loop, coding tools, JWT auth, per-user quotas, rate limiting, full audit trail, and optional Docker sandboxing. Mirrors pi's package layering:

| pi (TypeScript)   | pi-py (Python)                                        |
| ----------------- | ----------------------------------------------------- |
| `pi-ai`           | `pi.llm` (openai / anthropic / fake)                  |
| `pi-agent-core`   | `pi.agent` (asyncio loop + events)                    |
| `pi-coding-agent` | `pi.tools` + `pi.prompt`                              |
| daemon / server   | `pi.server` (FastAPI + SSE), `pi.cli` (serve/migrate) |

> **New to this codebase? Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first.**
> It is the onboarding handbook: full layer-by-layer architecture, every module and
> function explained with design rationale, quick-start, configuration reference,
> deployment notes, and the pitfalls the previous maintainer stepped on.

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
```

Notes:

- `PI_DATABASE_URL` is **required**. MySQL (`mysql+aiomysql://`) or PostgreSQL
  (`postgresql+asyncpg://`); the server raises at startup without it. There is no
  local-file fallback — SQLite survives only as a test dependency.
- Any OpenAI-compatible endpoint works via `OPENAI_BASE_URL` (Aliyun MaaS verified:
  `token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`).
- `<think>...</think>` reasoning spans are stripped from the stream automatically (qwen/deepseek style).
- No API key? Set `PI_MODEL=fake/demo` — the scripted provider keeps every HTTP path
  exercisable end to end with canned replies.
- Never commit `.env` (git-ignored). Rotate `PI_JWT_SECRET` deliberately: changing it
  invalidates every issued token.

## Run

```bash
pi-py migrate                                  # alembic upgrade head against PI_DATABASE_URL
pi-py serve --host 0.0.0.0 --port 8300
```

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

Or open `http://localhost:8300/` in a browser — the API process serves the built frontend from
`web/dist` (see "Web UI" below). Without a build there, that URL is a plain 404.

## Tools

Eight built-ins, registered in `tools/__init__.py::all_tools()`:

`bash` (timeout + exit code), `read` (line numbers, paging), `write`, `edit` (exact unique
match, replace_all, unified diff), `grep` (regex, skips VCS/build dirs), `find` (glob), `ls`,
`submit_plan` (record a structured plan; ends the turn).

**There is no local internet tool, by design.** The former `web_fetch` / `web_search` were
deleted rather than fixed: a local fetcher runs *inside the app process*, so `PI_SANDBOX=docker`
and `--network none` never constrained it, and with no address validation plus
`follow_redirects=True` it was an open SSRF path to the cloud metadata service. Web access is
the model endpoint's **own** builtin tools instead — pass `builtin_tools: ["web_search",
"web_extractor", "code_interpreter"]` in the `POST /runs` body and the provider fetches
server-side. `tests/test_security.py::test_the_local_tool_surface_has_no_internet_tool` pins
this: no registered tool module may import an HTTP client.

Context compaction is automatic once history exceeds 80,000 chars: an LLM-written summary
replaces the old prefix and the most recent 8 messages are kept verbatim. Compaction applies
**in memory for the current run only** — `RunManager` does not wire `AgentLoop`'s `on_compact`
hook and `MessageRepo` can only append, so the database keeps the full raw history and each
subsequent turn reloads and re-compacts it.

## Task planning (Phase 1)

For a task that will touch 2+ files, need 3+ tool calls, or involve a refactor or migration,
the system prompt tells the model to call `submit_plan` **first and alone** with a structured
plan (bounded in `pi.models.Plan`: title ≤200 chars, 1–20 steps of ≤300 chars each). It is a
**terminal** tool:

- A successful submission **ends the turn immediately**. Calls later in the same batch never
  execute, but each still gets a `toolcall_end` frame (`ok: false`, "did not execute") and a
  synthesized error `tool_result` — OpenAI/Anthropic reject a history whose tool calls are not
  all answered, and they reject it on the *next* request, which is the nasty part. Every skip
  is audited (`allowed: false`, reason `skipped: submit_plan ended the turn`).
- A rejected submission (oversized, malformed) is **not** terminal: the error goes back to the
  model and the turn continues, so a bad plan does not leave the user with neither plan nor
  answer.
- The plan is persisted on the session (`sessions.plan`, last write wins — the submit_plan
  arguments in the transcript are the immutable history) and returned by both
  `GET /v1/sessions` and `GET /v1/sessions/{id}`. On the wire it is one SSE `plan` frame,
  after the last `toolcall_end` and before `turn_end`.

Phase 1 is **record-only by design** — nothing is blocked or gated; the approval gate is
Phase 2. To disable planning entirely, add `submit_plan` to `deny_tools`: a policy-denied
call is just an error result, and per the rules above the turn simply continues.

## Enterprise security

`PI_POLICY` points at a JSON gate applied to **every** tool call. The `policy.json` in the repo
root is the **live** policy for both deployment modes (bare metal points `PI_POLICY` straight at
it; both compose files bind-mount the same file to `/etc/pi-py/policy.json`). Shape:

```json
{
  "deny_tools": [],
  "deny_command_patterns": [
    "(?:^|[;&|(]\\s*)sudo\\b",
    "\\brm\\s+(-{1,2}[a-z-]+\\s+)*/(\\s|$|\\*)"
  ],
  "path_sandbox": true,
  "redact": true
}
```

- **deny_tools** — tool blocklist, rejected before execution. **Empty in the shipped policy**:
  every registered tool is meant to be available, and the one genuinely dangerous surface
  (local internet fetching) was removed from the codebase instead of being blocklisted. Use it
  to narrow the agent's reach — `["bash", "write", "edit"]` gives you a read-only agent that can
  inspect a workspace but never mutate it or run a command (denying `bash` alone is *not*
  read-only: `write` and `edit` still change files).
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
| Streaming | SSE (`text/event-stream`) over **POST**, so `EventSource` cannot be used: start / text_delta / toolcall_start / toolcall_end / compaction / plan / turn_end / error / done. `error` arrives inside an HTTP 200 stream — a client that only checks `response.ok` silently swallows failed runs |
| API contract | every route declares a Pydantic response model, so FastAPI validates outgoing bodies and `/openapi.json` is complete enough to generate client types from. Errors are uniformly `{"detail": "..."}` (422 is FastAPI's own `HTTPValidationError`, 503 is `ReadyOut`). The SSE payload models are merged into `components.schemas` by hand — a `StreamingResponse` gives FastAPI nothing to discover — and `TestSseEventPayloads` pins them to what `event_to_sse()` actually emits |
| Web UI | `web/dist` is served by this same process, mounted at `/` last so it cannot shadow the API. No CORS middleware exists, and same-origin is the point: see "Web UI" below |
| Storage | SQLAlchemy 2.0 async over MySQL (aiomysql) or PostgreSQL (asyncpg); `PI_DATABASE_URL` required, schema managed by Alembic |
| Workspaces | per-user sandbox dir `PI_WORKSPACE_ROOT/<user>/` |
| Observability | JSON access log with request id + latency; `/healthz`, `/readyz`; full audit trail; per-run execution traces in MySQL (`agent_runs`/`agent_steps`, admin UI); spans exportable over OTLP; `/metrics` for Prometheus |

API — **23 business routes** (the first seven rows below), plus the infrastructure routes in the
last row; 30 registered in total. `web/openapi.json` (dumped from the app by
`tools/dump_openapi.py`) and `/docs` are authoritative **for everything they contain** — but note
`GET /metrics` is registered with `include_in_schema=False` and therefore appears in neither, so
they are no longer a *complete* route list. Re-derive the full set with the snippet in
`deploy/environments.md` §L10. The grouping here is a map, not a contract.

| Area | Routes |
|---|---|
| Auth | `POST /v1/auth/register`, `POST /v1/auth/login`, `POST /v1/auth/logout` (blacklists the jti) |
| Account | `GET /v1/me`, `DELETE /v1/me` (deregister: cascades over seven tables, kills the token) |
| Sessions | `GET/POST /v1/sessions`, `GET /v1/sessions/{id}`, `GET /v1/sessions/{id}/messages`, `POST /v1/sessions/{id}/runs` (SSE, the only rate-limited route) |
| Attachments | `POST /v1/sessions/{id}/files` (upload), `GET /v1/sessions/{id}/files` (list), `GET /files/{session_id}/{name}` (download — **outside `/v1`, deliberately unauthenticated**: the model gateway must fetch it with no token, so the session id is the capability. Disabled unless `PI_PUBLIC_BASE_URL` is set) |
| Memory | `GET /v1/memories`, `DELETE /v1/memories`, `DELETE /v1/memories/{fact_id}` |
| Usage | `GET /v1/usage` (monthly per-model token + cost breakdown) |
| Admin | `GET /v1/admin/users`, `PATCH /v1/admin/users/{u}`, `POST /v1/admin/users/{u}/revoke` (bumps the per-user epoch), `GET /v1/admin/audit`, `GET /v1/admin/traces`, `GET /v1/admin/traces/{run_id}` |
| Infra | `GET /healthz`, `GET /readyz`, `GET /metrics` (Prometheus; not in the OpenAPI document), `GET /openapi.json`, `GET /docs`, `GET /redoc`, `GET /` (serves `web/dist` when built) |

`web/openapi.json` is that contract, committed on purpose so a change to it shows up in a
diff and codegen does not need a running server. Regenerate it after touching a route:

```bash
python tools/dump_openapi.py   # boots the app against a throwaway SQLite, never your .env
```

## Web UI

A minimal two-screen client (login / chat) in `web/`: Vue 3 + TypeScript + Vite + Pinia +
Naive UI. No router — `App.vue` switches on `auth.signedIn`.

```bash
cd web && npm ci
npm run dev        # vite on :5173, proxies /v1 to :8300 (see vite.config.ts)
npm run build      # vue-tsc --noEmit && vitest run && vite build -> web/dist
```

The API process serves `web/dist` itself, mounted at `/` **after** every route so it cannot
shadow `/v1/*`, `/healthz`, `/openapi.json` or `/docs`. That is because there is no CORS
middleware: adding one would only mean the browser's origin and the API's origin differ for no
benefit. So after `npm run build`, `pi-py serve` gives you the UI at `http://host:8300/` with no
proxy changes. A missing `web/dist` is not an error — it just means no UI here, which is normal
for a backend-only or test deployment. Override the location with `PI_WEB_DIST`.

`vite.config.ts` sets `build.sourcemap: true`, so `dist/assets/*.js.map` (1.4 MB, full original
TypeScript including comments) is served to anyone who can reach the port. There are no secrets
in it and the bundle ships to the browser either way — sourcemaps only make it readable instead
of minified — but it is a deliberate trade: browser devtools stay usable against a production
bundle, which is worth a lot for a UI that has not been exercised in a real browser. Set it to
`false` if you would rather not publish the source.

Contract workflow: `src/pi/server/app.py` is the only source of truth. Edit a route or a
response model, then `python tools/dump_openapi.py` and `npm run gen:api`, which regenerates
`src/api/schema.d.ts`. Types are not hand-written, so a backend rename fails the frontend
typecheck instead of failing at runtime.

Things worth knowing before editing the client:

- **Token storage is `sessionStorage`**, not `localStorage` (`src/api/client.ts`). Per-tab, and
  gone when the tab closes. `auth.restore()` revalidates it against `GET /v1/me` on boot and
  clears it on a 401; any other failure leaves the token in place so a network blip does not log
  you out.
- **Enter sends, Shift+Enter breaks the line, and `e.isComposing` must be checked.** Confirming a
  Chinese IME candidate with Enter arrives as `keydown` with `key === "Enter"`, so without that
  guard picking a candidate sends a half-typed message.
- **There is no SSE message-boundary event.** Within a turn the loop streams text, then tool
  calls, then runs each tool — so a `text_delta` arriving *after* a `toolcall_end` means a new
  assistant message began. That heuristic lives in `stores/chat.ts::painter` and is pinned by
  `tests/transcript.test.ts`.
- **`toolcall_start` fires once per streamed argument chunk**, so the same id arrives several
  times. Key on the id; do not treat each frame as a new call.
- **Tool results on the wire are previews** — `AgentLoop` cuts them to 200 characters and
  flattens newlines before emitting. After each turn the client reloads the persisted transcript,
  which carries full arguments and untruncated results, and that replaces the live paint.
- The agent loop appends tool results as a `role: "user"` message, so a result arrives one
  message *after* the call it answers; `toUi` folds them back onto their calls and drops the
  then-empty user messages.
- No client-side form validation on purpose. The bounds live in `RegisterIn` and reach the
  browser as a 422 that `errorMessage()` already renders; duplicating the numbers would give
  them a second place to drift.

## Observability, metering, resilience

| Concern | Implementation |
|---|---|
| Tracing | `PI_TRACER=jsonl` (default, built-in span file) or `otel` (exports over **OTLP/gRPC** to Jaeger / Tempo / an OTel Collector; install `pi-py[observability]`). Spans form a **tree** — `agent.run` → `llm.call` (per turn) / `tool.call` / `memory.retrieve` → `memory.embed` / `memory.recall` / `memory.join` / `memory.rerank` — one `trace_id` per run, with `parent_span_id` in the jsonl file. Endpoint via `PI_OTLP_ENDPOINT` (falls through to the standard `OTEL_EXPORTER_OTLP_*` vars, then `localhost:4317`), sampling via `PI_TRACE_SAMPLE_RATE`. A missing exporter falls back to jsonl **with a warning**, as does an unrecognised `PI_TRACER` value — both used to be silent |
| Execution traces | every `POST /runs` writes one `agent_runs` row plus one `agent_steps` row per step: `tool_call` (full arguments and full result), `retrieval` (the memory verdict — outcome, which recall path ran, every candidate's scores and why it was dropped, the thresholds in force, per-stage milliseconds, and the text actually injected), `llm_call` (one per round-trip, including the one that raised), `plan`, `compaction`, `error`. Queryable at `GET /v1/admin/traces[/{run_id}]` and in the admin UI's 执行轨迹 tab; `agent_runs.request_id` joins a trace to its access-log line. Retention `PI_TRACE_RETENTION_DAYS` (30) |
| Metrics | `GET /metrics` in Prometheus text format: runs by status/model, in-flight gauge, run duration and turn counts, tokens, per-model LLM calls, per-tool calls, memory retrieval outcomes and index fallbacks, trace-write failures. Labels are bounded by design — never a username, session or prompt. `PI_METRICS=0` (or a missing `prometheus-client`) answers 503 with the reason; `PI_METRICS_TOKEN` gates it with a bearer token and answers **404**, not 403, on a wrong one. Compose ships Jaeger + Prometheus under `--profile observability`, both UI ports bound to loopback |
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
| Tool sandbox | `PI_SANDBOX=docker` runs bash commands in containers: workspace bind-mounted at `/ws`, **network disabled** by default (`PI_SANDBOX_NET=host` to allow), image via `PI_SANDBOX_IMAGE` (default `python:3.12-slim`); fail-closed if docker is unreachable. Any other value **fails at startup** — it used to fall through to running commands inside the app process, where they inherit `PI_JWT_SECRET` and `PI_DATABASE_URL` |
| Warm container pool | docker mode defaults to a per-workspace warm pool: containers are preheated at turn start (concurrent with the first LLM response), commands run via `docker exec` (no per-call container lifecycle), idle entries are recycled (`PI_SANDBOX_IDLE_TTL`, default 600s), LRU-evicted at capacity (`PI_SANDBOX_POOL_MAX`, default 16) with soft overshoot when all are busy, and vanished containers are rebuilt transparently. `PI_SANDBOX_POOL=0` restores the legacy fresh-container-per-call behaviour. Warm containers self-terminate after `PI_SANDBOX_WARM_LIFETIME` (default 2h) so a crashed app cannot orphan them forever |
| Container resource limits | `PI_SANDBOX_MEMORY` (1g) / `PI_SANDBOX_PIDS` (256) / `PI_SANDBOX_CPUS` (1.0). Docker's own defaults are **no limit at all** (`Memory=0`, `NanoCpus=0`, no `PidsLimit`), and registration is open, so uncapped containers let any account exhaust the host with one command — `--network none` does not cover resource exhaustion. Applied at **all four** container-creation paths (cold CLI, cold Engine API, warm CLI, warm API); `--memory-swap` is set equal to `--memory` because docker otherwise allows 2x via swap. Keep `PI_SANDBOX_MEMORY × PI_MAX_CONCURRENT_RUNS` well under physical RAM (shipped: 1g × 8 = 8 GiB of 16 GiB). `PI_SANDBOX_USER` defaults to the **app's own uid:gid** so files written through the bind mount stay readable and deletable by it — `0:0` bare metal, `10001:10001` under compose, no config change needed |
| Fail-loud config validation | Two settings whose wrong value silently removed isolation now refuse to start or self-correct: an unrecognised `PI_SANDBOX` raises in `create_app` (and the legitimate local path logs a warning spelling out the consequence), and `server_policy()` forces `path_sandbox`/`redact` on so a `PI_POLICY` file can add rules but never subtract them |
| Container image | multi-stage `Dockerfile` (non-root uid 10001, HEALTHCHECK `/healthz`, migrations baked into `/opt/pi-py`, observability deps installed at runtime so `PI_TRACER=otel` and `/metrics` work in the container and not only on a host that installed the extra); `docker-compose.yml` brings up app + postgres + redis with health-gated startup, and Jaeger + Prometheus under `--profile observability` |
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
  tools/                    base + bash/read/write/edit/grep/find/ls/plan + sandbox
  security/                 policy.py + audit.py + redact.py
  observability/            tracing.py + metrics.py + metering.py + prices.py
  memory/                   long-term memory: store (Milvus) + repo (MySQL) + embed +
                            rerank + extract + arbitrate + service
  server/                   config, db, auth, cache, ratelimit, runner, app (FastAPI)
  cli.py                    argparse entrypoints: serve / migrate
migrations/                 Alembic versions 0001-0007 (head: 0007_trace_fidelity)
tests/                      pytest suite: 365 tests, no network/DB/model needed
tools/dump_openapi.py       regenerate web/openapi.json from a throwaway app instance
web/                        Vue 3 + TS frontend (see "Web UI" below)
  openapi.json              committed contract, dumped from the app
  src/api/                  client.ts (fetch + ApiError), sse.ts (frame parser), endpoints.ts,
                            schema.d.ts + types.ts (generated from openapi.json)
  src/stores/               auth.ts, chat.ts (Pinia setup stores)
  src/views/                LoginView.vue, ChatView.vue
  tests/                    sse/transcript/render tests + fixtures captured from a live server
  tests/live/               opt-in integration suite, needs a running server
tools/loadtest.py           SSE load test
tools/seed_testdb.py        seed a reusable *_test database (idempotent, refuses production)
tools/sandbox_bench.py      docker warm-pool capacity sweep (concurrent users -> exec latency)
tools/rebuild_milvus.py     drop + recreate the vector index from MySQL (zero API calls)
deploy/                     Caddyfiles, cloud runbook, .env template, environments.md
```

## Testing

```bash
pip install -e ".[dev]"
python -m pytest -q          # 364 passed, 1 skipped
```

> ✅ **The suite is no longer flaky.** It used to be: ten full-suite runs on 2026-09-06 were
> green four times and `1 failed` six times, always
> `test_server.py::TestDeregister::test_the_cascade_wipes_every_trace_and_the_token`.
> Its `_counts()` snapshot did not wait for the in-flight **background fact extraction** to
> settle, and extraction writes a `turns=0` `usage_records` row whether or not it succeeds
> (`pi/memory/service.py:732`), so a row landing after the snapshot made `purged` exceed
> `before` by one. Fixed by `_wait_usage_settled()`, which mirrors the existing
> `_wait_audit_flushed` and polls for **quiescence** rather than a fixed row count — pinning a
> number would couple the test to however many extractions the fake provider happens to yield.
> Verified 30/30 in isolation (was 4/20 failing) and five consecutive full-suite runs green.
>
> ⚠️ **That fixed the test, not the product.** The underlying race is still open: if an
> extraction is in flight at the moment a user deregisters, the purge can miss that
> `usage_records` row and the receipt misreports by one. For a promise of *auditable erasure*
> that is a real gap, and deregistration should be mutually exclusive with in-flight
> extractions (or wait for them). Tracked with the Run/append-only-event-log work.
> Full analysis in `deploy/environments.md` **L11**.

The suite needs no database, Redis, Docker, or API key: `tests/conftest.py` pins
`PI_REDIS_URL` / `PI_SANDBOX` / `PI_POLICY` / `PI_MILVUS_URI` / `PI_EMBEDDING_MODEL` /
`PI_RERANK_URL` / `PI_MEMORY_MODEL` / `PI_MEMORY_ARBITER_MODEL` / `PI_METRICS_TOKEN` empty,
`PI_TRACER=noop`, `PI_METRICS=1` and `PI_WEB_DIST` at a nonexistent path before `pi` is
imported, so a production `.env` in the repo root cannot leak into a test run — and the
`web/dist` pin means routing does not depend on whether someone happened to run
`npm run build`, which would otherwise add a catch-all mount at `/`.
Tests use `FakeProvider` plus throwaway SQLite files, and async tests are wrapped in
`asyncio.run()` (no pytest-asyncio dependency).

> That pin list is **load-bearing and was incomplete once already**. `PI_METRICS_TOKEN` was
> missing, which was invisible while `.env` happened to carry an *empty* value for it — the
> moment the empty duplicate was deleted (see `deploy/environments.md` **L12**) the real token
> leaked in, `/metrics` started answering 404 to unauthenticated scrapes, and two
> `TestMetricsEndpoint` cases flipped from 200/503 to 404. **When you add a `PI_*` setting to
> `.env`, ask whether the suite's behaviour depends on it, and pin it in `conftest.py` if so.**
> Full write-up: **L14**.

The frontend has its own suite (Node, not Python):

```bash
cd web && npm ci
npm test          # 56 tests: SSE framing, the two transcript reducers, SSR render assertions
npm run typecheck # vue-tsc --noEmit
PI_LIVE_API=http://127.0.0.1:8300 npm run test:live   # 11 opt-in tests, needs a running server
```

`tests/live/*.live.ts` is deliberately outside vitest's default include glob, so `npm test`
never needs a server. `npm run build` chains typecheck + tests + bundle: there is no CI, so the
build is the only gate before a bundle gets served.

### Shared test database

For manual checks against real MySQL + Redis, `.env.test` points everything at a separate
schema and namespace. It is a **twelve-key overlay, not a full config** — `PI_DATABASE_URL` →
`pi_py_test`, `PI_REDIS_NS=test`, `PI_WORKSPACE_ROOT` → `~/.pi-py/workspaces-test`,
`PI_AUDIT_PATH` → `~/.pi-py/audit-test.jsonl`, `PI_MILVUS_NS=it`, `PI_PUBLIC_BASE_URL`,
`PI_METRICS`/`PI_METRICS_TOKEN`, `PI_TRACER=otel`/`PI_OTLP_ENDPOINT`, `PI_ENVIRONMENT=test`,
`PI_SERVICE_NAME`. Every other key (`PI_JWT_SECRET`, all other credentials, model selection,
sandbox and capacity settings) is **inherited from `.env`**, so the two environments share
secrets and are separated only by where data lands — see `deploy/environments.md` §2 and its
landmine **L4** before assuming isolation. (`PI_METRICS_TOKEN` is the one credential the overlay
*does* set independently, and it is the model to copy for L4.)

`pi/__init__.py` only auto-loads `.pi-py.env` / `.env` / `~/.pi-py/.env`, so test overrides need
an explicit source — and the order matters, because existing environment variables always win:

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

> ⚠️ On the shared test instance, **`admin`'s password was rotated off the default on
> 2026-09-06** — `pi-test-123` now returns 401 for it, while the other five fixtures still use
> the default. Re-running `seed_testdb.py` will **not** reset it: the script only calls
> `create()` when `by_username()` returns `None`, and never rewrites an existing
> `password_hash`. The rotated value is deliberately not written down anywhere in this repo
> (it has a public remote). Note also that there is **no password-change API at all** —
> `UserRepo` has `set_active`/`set_quota`/`set_admin` but no `set_password`, and no route
> exposes one, so rotation means a direct `UPDATE users SET password_hash=…` using
> `pi.server.auth.hash_password()`. Changing a password does **not** invalidate issued tokens
> (`current_user()` never compares passwords); call `POST /v1/admin/users/{u}/revoke` for that.
> See `deploy/environments.md` **L5** and **L13**.

Two things bite people here, both written up in **[`deploy/environments.md`](deploy/environments.md)**:

- **"The service is running" does not tell you which environment it is on.** A bare `pi-py serve`
  loads only `.env` and therefore hits **production** `pi_py`. Read `/proc/<pid>/environ` to find
  out what a live process actually connected to (§1 of that doc).
- **Every port default in the code is 8300**, so against a test instance on 8398 you must override
  `PI_API` / `PI_LIVE_API` for `npm run dev`, `npm run test:live` and `tools/loadtest.py` (landmine
  **L2**). `tools/dump_openapi.py` needs no live server at all — prefer it for refreshing
  `web/openapi.json`.

> ⚠️ Before pointing anything at production: `pi_py` is currently **five migrations behind** head
> (at `0002_user_active`) and empty. Starting the server there lets `Database.init()`'s
> `create_all` add the missing *tables* but never the missing `sessions.plan` *column*, after which
> every session read/write fails with `Unknown column 'sessions.plan'` while `/readyz` still reports
> `db: ok`. Run `pi-py migrate` first. Full analysis: landmine **L1**.

## Security note

With `PI_SANDBOX` unset, tools execute with the server process's own privileges **and inherit its
environment** — `PI_JWT_SECRET` and `PI_DATABASE_URL` included. The policy engine is a safety net,
not a sandbox: it enforces path confinement and command blocklists, but it cannot stop a command
from reading the environment (`env`, `printenv`, `/proc/self/environ`, any interpreter one-liner).
Approval gates address mistakes; only a system boundary addresses malice.

The shipped `.env` therefore sets `PI_SANDBOX=docker` (network disabled, per-container memory /
pids / cpu ceilings, running as the app's own uid). The known gap that remains: on the **cold CLI**
path (`PI_SANDBOX_POOL=0`) a timeout kills only the local `docker run` client — a SIGKILL cannot be
forwarded to the container and `--rm` fires on container *exit*, so the container keeps running
until its own command finishes. (Established by reading the code, not reproduced live; the default
warm pool calls `docker rm -f` and the Engine API path POSTs `/kill`, so both do tear it down.)
A second gap — the local `web_fetch` / `web_search` tools running inside the app process, out of
the sandbox's reach, as an open SSRF path — was closed by **deleting them**; see "Tools" above.
For hostile multi-tenant workloads, run tool execution inside microVMs. Never commit `.env`
(git-ignored); rotate `PI_JWT_SECRET` in production.
