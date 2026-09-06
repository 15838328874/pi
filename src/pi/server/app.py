"""pi-py multi-user server: FastAPI app with JWT auth, session isolation, SSE runs.

Boot: PI_JWT_SECRET (auto-generated for dev), PI_DATABASE_URL (required;
mysql+aiomysql:// or postgresql+asyncpg://), PI_MODEL, PI_POLICY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Union
from urllib.parse import quote

import jwt as pyjwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from pi.llm import DEFAULT_MODEL
from pi.memory import MemoryService, get_embedder, get_reranker, get_store
from pi.models import Block, FileBlock, Message, Plan, Role, Usage
from pi.observability.metering import UsageTracker
from pi.observability.metrics import Metrics
from pi.observability.tracing import get_tracer
from pi.server.auth import create_token, decode_token, hash_password, verify_password
from pi.server.cache import CacheBackend, get_backend
from pi.server.config import ServerSettings
from pi.server.db import (
    AgentRunRepo,
    AuditEventRepo,
    Database,
    MessageRepo,
    SessionRepo,
    SessionRow,
    UserMemoryRepo,
    UserRepo,
    memory_dirty_users,
    purge_user,
)
from pi.server.ratelimit import RateLimiter
from pi.server.runner import (
    TRACE_ONLY_EVENTS,
    RunManager,
    event_to_sse,
    request_id_var,
    server_policy,
)
from pi.security.audit import AuditLogger
from pi.tools.sandbox import shutdown_docker_pool, validate_sandbox_mode

log = logging.getLogger("pi.server")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")

_FILENAME_RE = re.compile(r"^[^/\\\x00]{1,200}$")


def _clean_file_name(raw: str) -> str:
    """Basename + charset gate for upload/download file names.

    The download route serves from disk using a name taken straight out of the
    URL, so this is the path-traversal defense: separators stripped by the
    basename, traversal and NUL rejected, length bounded. Raises 400.
    """
    name = Path(raw.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or not _FILENAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid file name")
    return name


def _client_ip(request: Request) -> str:
    """Real client IP, but only if PI_FORWARDED_ALLOW_IPS covers the reverse proxy.

    uvicorn rewrites scope["client"] from X-Forwarded-For; if the proxy's own
    address is not trusted it leaves it alone and this returns the proxy's IP.
    """
    return request.client.host if request.client else "unknown"


class RegisterIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str


class DeregisterIn(BaseModel):
    """Password confirmation for DELETE /v1/me.

    The bearer token already proves session ownership, but erasure is irreversible
    and tokens can be left on a shared machine - a stolen token must not be enough
    to erase an account.
    """

    password: str = Field(min_length=1, max_length=128)


class SessionIn(BaseModel):
    title: str = "session"
    model: str | None = None


class UserUpdateIn(BaseModel):
    quota_tokens: int | None = None
    is_active: bool | None = None


class RunIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_000)
    model: str | None = None
    enable_search: bool = Field(
        default=False,
        description="Endpoint-side web search woven into the answer (extra_body.enable_search).",
    )
    builtin_tools: list[Literal["web_search", "web_extractor", "code_interpreter"]] = Field(
        default_factory=list,
        max_length=3,
        description="Gateway-executed tools to offer the model this run.",
    )
    files: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Names of files uploaded to this session to attach to the prompt.",
    )


# --- Response models -----------------------------------------------------
# The OpenAPI document is the single source of truth for the front/back
# contract: web/ runs openapi-typescript over it rather than hand-writing types.
# Declaring them also makes FastAPI validate every outgoing body, so a route
# that starts returning an unexpected shape fails in the test suite instead of
# in somebody's browser.

class ErrorOut(BaseModel):
    """FastAPI's HTTPException body; every non-2xx below uses this shape."""

    detail: str


class HealthOut(BaseModel):
    status: Literal["ok"]


class ReadyOut(BaseModel):
    status: Literal["ready", "not-ready"]
    checks: dict[str, str] = Field(
        description="Per dependency: 'ok', 'unreachable', or 'error: <detail>'."
    )


class RegisterOut(BaseModel):
    id: int
    username: str
    is_admin: bool = Field(description="Always false. Admin is granted by editing the DB.")


class LoginOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    expires_in: int = Field(description="Seconds. PI_TOKEN_TTL_MINUTES * 60.")
    username: str = Field(
        description=(
            "The stored account name, not an echo of the request. MySQL's default "
            "collation matches case-insensitively, so the login form's spelling and "
            "the account's spelling can differ."
        )
    )
    is_admin: bool = Field(
        description="So the client can show/hide the admin console right after login."
    )


class LogoutOut(BaseModel):
    revoked: bool
    username: str


class MeOut(BaseModel):
    username: str
    is_admin: bool = Field(
        description="So the client can show/hide the admin console after token restore."
    )


class DeregisterOut(BaseModel):
    """DELETE /v1/me. `purged` says what actually went away, per table."""

    username: str
    deleted: Literal[True]
    purged: dict[str, int] = Field(
        description=(
            "Row counts removed per table (memories, messages, sessions, "
            "usage_records, account). The workspace directory is deleted even when "
            "it held no database rows."
        )
    )


class SessionSummary(BaseModel):
    """Body of GET /v1/sessions/{id} and one element of GET /v1/sessions."""

    id: str
    title: str
    model: str
    created_at: str = Field(description="UTC ISO-8601, second precision (stored as a string).")
    plan: Plan | None = Field(
        default=None,
        description=(
            "The session's current plan, or null until submit_plan has run. Both the "
            "list and the detail endpoint return it, so a reload shows the same plan "
            "the live stream painted. A newer plan replaces an older one; the "
            "immutable history is the submit_plan call in the transcript."
        ),
    )


class SessionListOut(BaseModel):
    sessions: list[SessionSummary] = Field(description="Newest first, capped at 50.")


class SessionCreatedOut(BaseModel):
    """POST /v1/sessions. cwd is echoed so the client can show where bash runs."""

    id: str
    title: str
    model: str
    cwd: str


class MessageOut(BaseModel):
    idx: int = Field(description="0-based position of the message within the session.")
    role: Role
    blocks: list[Block]


class MessageListOut(BaseModel):
    messages: list[MessageOut] = Field(description="Ordered by idx, oldest first.")


class FileOut(BaseModel):
    """A file uploaded to a session. `url` is public (no auth) and is what the
    model gateway fetches when the file is attached to a run."""

    name: str
    size: int
    url: str


class FileListOut(BaseModel):
    files: list[FileOut] = Field(description="Sorted by name.")


class AdminUserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    is_active: bool
    quota_tokens: int
    created_at: str


class AdminUserListOut(BaseModel):
    users: list[AdminUserOut] = Field(description="Ordered by id, capped at 200.")


class UserUpdateOut(BaseModel):
    username: str
    changed: list[Literal["quota_tokens", "is_active"]] = Field(
        description="Which fields this call actually wrote; empty if the body was {}."
    )


class RevokeOut(BaseModel):
    username: str
    revoked: Literal[True]


class AuditOut(BaseModel):
    records: list[dict[str, Any]] = Field(
        description=(
            "Newest first. Deliberately untyped: this reads the audit_events table "
            "whose payload schema has grown over time (event = tool_call | "
            "compaction | auth | memory, all carrying ts), and a strict model would turn "
            "one legacy record into a 500."
        )
    )


class TraceStepOut(BaseModel):
    seq: int = Field(description="order within the run, 0-based")
    kind: str = Field(
        description=(
            "tool_call | retrieval | llm_call | plan | compaction | error. "
            "retrieval is the long-term-memory lookup before the first model call "
            "(at most one per run); llm_call is one per model round-trip, including "
            "the one that raised."
        )
    )
    name: str = Field(
        default="", description="Tool name, 'memory.retrieve', or an llm_call's model."
    )
    ok: bool = True
    args: str = Field(
        default="",
        description=(
            "Full JSON arguments of the tool call (tool_call steps), or the query "
            "retrieval selected facts for (retrieval steps). Empty otherwise."
        ),
    )
    detail: str = Field(
        default="",
        description=(
            "Full result content for tool_call steps. For retrieval, the whole "
            "account as JSON: outcome, which recall path ran, every candidate with "
            "its cosine and rerank scores and why it was dropped, per-stage "
            "milliseconds, and the text actually injected - the only record of it, "
            "since injected memory never enters the transcript. For llm_call, turn, "
            "stop_reason, tokens, duration and error as JSON. The plan title or the "
            "error message for those kinds. Never the SSE preview."
        ),
    )
    duration_ms: float = 0.0
    ts: str = ""


class TraceRunOut(BaseModel):
    run_id: str = Field(description="12-hex id; also the /v1/admin/traces/{run_id} key")
    username: str
    session_id: str
    model: str
    prompt: str = Field(
        default="",
        description="The user's input, verbatim. The list view truncates to 200 chars.",
    )
    request_id: str = Field(default="", description="Matches X-Request-Id and the access-log line.")
    enable_search: bool = Field(default=False, description="Model-native web search was on.")
    builtin_tools: list[str] = Field(
        default_factory=list,
        description="Gateway-executed tools this run offered the model.",
    )
    first_idx: int | None = Field(
        default=None,
        description="First message idx this run persisted; null when nothing landed.",
    )
    last_idx: int | None = None
    status: str = Field(description="ok | error | timeout")
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    failed_tools: int = 0
    duration_ms: float = 0.0
    flags: list[str] = Field(
        default_factory=list,
        description=(
            "Anomaly verdict: status name when the run failed, 'empty' when it "
            "produced no turns, 'tool_storm' at >= 3 failed tool calls, "
            "'memory_failed' when a retrieval stage raised. An empty recall is not "
            "flagged - it is the normal answer for a user with no facts yet. Empty "
            "list = unremarkable run."
        ),
    )
    started_at: str
    ended_at: str = ""
    steps: list[TraceStepOut] | None = Field(
        default=None,
        description="Only /v1/admin/traces/{run_id} fills this; the list view omits it.",
    )
    messages: list[MessageOut] | None = Field(
        default=None,
        description=(
            "Only /v1/admin/traces/{run_id} fills this: the full transcript "
            "slice this run wrote (first_idx..last_idx) - input, assistant "
            "output, tool calls with arguments and results, attachments."
        ),
    )


class TraceListOut(BaseModel):
    runs: list[TraceRunOut]


class UsageModelOut(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    runs: int


class UsageOut(BaseModel):
    month: str = Field(description="'YYYY-MM' in UTC.")
    models: list[UsageModelOut]
    total_input_tokens: int
    total_output_tokens: int
    total_est_cost_usd: float
    quota_tokens: int = Field(
        description="Effective monthly quota: the user's own, or PI_DEFAULT_QUOTA_TOKENS when that is 0."
    )
    used_tokens: int


class MemoryFactOut(BaseModel):
    """One stored long-term fact. Text is redacted before it is ever written."""

    id: int = Field(description="Store-local id, unique within this user's facts.")
    text: str
    kind: str = Field(description="preference | convention | environment | fact.")
    source_session: str = Field(description="Session the fact was extracted from.")
    created_at: str


class MemoryListOut(BaseModel):
    facts: list[MemoryFactOut] = Field(description="Oldest first, capped at 500.")
    status: str = Field(
        description=(
            "'ready', 'disabled' or 'unavailable: <reason>'. 'disabled' with an empty "
            "list is the normal state when PI_MILVUS_URI or PI_EMBEDDING_MODEL is "
            "unset, not an error."
        )
    )


class MemoryDeleteOut(BaseModel):
    deleted: Literal[True] = Field(description="Always true; a fact that is not the caller's is a 404.")


class MemoryClearOut(BaseModel):
    deleted: int = Field(description="How many facts this user had; 0 when memory is off.")


def _err(description: str) -> dict:
    return {"model": ErrorOut, "description": description}


UNAUTHORIZED = {
    401: _err(
        "Missing, malformed, expired or revoked bearer token - or the account "
        "was disabled or deleted since the token was issued."
    )
}
NOT_FOUND = {
    404: _err(
        "No such session. Ownership is part of the lookup, so another user's "
        "session is also 404 and never 403 - the existence of an id is not "
        "leaked across accounts."
    )
}
ADMIN_REQUIRED = {
    **UNAUTHORIZED,
    403: _err(
        "Authenticated but not an admin. is_admin is granted by writing the "
        "users table directly; no endpoint can grant it."
    ),
}


# --- SSE payloads --------------------------------------------------------
# /v1/sessions/{id}/runs answers with text/event-stream, which OpenAPI has no
# way to describe: the payload is chosen by the `event:` line, not by a field in
# the JSON. These models are that contract. They are documentation-only, so
# TestSseEventPayloads pins runner.event_to_sse() to them - without that test
# they would be free to drift away from what the server actually emits.

class SseStartData(BaseModel):
    session: str
    model: str = Field(description="The model this turn actually resolved to.")


class SseTextDeltaData(BaseModel):
    text: str = Field(description="One incremental chunk of assistant text.")


class SseToolCallStartData(BaseModel):
    id: str = Field(
        description=(
            "Tool call id; matches the id in toolcall_end. Emitted once per streamed "
            "argument chunk, so the same id arrives several times - key on the id, do "
            "not treat each frame as a new call."
        )
    )
    name: str


class SseToolCallEndData(BaseModel):
    id: str
    name: str
    ok: bool = Field(
        description=(
            "False when the tool raised, when the policy denied it, or when a "
            "terminal tool earlier in the same batch ended the turn and this call "
            "was skipped without executing - in that last case `result` says so, and "
            "the tool had no side effects."
        )
    )
    result: str = Field(
        description=(
            "Preview only: AgentLoop cuts it to 200 characters and flattens newlines "
            "to spaces before emitting the event. Fetch the full result from "
            "GET /v1/sessions/{id}/messages instead."
        )
    )


class SseCompactionData(BaseModel):
    dropped: int = Field(description="Number of messages replaced by the summary.")
    chars_before: int
    chars_after: int


class SsePlanData(Plan):
    """Inherits Plan instead of re-declaring title/steps, so the bounds live in one place."""


class SseTurnEndData(BaseModel):
    turns: int = Field(description="LLM round-trips this run took, including tool loops.")
    usage: Usage


class SseErrorData(BaseModel):
    message: str = Field(
        description="Emitted inside an HTTP 200 stream: a failed run is an event, not a status code."
    )


class SseDoneData(BaseModel):
    """End-of-stream marker. Always `{}`."""


SSE_DATA_MODELS: dict[str, type[BaseModel]] = {
    "start": SseStartData,
    "text_delta": SseTextDeltaData,
    "toolcall_start": SseToolCallStartData,
    "toolcall_end": SseToolCallEndData,
    "compaction": SseCompactionData,
    "plan": SsePlanData,
    "turn_end": SseTurnEndData,
    "error": SseErrorData,
    "done": SseDoneData,
}

SseData = Union[tuple(SSE_DATA_MODELS.values())]  # type: ignore[arg-type]

# ref_template points straight at components/schemas, so the anyOf below can be
# inlined into the response while the $defs are merged in by custom_openapi().
_SSE_SCHEMA = TypeAdapter(SseData).json_schema(ref_template="#/components/schemas/{model}")

SSE_DOC = """\
Server-Sent Events, over **POST** - so `EventSource` cannot be used. Read
`response.body` with a `ReadableStream` reader, buffer on blank lines, and take
the `event:` / `data:` fields of each frame (a leading `:` is a comment).

One turn emits `start`, then any number of `text_delta`, `toolcall_start`,
`toolcall_end` and `compaction`, then at most one `plan`, then `turn_end`, then
`done`. `error` can arrive at any point **and the HTTP status is still 200**, so a
client that only checks `response.ok` will silently swallow failed runs.

Within a turn the agent loop streams text first, then tool calls, then runs each
tool in order - so a `text_delta` arriving *after* a `toolcall_end` means a new
assistant message has begun. There is no message-boundary event; a client that
rebuilds a transcript has to infer the boundary that way.

`plan` comes after the last `toolcall_end` and before `turn_end`. It is emitted by
`submit_plan`, a terminal tool, so the run ends right after it - at most one per
run. The same plan is persisted and returned as
`GET /v1/sessions/{id}`'s `plan` field, so painting it from the stream and reading
it back after a reload agree.

A run cut short that way still emits exactly one `toolcall_end` for every
`toolcall_start`, including the calls it skipped; those carry `ok: false` and a
`result` saying they never executed. No call is ever left looking like it is still
running.

The `data` payload of each frame is the matching `Sse*Data` schema below; the
event name is *not* repeated inside the JSON.

Forward compatibility: an event type this version of the server does not know
how to serialize is emitted as `event: unknown` with `{}`. Clients must ignore
event names they do not recognise instead of failing on them, so a newer server
can add events without breaking an older browser tab.
"""

#: Redis keys the memory maintenance loop uses, namespaced by PI_REDIS_NS.
_ARBITER_LOCK = "memory:arbiter"
_ARBITER_CHECKPOINT = "memory:arbiter:checkpoint"
_MAINTENANCE_LOCK = "memory:maintenance"


async def _memory_maintenance_sweep(
    memory: MemoryService, cache: CacheBackend, settings: ServerSettings
) -> dict[str, int]:
    """One maintenance tick: index healing, pending-sync retry, decay.

    The Redis lock is the multi-replica guard: the work here is idempotent (a
    second pass finds nothing to do), so the lock saves cost rather than guarding
    correctness - unlike the arbiter's checkpoint, no cross-tick state is kept.
    """
    if not await cache.acquire_lock(
        _MAINTENANCE_LOCK, ttl_seconds=float(settings.memory_arbiter_interval_seconds)
    ):
        return {}
    try:
        # Collection healing first: a Milvus that was down at boot (degraded mode)
        # or an operator-recreated collection fixes itself here instead of needing
        # a service restart. Also clears the index breaker when it answers.
        await memory.ensure_index()
        synced = await memory.sync_pending()
        decayed = await memory.decay(settings.memory_decay_days)
        return {"synced": synced, "decayed": decayed}
    finally:
        await cache.release_lock(_MAINTENANCE_LOCK)


async def _memory_maintenance_loop(
    memory: MemoryService,
    cache: CacheBackend,
    engine: AsyncEngine,
    settings: ServerSettings,
) -> None:
    """Sleep-first periodic maintenance. One failure never kills the loop.

    Runs whenever memory is enabled - including degraded mode, which is exactly
    when pending_sync has work to do. Arbitration is the one stage that still
    needs its own model configured; its sweep carries its own lock and checkpoint.
    """
    while True:
        await asyncio.sleep(settings.memory_arbiter_interval_seconds)
        try:
            stats = await _memory_maintenance_sweep(memory, cache, settings)
            if settings.memory_arbiter_model:
                stats["arbitrated"] = await _memory_arbiter_sweep(
                    memory, cache, engine, settings
                )
            if any(stats.values()):
                log.info("memory maintenance: %s", stats)
        except Exception:  # noqa: BLE001 - the loop outlives any single failure
            log.exception("memory maintenance sweep failed")


async def _memory_arbiter_sweep(
    memory: MemoryService,
    cache: CacheBackend,
    engine: AsyncEngine,
    settings: ServerSettings,
) -> int:
    """One arbitration pass over the users whose facts changed recently.

    The Redis lock is the multi-replica guard: every replica runs the same loop,
    but only the lock holder sweeps this tick, so arbitration cost stays flat as
    the deployment scales out (the same reason rate limiting is Redis-backed).
    The checkpoint is written before the sweep's own usage rows could re-mark
    anyone dirty - arbitration meters under the arbiter model, and the dirty
    query filters on the extraction model, so a sweep can never feed itself.
    """
    if not await cache.acquire_lock(
        _ARBITER_LOCK, ttl_seconds=float(settings.memory_arbiter_interval_seconds)
    ):
        return 0
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        since = await cache.get(_ARBITER_CHECKPOINT) or (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat(timespec="seconds")
        dirty = await memory_dirty_users(
            engine, since, settings.memory_model, settings.memory_arbiter_batch
        )
        for user_id, username in dirty:
            await memory.arbitrate(user_id, username)
        await cache.setex(_ARBITER_CHECKPOINT, 86400.0, now)
        return len(dirty)
    finally:
        await cache.release_lock(_ARBITER_LOCK)


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings.from_env()
    # A typo here used to silently mean "no sandbox": bash would run inside the
    # app process with its environment readable. Refuse to start instead.
    validate_sandbox_mode(settings.sandbox)

    db = Database(settings.database_url)
    users = UserRepo(db)
    sessions = SessionRepo(db)
    messages = MessageRepo(db)
    cache = get_backend(settings.redis_url, namespace=settings.redis_ns)
    limiter = RateLimiter(settings.rate_limit_runs_per_min, backend=cache)
    usage_tracker = UsageTracker(db.engine, default_quota=settings.default_quota_tokens)
    audit = AuditLogger(settings.audit_path)
    # One tracer for the whole process, shared by the agent loop and the memory
    # service. Sharing is what makes a retrieval span land inside its run's trace
    # instead of starting a second one: the span stack is a ContextVar, so it only
    # nests when both callers push onto the same tracer.
    tracer = get_tracer(
        settings.tracer_backend,
        endpoint=settings.otlp_endpoint,
        sample_rate=settings.trace_sample_rate,
        service_name=settings.service_name,
        environment=settings.environment,
    )
    metrics = Metrics(enabled=settings.metrics_enabled)
    if metrics.enabled and not settings.metrics_token:
        # Said once at boot rather than left to the docs: an open /metrics is
        # invisible from inside the app, and the series it serves describe traffic
        # volume and model mix to anyone who can reach the port.
        log.warning(
            "/metrics is open (no PI_METRICS_TOKEN). Fine behind a private network, "
            "not fine on a published port."
        )
    # Closure local, like every other collaborator here. The meter is passed through
    # unchanged because UsageSink is defined as exactly UsageTracker.record's
    # keyword arguments - extraction spend lands in usage_records with turns=0.
    memory = MemoryService(
        store=get_store(
            settings.milvus_uri,
            token=settings.milvus_token,
            namespace=settings.milvus_ns,
            dim=settings.embedding_dim or 512,
            num_partitions=settings.milvus_partitions,
        ),
        embedder=get_embedder(settings.embedding_model, dim=settings.embedding_dim),
        # MySQL is the source of truth for facts; the store above only mirrors it.
        repo=UserMemoryRepo(db),
        reranker=get_reranker(settings.rerank_url, settings.rerank_model),
        audit=audit,
        tracer=tracer,
        meter=usage_tracker.record,
        memory_model=settings.memory_model,
        dim=settings.embedding_dim,
        top_k=settings.memory_top_k,
        recall_k=settings.memory_recall_k,
        min_similarity=settings.memory_min_similarity,
        rerank_min_score=settings.memory_rerank_min_score,
        dedup_similarity=settings.memory_dedup_similarity,
        max_facts=settings.memory_max_facts,
        extract_min_chars=settings.memory_extract_min_chars,
        extract_concurrency=settings.memory_extract_concurrency,
        inject_max_chars=settings.memory_inject_max_chars,
        arbiter_model=settings.memory_arbiter_model,
    )
    runs = RunManager(
        policy=server_policy(settings.policy_path),
        audit=audit,
        max_concurrent=settings.max_concurrent_runs,
        timeout_seconds=settings.run_timeout_seconds,
        usage=usage_tracker,
        tracer=tracer,
        cache=cache,
        sandbox=settings.sandbox,
        sandbox_image=settings.sandbox_image,
        memory=memory,
        traces=AgentRunRepo(db),
        trace_retention_days=settings.trace_retention_days,
        metrics=metrics,
    )
    runs.sandbox_network = settings.sandbox_net

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init()
        # MySQL is the audit source of truth from here on; the JSONL file keeps
        # mirroring. Started before any route can write an audit record, closed
        # before db.dispose() so the final flush still has a live engine.
        audit.attach_db(db)
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        # Never raises: an unreachable vector database logs and surfaces through
        # /readyz rather than blocking startup, since most requests do not need it.
        await memory.setup()
        # After setup(): memory.enabled is now known. The maintenance loop runs in
        # degraded mode too - pending-sync retries are exactly what recover it.
        # Sleep-first, so a rolling deploy does not stampede the gateway at boot.
        maintenance: asyncio.Task[None] | None = None
        if memory.enabled:
            maintenance = asyncio.create_task(
                _memory_maintenance_loop(memory, cache, db.engine, settings)
            )
        yield
        if maintenance is not None:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
        # Before db.dispose(): close() drains in-flight extractions, and a drained
        # extraction still writes its metering row through the usage tracker.
        await memory.close()
        await audit.close()
        # Last of the writers, after anything that can still open a span.
        # BatchSpanProcessor holds finished spans until its next scheduled export,
        # so without this the final runs of a process are the ones most likely to
        # be missing from the collector - which is exactly when someone goes looking.
        tracer.shutdown()
        await db.dispose()
        await shutdown_docker_pool()  # destroy warm sandbox containers

    app = FastAPI(title="pi-py server", version="0.1.0", docs_url="/docs", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db

    def custom_openapi() -> dict:
        """Default document plus the SSE payload schemas.

        FastAPI only discovers schemas reachable from a request or response
        model, and the stream route has neither - so the Sse*Data models would
        be absent from components.schemas and web/ would have to hand-write the
        one contract it cannot afford to get wrong.
        """
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {}).setdefault("schemas", {}).update(_SSE_SCHEMA["$defs"])
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        # The trace recorder reads this at run finalize, so its agent_runs row
        # carries the same id as the access-log line and the X-Request-Id header.
        request_id_var.set(request_id)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        log.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                }
            )
        )
        return response

    async def current_user(request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        try:
            payload = decode_token(auth.removeprefix("Bearer "), settings.jwt_secret)
        except pyjwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
        username = str(payload["sub"])
        jti = str(payload.get("jti", ""))
        # per-token revocation (logout): jti blacklist kept for the token's remaining life
        if jti and await cache.get(f"revoked:{jti}") is not None:
            raise HTTPException(status_code=401, detail="token has been revoked")
        # per-user revocation epoch: admin kicked all tokens issued before this time
        epoch = await cache.get(f"epoch:{username}")
        if epoch is not None and float(payload.get("iat", 0)) <= float(epoch):
            raise HTTPException(status_code=401, detail="token has been revoked")
        user = await users.by_username(username)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="account disabled or missing")
        return username

    async def require_admin(username: str = Depends(current_user)) -> str:
        user = await users.by_username(username)
        if user is None or not user.is_admin:
            raise HTTPException(status_code=403, detail="admin privileges required")
        return username

    @app.get("/healthz")
    async def healthz() -> HealthOut:
        """Liveness: the process is up. Checks nothing, so it cannot flap."""
        return {"status": "ok"}

    @app.get(
        "/readyz",
        responses={
            503: {
                "model": ReadyOut,
                "description": "DB or cache is not 'ok'. Same body as 200, with status 'not-ready'. The memory entry is reported but never triggers this.",
            }
        },
    )
    async def readyz() -> ReadyOut:
        """Readiness: DB reachable and cache pingable. Memory is reported alongside
        them but does not gate the status. Returns a JSONResponse directly so the
        status code can be 503 without raising."""
        checks: dict[str, str] = {}
        try:
            await users.count()
            checks["db"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["db"] = f"error: {exc}"
        try:
            checks["cache"] = "ok" if await cache.ping() else "unreachable"
        except Exception as exc:  # noqa: BLE001
            checks["cache"] = f"error: {exc}"
        healthy = all(v == "ok" for v in checks.values())
        # Appended after `healthy` on purpose. Memory is an enhancement: a deployment
        # with no vector database configured is a normal install rather than an
        # outage, and gating readiness on it would pull the whole service out of
        # rotation over a feature most requests never touch.
        if not memory.enabled:
            # 'disabled' (nothing configured), 'unavailable: <ExcType>' (embedder
            # probe or an index-schema guard failed), or 'ready (index degraded)'
            # cannot appear here because that state keeps memory enabled.
            checks["memory"] = memory.status
        else:
            try:
                checks["memory"] = "ok" if await memory.ping() else "unreachable"
            except Exception as exc:  # noqa: BLE001
                checks["memory"] = f"error: {exc}"
        status = 200 if healthy else 503
        return JSONResponse({"status": "ready" if healthy else "not-ready", "checks": checks}, status_code=status)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        """Counters and histograms for scraping, in Prometheus text format 0.0.4.

        Kept out of the OpenAPI document on purpose: it is not part of the contract
        web/ is generated from, and a scraper reads the exposition format rather
        than a schema.

        Every series is aggregate - no username, session id or prompt is ever a
        label, because those grow without bound - but together they do describe
        traffic volume and which models are in use, so PI_METRICS_TOKEN exists for
        a published port. A wrong token answers 404, not 403: an endpoint that says
        "forbidden" has just told a scanner it is there.
        """
        token = settings.metrics_token
        if token:
            presented = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not secrets.compare_digest(presented, token):
                return Response(status_code=404)
        rendered = metrics.render()
        if rendered is None:
            return Response(
                "metrics are off: set PI_METRICS=1 and install pi-py[observability]\n",
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        payload, content_type = rendered
        return Response(payload, media_type=content_type)

    @app.post(
        "/v1/auth/register",
        responses={
            400: _err(
                "Username does not match ^[a-zA-Z0-9_-]{2,32}$. The password "
                "bounds are enforced earlier, by the request body schema (422)."
            ),
            409: _err("Username already exists - including a signup that lost the race to an identical one."),
        },
    )
    async def register(body: RegisterIn, request: Request) -> RegisterOut:
        """Open signup, always a normal user; admin is granted by editing the DB.

        Unauthenticated by design, so 8300 must not be reachable from the internet.
        """
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")

        def record(ok: bool, reason: str = "") -> None:
            audit.auth(action="register", username=body.username, ip=ip,
                       user_agent=ua, ok=ok, reason=reason)

        if not _USERNAME_RE.match(body.username):
            record(False, "invalid_username")
            raise HTTPException(status_code=400, detail="invalid username")
        if await users.by_username(body.username) is not None:
            record(False, "duplicate")
            raise HTTPException(status_code=409, detail="username already exists")
        try:
            user = await users.create(
                body.username,
                # PBKDF2 blocks ~50ms; must not run on the event loop. to_thread
                # is safe here because pbkdf2_hmac releases the GIL in C.
                await asyncio.to_thread(hash_password, body.password),
                is_admin=False,
                quota_tokens=settings.default_quota_tokens,
            )
        except IntegrityError as exc:  # lost the race against an identical signup
            record(False, "duplicate")
            raise HTTPException(status_code=409, detail="username already exists") from exc
        record(True)
        return {"id": user.id, "username": user.username, "is_admin": user.is_admin}

    @app.post(
        "/v1/auth/login",
        responses={
            401: _err(
                "'invalid credentials' for an unknown username or a wrong "
                "password (deliberately indistinguishable), 'account disabled' "
                "only when the password was correct - so the second message "
                "does not enumerate accounts either."
            )
        },
    )
    async def login(body: LoginIn, request: Request) -> LoginOut:
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")

        def record(ok: bool, reason: str = "") -> None:
            audit.auth(action="login", username=body.username, ip=ip,
                       user_agent=ua, ok=ok, reason=reason)

        user = await users.by_username(body.username)
        # PBKDF2 blocks ~50ms; see register() — must not run on the event loop.
        password_ok = user is not None and await asyncio.to_thread(
            verify_password, body.password, user.password_hash
        )
        if not password_ok:
            record(False, "invalid_credentials")
            raise HTTPException(status_code=401, detail="invalid credentials")
        if not user.is_active:
            record(False, "disabled")
            raise HTTPException(status_code=401, detail="account disabled")
        token = create_token(user.username, settings.jwt_secret, settings.token_ttl_minutes)
        record(True)
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": settings.token_ttl_minutes * 60,
            "username": user.username,
            "is_admin": user.is_admin,
        }

    @app.get("/v1/me", responses=UNAUTHORIZED)
    async def me(username: str = Depends(current_user)) -> MeOut:
        """Whoami: the cheapest way for the client to validate a stored token."""
        user = await users.by_username(username)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        return {"username": user.username, "is_admin": user.is_admin}

    @app.delete(
        "/v1/me",
        responses={
            **UNAUTHORIZED,
            400: _err(
                "'password confirmation failed' (the token alone must not be enough "
                "for irreversible erasure), or 'cannot delete the last admin'."
            ),
        },
    )
    async def deregister(
        body: DeregisterIn, request: Request, username: str = Depends(current_user)
    ) -> DeregisterOut:
        """Erase this account and every trace of it.

        The full cascade: memory facts and their vector mirror, then messages,
        sessions, usage rows, the account row, and the workspace directory. All
        interactions leave traces in MySQL by design, so erasing one user means
        walking all of them - in FK order, one transaction for the relational
        part. A run still streaming for this user fails its next message write
        (the session row is gone); that is the accepted behavior, not a crash.

        The revocation epoch is bumped last and matters most on re-registration:
        a token issued to the deleted account must never authenticate against a
        new account created under the same username later.
        """
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")
        user = await users.by_username(username)
        if user is None:
            raise HTTPException(status_code=401, detail="account disabled or missing")
        # PBKDF2 blocks ~50ms; see login().
        password_ok = await asyncio.to_thread(
            verify_password, body.password, user.password_hash
        )
        if not password_ok:
            audit.auth(action="deregister", username=username, ip=ip,
                       user_agent=ua, ok=False, reason="bad_password")
            raise HTTPException(status_code=400, detail="password confirmation failed")
        if user.is_admin and not [
            u for u in await users.list_all()
            if u.is_admin and u.is_active and u.id != user.id
        ]:
            raise HTTPException(status_code=400, detail="cannot delete the last admin")
        # Memory first: clear() counts the repo rows it drops, and purge_user()
        # would take them away before anything could report them. clear() also
        # unconditionally drops the vector mirror - NoOpStore makes that free.
        memories = await memory.clear(user.id)
        purged = await purge_user(db.engine, user.id)
        purged["memories"] += memories
        # The workspace holds user-written files, not database rows: it is erased
        # even when every count above is zero. rmtree can walk many files, so it
        # runs off the event loop.
        await asyncio.to_thread(shutil.rmtree, settings.workspace_root / username, True)
        # Kills every token issued before now - including the one making this
        # call - and any stale token that would otherwise match a re-registered
        # same-name account within this TTL. Sub-second: iat has that
        # resolution, so a token minted after this moment is never caught.
        await cache.setex(
            f"epoch:{username}",
            settings.token_ttl_minutes * 60 + 60,
            str(time.time()),
        )
        audit.auth(action="deregister", username=username, ip=ip, user_agent=ua, ok=True)
        return {"username": username, "deleted": True, "purged": purged}

    @app.get("/v1/sessions", responses=UNAUTHORIZED)
    async def list_sessions(username: str = Depends(current_user)) -> SessionListOut:
        user = await users.by_username(username)
        rows = await sessions.list_for_user(user.id)
        return {
            "sessions": [
                {
                    "id": r.id,
                    "title": r.title,
                    "model": r.model,
                    "created_at": r.created_at,
                    "plan": _plan_of(r),
                }
                for r in rows
            ]
        }

    @app.post("/v1/sessions", responses=UNAUTHORIZED)
    async def create_session(body: SessionIn, username: str = Depends(current_user)) -> SessionCreatedOut:
        user = await users.by_username(username)
        model = body.model or settings.default_model
        cwd = settings.workspace_root / username
        cwd.mkdir(parents=True, exist_ok=True)
        row = await sessions.create(user.id, body.title, model, cwd)
        return {"id": row.id, "title": row.title, "model": row.model, "cwd": row.cwd}

    async def _owned_session(session_id: str, username: str):
        user = await users.by_username(username)
        row = await sessions.for_user(user.id, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row

    def _plan_of(row: SessionRow) -> Plan | None:
        """Parse sessions.plan. No corruption fallback, for the same reason
        get_messages has none: the column is only ever written by
        Plan.model_dump_json(), so a value that fails to parse is a bug to surface,
        not a state to paper over."""
        return Plan.model_validate_json(row.plan) if row.plan else None

    @app.get("/v1/sessions/{session_id}", responses={**UNAUTHORIZED, **NOT_FOUND})
    async def get_session(session_id: str, username: str = Depends(current_user)) -> SessionSummary:
        row = await _owned_session(session_id, username)
        return {
            "id": row.id,
            "title": row.title,
            "model": row.model,
            "created_at": row.created_at,
            "plan": _plan_of(row),
        }

    @app.get("/v1/sessions/{session_id}/messages", responses={**UNAUTHORIZED, **NOT_FOUND})
    async def get_messages(session_id: str, username: str = Depends(current_user)) -> MessageListOut:
        """Full transcript, oldest first.

        The `blocks` column holds a whole serialized Message, not a bare block
        array: runner.py writes m.model_dump_json() and reads history back with
        Message.model_validate_json(). Returning the raw parse therefore nested
        a duplicate of the message under `blocks` - parse it as a Message and
        take the array.
        """
        await _owned_session(session_id, username)
        rows = await messages.list_for_session(session_id)
        out: list[MessageOut] = []
        for r in rows:
            msg = Message.model_validate_json(r.blocks)
            out.append(MessageOut(idx=r.idx, role=msg.role, blocks=msg.blocks))
        return MessageListOut(messages=out)

    def _uploads_dir(row: SessionRow) -> Path:
        """Per-session subdir of the (per-user, session-shared) cwd. Only files
        under here are ever exposed by the public download route - the rest of
        the workspace holds agent-written files that must stay private."""
        return Path(row.cwd) / "uploads" / row.id

    def _public_file_url(session_id: str, name: str) -> str:
        return f"{settings.public_base_url}/files/{session_id}/{quote(name)}"

    async def _file_blocks(row: SessionRow, names: list[str]) -> list[FileBlock]:
        """Resolve run-requested file names to attachable FileBlocks.

        A name that was never uploaded is a 404, not a silent skip: the user
        asked for the file to be read, and the model answering without it
        would look exactly like the model ignoring the attachment.
        """
        if not names:
            return []
        if not settings.public_base_url:
            raise HTTPException(
                status_code=400,
                detail="file attachments are disabled: PI_PUBLIC_BASE_URL is not set",
            )
        blocks: list[FileBlock] = []
        for n in names:
            name = _clean_file_name(n)
            if not (_uploads_dir(row) / name).is_file():
                raise HTTPException(status_code=404, detail=f"file not uploaded: {name}")
            blocks.append(FileBlock(file_url=_public_file_url(row.id, name), name=name))
        return blocks

    @app.post(
        "/v1/sessions/{session_id}/files",
        responses={
            **UNAUTHORIZED,
            **NOT_FOUND,
            400: _err("PI_PUBLIC_BASE_URL is not set, or the file name is invalid."),
            413: _err("File exceeds the PI_MAX_UPLOAD_BYTES limit."),
        },
    )
    async def upload_file(
        session_id: str, file: UploadFile, username: str = Depends(current_user)
    ) -> FileOut:
        """Upload a file into this session's workspace.

        The file becomes attachable to runs (`RunIn.files`) and downloadable by
        anyone holding the returned URL - which is the point: the model gateway
        fetches it with no credentials when a run references it.
        """
        row = await _owned_session(session_id, username)
        if not settings.public_base_url:
            raise HTTPException(
                status_code=400,
                detail="file uploads are disabled: PI_PUBLIC_BASE_URL is not set",
            )
        name = _clean_file_name(file.filename or "")
        data = await file.read()
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds the {settings.max_upload_bytes}-byte limit",
            )
        target = _uploads_dir(row) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        audit.file(action="upload", user_id=username, session_id=row.id, name=name, size=len(data))
        return FileOut(name=name, size=len(data), url=_public_file_url(row.id, name))

    @app.get("/v1/sessions/{session_id}/files", responses={**UNAUTHORIZED, **NOT_FOUND})
    async def list_files(
        session_id: str, username: str = Depends(current_user)
    ) -> FileListOut:
        """Files uploaded to this session, with their public download URLs."""
        row = await _owned_session(session_id, username)
        out: list[FileOut] = []
        d = _uploads_dir(row)
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file():
                    out.append(
                        FileOut(
                            name=p.name,
                            size=p.stat().st_size,
                            url=_public_file_url(row.id, p.name),
                        )
                    )
        return FileListOut(files=out)

    @app.get(
        "/files/{session_id}/{name}",
        response_class=FileResponse,
        responses={**NOT_FOUND},
    )
    async def download_file(session_id: str, name: str) -> FileResponse:
        """Public download of a session upload.

        No auth: the model gateway must fetch this URL with no token, so the
        unguessable session id plus the file name is the bearer capability.
        Every hit is audited under the session's owner.
        """
        clean = _clean_file_name(name)
        row = await sessions.by_id(session_id)
        path = _uploads_dir(row) / clean if row is not None else None
        if row is None or path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        owner = await users.by_id(row.user_id)
        audit.file(
            action="download",
            user_id=owner.username if owner else f"user:{row.user_id}",
            session_id=row.id,
            name=clean,
            size=path.stat().st_size,
        )
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(clean)[0] or "application/octet-stream",
            filename=clean,
        )

    @app.post(
        "/v1/sessions/{session_id}/runs",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": SSE_DOC,
                "content": {"text/event-stream": {"schema": {"anyOf": _SSE_SCHEMA["anyOf"]}}},
            },
            **UNAUTHORIZED,
            **NOT_FOUND,
            402: _err(
                "Monthly token quota exhausted. The detail carries the used/limit "
                "figures; GET /v1/usage returns them as numbers."
            ),
            429: {
                **_err("Per-user fixed-window run limit (PI_RATE_LIMIT_RUNS_PER_MIN) exceeded."),
                "headers": {
                    "Retry-After": {
                        "schema": {"type": "integer"},
                        "description": "Whole seconds to wait, always at least 1.",
                    }
                },
            },
        },
    )
    async def run(
        session_id: str, body: RunIn, username: str = Depends(current_user)
    ) -> StreamingResponse:
        user = await users.by_username(username)
        row = await _owned_session(session_id, username)
        if not await limiter.allow(username):
            retry = limiter.retry_after(username)
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded, retry after {retry:.0f}s",
                headers={"Retry-After": str(int(retry) + 1)},
            )
        quota = await usage_tracker.quota_check(user.id, user.quota_tokens)
        if not quota.allowed:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"monthly token quota exhausted: used {quota.used_tokens} / "
                    f"{quota.quota_tokens} tokens"
                ),
            )
        model = body.model or row.model or DEFAULT_MODEL
        files = await _file_blocks(row, body.files)

        async def stream():
            yield f"event: start\ndata: {json.dumps({'session': row.id, 'model': model})}\n\n"
            async for ev in runs.run_turn(
                session=row,
                username=username,
                user_id=user.id,
                prompt=body.prompt,
                model=model,
                message_repo=messages,
                session_repo=sessions,
                enable_search=body.enable_search,
                builtin_tools=list(body.builtin_tools),
                files=files,
            ):
                # Retrieval and per-turn model calls are recorded in agent_steps
                # and dropped here: neither is in the SSE contract, so sending
                # them would put `event: unknown` on the wire for every run.
                if isinstance(ev, TRACE_ONLY_EVENTS):
                    continue
                yield event_to_sse(ev)
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/auth/logout", responses=UNAUTHORIZED)
    async def logout(request: Request, username: str = Depends(current_user)) -> LogoutOut:
        """Blacklist this token's jti for its remaining lifetime.

        The token stays cryptographically valid; revocation is a cache lookup,
        so it is lost if the cache is flushed before the token would expire.
        """
        auth = request.headers.get("Authorization", "")
        payload = decode_token(auth.removeprefix("Bearer "), settings.jwt_secret)
        jti = str(payload.get("jti", ""))
        remaining = max(60, int(payload.get("exp", 0)) - int(time.time()))
        if jti:
            await cache.setex(f"revoked:{jti}", remaining, "1")
        return {"revoked": True, "username": username}

    @app.get("/v1/admin/users", responses=ADMIN_REQUIRED)
    async def admin_list_users(admin: str = Depends(require_admin)) -> AdminUserListOut:
        rows = await users.list_all()
        return {
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "is_admin": u.is_admin,
                    "is_active": u.is_active,
                    "quota_tokens": u.quota_tokens,
                    "created_at": u.created_at,
                }
                for u in rows
            ]
        }

    @app.patch(
        "/v1/admin/users/{username}",
        responses={
            **ADMIN_REQUIRED,
            400: _err("'cannot disable yourself' - the last admin must not be able to lock the panel out."),
            404: _err("No such username."),
        },
    )
    async def admin_update_user(
        username: str, body: UserUpdateIn, admin: str = Depends(require_admin)
    ) -> UserUpdateOut:
        """Partial update: omitted fields are untouched.

        Setting is_active=false also bumps the user's revocation epoch, so every
        token issued before now is rejected on its next request.
        """
        user = await users.by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        changed: list[str] = []
        if body.quota_tokens is not None:
            await users.set_quota(user.id, body.quota_tokens)
            changed.append("quota_tokens")
        if body.is_active is not None:
            if not body.is_active and user.username == admin:
                raise HTTPException(status_code=400, detail="cannot disable yourself")
            await users.set_active(user.id, body.is_active)
            changed.append("is_active")
            if not body.is_active:
                # kick all active tokens for this user
                await cache.setex(f"epoch:{username}", settings.token_ttl_minutes * 60 + 60, str(time.time()))
        return {"username": username, "changed": changed}

    @app.post(
        "/v1/admin/users/{username}/revoke",
        responses={**ADMIN_REQUIRED, 404: _err("No such username.")},
    )
    async def admin_revoke_user(username: str, admin: str = Depends(require_admin)) -> RevokeOut:
        """Invalidate every active token of a user without disabling the account."""
        user = await users.by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        await cache.setex(f"epoch:{username}", settings.token_ttl_minutes * 60 + 60, str(time.time()))
        return {"username": username, "revoked": True}

    @app.get("/v1/admin/audit", responses=ADMIN_REQUIRED)
    async def admin_audit(
        admin: str = Depends(require_admin),
        user: str | None = None,
        tool: str | None = None,
        event: str | None = None,
        limit: int = 50,
    ) -> AuditOut:
        """Audit history from audit_events, newest first.

        Reads MySQL, not the JSONL mirror: history is no longer bounded by the
        daily file rotation, and the indexed columns (actor/tool/event) do the
        filtering instead of a Python scan. `user` matches both tool_call
        records (keyed by username) and auth records (keyed by username);
        `limit` is capped at 500 server-side, silently reduced rather than
        rejected. Records surface a beat after the audited request: they reach
        this table through the async drainer, not on the request path.
        """
        records = await AuditEventRepo(db).list_recent(
            event=event or "",
            actor=user or "",
            tool=tool or "",
            limit=limit,
        )
        return {"records": records}

    @app.get("/v1/admin/traces", responses=ADMIN_REQUIRED)
    async def admin_traces(
        admin: str = Depends(require_admin),
        user: str | None = None,
        session: str | None = None,
        status: str | None = None,
        anomaly: bool = False,
        limit: int = 50,
    ) -> TraceListOut:
        """Execution traces, newest first: one agent_runs row per POST /runs.

        `anomaly=true` keeps only flagged runs (failed / empty / tool storm).
        `limit` is capped at 200 server-side. The single-run view
        (/v1/admin/traces/{run_id}) carries the steps and the transcript; this
        list view omits them so a page stays one query. The prompt is
        truncated to 200 chars here - the detail view carries it in full.
        """
        rows = await AgentRunRepo(db).list_runs(
            username=user or "",
            session_id=session or "",
            status=status or "",
            anomalous=anomaly,
            limit=limit,
        )
        return {
            "runs": [
                {
                    "run_id": r.run_id,
                    "username": r.username,
                    "session_id": r.session_id,
                    "model": r.model,
                    "prompt": r.prompt[:200],
                    "request_id": r.request_id,
                    "enable_search": r.enable_search,
                    "builtin_tools": [t for t in r.builtin_tools.split(",") if t],
                    "first_idx": r.first_idx,
                    "last_idx": r.last_idx,
                    "status": r.status,
                    "error": r.error,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "turns": r.turns,
                    "failed_tools": r.failed_tools,
                    "duration_ms": r.duration_ms,
                    "flags": [f for f in r.flags.split(",") if f],
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                }
                for r in rows
            ]
        }

    @app.get(
        "/v1/admin/traces/{run_id}",
        responses={**ADMIN_REQUIRED, 404: _err("No run with this id.")},
    )
    async def admin_trace(
        run_id: str, admin: str = Depends(require_admin)
    ) -> TraceRunOut:
        """One run, complete: prompt, capability flags, ordered steps with full
        tool arguments and results, and the transcript slice it wrote.

        Retention deletes old traces, so a 404 can also mean "older than
        PI_TRACE_RETENTION_DAYS". A run whose session was deleted still shows
        steps but no messages (the idx range points at rows that are gone).
        """
        found = await AgentRunRepo(db).get_run(run_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such run")
        run, steps = found
        slice_msgs: list[MessageOut] = []
        if run.first_idx is not None and run.last_idx is not None:
            for r in await messages.list_range(run.session_id, run.first_idx, run.last_idx):
                msg = Message.model_validate_json(r.blocks)
                slice_msgs.append(MessageOut(idx=r.idx, role=msg.role, blocks=msg.blocks))
        return {
            "run_id": run.run_id,
            "username": run.username,
            "session_id": run.session_id,
            "model": run.model,
            "prompt": run.prompt,
            "request_id": run.request_id,
            "enable_search": run.enable_search,
            "builtin_tools": [t for t in run.builtin_tools.split(",") if t],
            "first_idx": run.first_idx,
            "last_idx": run.last_idx,
            "status": run.status,
            "error": run.error,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "turns": run.turns,
            "failed_tools": run.failed_tools,
            "duration_ms": run.duration_ms,
            "flags": [f for f in run.flags.split(",") if f],
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "steps": [
                {
                    "seq": s.seq,
                    "kind": s.kind,
                    "name": s.name,
                    "ok": s.ok,
                    "args": s.args,
                    "detail": s.detail,
                    "duration_ms": s.duration_ms,
                    "ts": s.ts,
                }
                for s in steps
            ],
            "messages": slice_msgs,
        }

    @app.get("/v1/usage", responses=UNAUTHORIZED)
    async def usage_summary(username: str = Depends(current_user)) -> UsageOut:
        """This month's token spend per model, plus the caller's quota position."""
        user = await users.by_username(username)
        summary = await usage_tracker.monthly_summary(username)
        quota = await usage_tracker.quota_check(user.id, user.quota_tokens)
        summary["quota_tokens"] = quota.quota_tokens
        summary["used_tokens"] = quota.used_tokens
        return summary

    @app.get("/v1/memories", responses=UNAUTHORIZED)
    async def list_memories(
        username: str = Depends(current_user), limit: int = 100
    ) -> MemoryListOut:
        """The caller's stored long-term facts, oldest first.

        The user id comes from the token and never from a parameter, so there is no
        way to ask for somebody else's memory. `limit` is capped server-side at 500.
        """
        user = await users.by_username(username)
        facts = await memory.list_for_user(user.id, limit=limit)
        return {
            "facts": [
                {
                    "id": f.id,
                    "text": f.text,
                    "kind": f.kind,
                    "source_session": f.source_session,
                    "created_at": f.created_at,
                }
                for f in facts
            ],
            "status": memory.status,
        }

    @app.delete(
        "/v1/memories/{fact_id}",
        responses={
            **UNAUTHORIZED,
            404: _err("No such fact - including one that exists but belongs to another user."),
        },
    )
    async def delete_memory(fact_id: int, username: str = Depends(current_user)) -> MemoryDeleteOut:
        """Forget one fact.

        The path id is not authority on its own: the delete is scoped to the caller,
        so guessing another tenant's id is a 404 rather than a removal. That also
        means the 404 leaks nothing - it is identical for "not yours" and "never
        existed".
        """
        user = await users.by_username(username)
        deleted = await memory.delete(user.id, fact_id)
        audit.memory(action="delete", user_id=username, facts=1 if deleted else 0)
        if not deleted:
            raise HTTPException(status_code=404, detail="no such memory")
        return {"deleted": True}

    @app.delete("/v1/memories", responses=UNAUTHORIZED)
    async def clear_memories(username: str = Depends(current_user)) -> MemoryClearOut:
        """Forget everything. This is the erasure endpoint (PIPL art. 47 / GDPR art. 17).

        Returns a count rather than a flag so the caller can tell "you had nothing
        stored" from "memory is not enabled here" without a second request.
        """
        user = await users.by_username(username)
        deleted = await memory.clear(user.id)
        audit.memory(action="clear", user_id=username, facts=deleted)
        return {"deleted": deleted}

    # Mounted last on purpose: a Mount at "/" matches every path, so it has to
    # lose to /v1/*, /healthz, /readyz, /openapi.json and /docs.
    #
    # The UI is served by this process because there is no CORS middleware, and
    # adding one would only mean the browser's origin and the API's origin differ
    # for no benefit. No dist directory means nobody ran `npm run build` here,
    # which is normal for a backend-only or test deployment.
    if settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")

    return app
