"""pi-py multi-user server: FastAPI app with JWT auth, session isolation, SSE runs.

Boot: PI_JWT_SECRET (auto-generated for dev), PI_DATABASE_URL (required;
mysql+aiomysql:// or postgresql+asyncpg://), PI_MODEL, PI_POLICY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import jwt as pyjwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from pi.llm import DEFAULT_MODEL
from pi.observability.metering import UsageTracker
from pi.observability.tracing import get_tracer
from pi.server.auth import create_token, decode_token, hash_password, verify_password
from pi.server.cache import get_backend
from pi.server.config import ServerSettings
from pi.server.db import Database, MemoryRepo, MessageRepo, SessionRepo, UserRepo
from pi.server.ratelimit import RateLimiter
from pi.server.runner import RunManager, event_to_sse, server_policy
from pi.security.audit import AuditLogger
from pi.security.redact import mask_url
from pi.tools.mcp import McpToolProvider
from pi.tools.registry import BuiltinToolProvider, ToolProvider, ToolRegistry
from pi.tools.sandbox import shutdown_docker_pool, validate_sandbox_mode
from pi.tools.skill import SkillToolProvider

log = logging.getLogger("pi.server")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")


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


class SessionIn(BaseModel):
    title: str = "session"
    model: str | None = None


class UserUpdateIn(BaseModel):
    quota_tokens: int | None = None
    is_active: bool | None = None


class RunIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=32_000)
    model: str | None = None


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings.from_env()
    # A typo here used to silently mean "no sandbox": bash would run inside the
    # app process with its environment readable. Refuse to start instead.
    validate_sandbox_mode(settings.sandbox)

    db = Database(settings.database_url)
    users = UserRepo(db)
    sessions = SessionRepo(db)
    messages = MessageRepo(db)
    memories = MemoryRepo(db)
    # Vector semantic memory: all four PI_EMBEDDING_*/PI_MILVUS_URI vars must be
    # set; otherwise MemoryRepo stays lexical-only. Both clients are lazy (no
    # network I/O here), so create_app stays fast and testable.
    vector_store = None
    if settings.vector_memory_enabled:
        from pi.llm.embedding import EmbeddingClient
        from pi.server.vectorstore import MilvusStore

        embedder = EmbeddingClient(
            settings.embedding_url, settings.embedding_api_key, settings.embedding_model
        )
        vector_store = MilvusStore(settings.milvus_uri)

        # Meter embedding tokens like LLM tokens: recorded under
        # model "embedding/<model>" so the monthly per-model breakdown (and the
        # token quota) reflects the real spend. Accounting failures are logged
        # and swallowed by MemoryRepo - never a run failure.
        async def on_embed_usage(user_id: int, tokens: int) -> None:
            row = await users.by_id(user_id)
            if row is None:
                return
            await usage_tracker.record(
                user_id=user_id,
                username=row.username,
                session_id="",
                model=f"embedding/{settings.embedding_model}",
                input_tokens=tokens,
                output_tokens=0,
                turns=0,
            )

        memories = MemoryRepo(
            db, vector_store=vector_store, embedder=embedder, on_embed_usage=on_embed_usage
        )
        # mask_url: a serverless Milvus URI can embed a token in its hostname
        # section - the startup log lands in journald and must not carry it.
        log.info(
            "vector memory enabled: milvus=%s model=%s",
            mask_url(settings.milvus_uri),
            settings.embedding_model,
        )
    cache = get_backend(settings.redis_url, namespace=settings.redis_ns)
    # Tool sources: builtin always; MCP / skills when configured. Warmup happens
    # in lifespan (create_app is sync, and MCP connects spawn child processes).
    providers: list[ToolProvider] = [BuiltinToolProvider()]
    if settings.mcp_servers:
        providers.append(McpToolProvider(settings.mcp_servers))
    if settings.skills_dir:
        providers.append(SkillToolProvider([Path(settings.skills_dir)]))
    registry = ToolRegistry(providers)
    limiter = RateLimiter(settings.rate_limit_runs_per_min, backend=cache)
    usage_tracker = UsageTracker(db.engine, default_quota=settings.default_quota_tokens)
    audit = AuditLogger(settings.audit_path)
    runs = RunManager(
        policy=server_policy(settings.policy_path),
        audit=audit,
        max_concurrent=settings.max_concurrent_runs,
        timeout_seconds=settings.run_timeout_seconds,
        usage=usage_tracker,
        tracer=get_tracer(settings.tracer_backend),
        cache=cache,
        sandbox=settings.sandbox,
        sandbox_image=settings.sandbox_image,
        registry=registry,
    )
    runs.sandbox_network = settings.sandbox_net

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init()
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        await registry.warmup()  # preconnect MCP servers / load skill tools
        yield
        await registry.close()  # terminate MCP child processes
        await db.dispose()
        if vector_store is not None:
            try:
                await vector_store.close()
            except Exception:  # noqa: BLE001 - teardown must not block shutdown
                log.debug("vector store close failed", exc_info=True)
        await shutdown_docker_pool()  # destroy warm sandbox containers

    app = FastAPI(title="pi-py server", version="0.1.0", docs_url="/docs", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.vector_store = vector_store

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
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
        if epoch is not None and int(payload.get("iat", 0)) <= int(epoch):
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
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict:
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
        if vector_store is not None:
            # Informational: a Milvus outage degrades retrieval quality, not
            # correctness (lexical fallback) - unlike the cache check, whose
            # failure is a multi-instance lock hazard and does flip status.
            try:
                checks["milvus"] = "ok" if await vector_store.ping() else "degraded"
            except Exception as exc:  # noqa: BLE001
                checks["milvus"] = f"degraded: {exc}"
        healthy = all(v in ("ok", "degraded") for v in checks.values())
        status = 200 if healthy else 503
        return JSONResponse({"status": "ready" if healthy else "not-ready", "checks": checks}, status_code=status)

    @app.post("/v1/auth/register")
    async def register(body: RegisterIn, request: Request) -> dict:
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

    @app.post("/v1/auth/login")
    async def login(body: LoginIn, request: Request) -> dict:
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
        return {"access_token": token, "token_type": "bearer", "expires_in": settings.token_ttl_minutes * 60}

    @app.get("/v1/me")
    async def me(username: str = Depends(current_user)) -> dict:
        return {"username": username}

    @app.get("/v1/sessions")
    async def list_sessions(username: str = Depends(current_user)) -> dict:
        user = await users.by_username(username)
        rows = await sessions.list_for_user(user.id)
        return {
            "sessions": [
                {"id": r.id, "title": r.title, "model": r.model, "created_at": r.created_at}
                for r in rows
            ]
        }

    @app.post("/v1/sessions")
    async def create_session(body: SessionIn, username: str = Depends(current_user)) -> dict:
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

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, username: str = Depends(current_user)) -> dict:
        row = await _owned_session(session_id, username)
        return {"id": row.id, "title": row.title, "model": row.model, "created_at": row.created_at}

    @app.get("/v1/sessions/{session_id}/messages")
    async def get_messages(session_id: str, username: str = Depends(current_user)) -> dict:
        await _owned_session(session_id, username)
        rows = await messages.list_for_session(session_id)
        return {
            "messages": [
                {"idx": r.idx, "role": r.role, "blocks": json.loads(r.blocks)} for r in rows
            ]
        }

    @app.post("/v1/sessions/{session_id}/runs")
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

        async def stream():
            yield f"event: start\ndata: {json.dumps({'session': row.id, 'model': model})}\n\n"
            async for ev in runs.run_turn(
                session=row,
                username=username,
                user_id=user.id,
                prompt=body.prompt,
                model=model,
                message_repo=messages,
                memory_repo=memories,
            ):
                yield event_to_sse(ev)
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/auth/logout")
    async def logout(request: Request, username: str = Depends(current_user)) -> dict:
        auth = request.headers.get("Authorization", "")
        payload = decode_token(auth.removeprefix("Bearer "), settings.jwt_secret)
        jti = str(payload.get("jti", ""))
        remaining = max(60, int(payload.get("exp", 0)) - int(time.time()))
        if jti:
            await cache.setex(f"revoked:{jti}", remaining, "1")
        return {"revoked": True, "username": username}

    @app.get("/v1/admin/users")
    async def admin_list_users(admin: str = Depends(require_admin)) -> dict:
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

    @app.patch("/v1/admin/users/{username}")
    async def admin_update_user(username: str, body: UserUpdateIn, admin: str = Depends(require_admin)) -> dict:
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
                await cache.setex(f"epoch:{username}", settings.token_ttl_minutes * 60 + 60, str(int(time.time())))
        return {"username": username, "changed": changed}

    @app.post("/v1/admin/users/{username}/revoke")
    async def admin_revoke_user(username: str, admin: str = Depends(require_admin)) -> dict:
        """Invalidate every active token of a user without disabling the account."""
        user = await users.by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        await cache.setex(f"epoch:{username}", settings.token_ttl_minutes * 60 + 60, str(int(time.time())))
        return {"username": username, "revoked": True}

    @app.get("/v1/admin/audit")
    async def admin_audit(
        admin: str = Depends(require_admin),
        user: str | None = None,
        tool: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Tail the daily audit JSONL with optional user/tool filters."""
        import json as json_mod
        from datetime import datetime, timezone as tz

        from pi.security.audit import _daily_path

        day = datetime.now(tz.utc).strftime("%Y-%m-%d")
        path = _daily_path(settings.audit_path, day)
        records: list[dict] = []
        if path.is_file():
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                try:
                    rec = json_mod.loads(line)
                except json_mod.JSONDecodeError:
                    continue
                # auth records carry "username", tool_call/compaction carry "user"
                if user and rec.get("user") != user and rec.get("username") != user:
                    continue
                if tool and rec.get("tool") != tool:
                    continue
                records.append(rec)
                if len(records) >= min(limit, 500):
                    break
        return {"records": records}

    @app.get("/v1/usage")
    async def usage_summary(username: str = Depends(current_user)) -> dict:
        user = await users.by_username(username)
        summary = await usage_tracker.monthly_summary(username)
        quota = await usage_tracker.quota_check(user.id, user.quota_tokens)
        summary["quota_tokens"] = quota.quota_tokens
        summary["used_tokens"] = quota.used_tokens
        return summary

    return app
