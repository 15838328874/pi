"""pi-py multi-user server: FastAPI app with JWT auth, session isolation, SSE runs.

Boot: PI_JWT_SECRET (auto-generated for dev), PI_DATABASE_URL (required;
mysql+aiomysql:// or postgresql+asyncpg://), PI_MODEL, PI_POLICY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import jwt as pyjwt
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from pi.llm import DEFAULT_MODEL
from pi.observability.metering import UsageTracker
from pi.observability.metrics import Metrics
from pi.observability.tracing import get_tracer
from pi.rag.integration import RagToolProvider, health as rag_health, install, shutdown_rag
from pi.server.auth import create_token, decode_token, hash_password, verify_password
from pi.server.cache import get_backend
from pi.server.config import ServerSettings
from pi.server.db import AuditRepo, Database, FileRepo, MemoryRepo, MessageRepo, RunRepo, SessionRepo, UserRepo
from pi.server.storage import ObjectStore
from pi.server.ratelimit import RateLimiter
from pi.server.runner import RunManager, event_to_sse, server_policy
from pi.server.trajectory_store import latest_trajectory
from pi.security.audit import AuditLogger
from pi.security.redact import mask_url
from pi.tools.mcp import McpToolProvider
from pi.tools.registry import BuiltinToolProvider, ToolProvider, ToolRegistry
from pi.tools.sandbox import sandbox_health, shutdown_docker_pool, validate_sandbox_mode
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


class FileUploadIn(BaseModel):
    """Initiate an upload: server signs a presigned PUT URL (direct handshake),
    after de-duplicating on (user_id, sha256). No bytes pass through the app."""

    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    content_type: str = ""
    sha256: str = Field(min_length=64, max_length=64)


class FileCommitIn(BaseModel):
    """Confirm the direct PUT completed; server HEADs the object and records it."""

    object_key: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    sha256: str = Field(min_length=64, max_length=64)
    content_type: str = ""


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings.from_env()
    # A typo here used to silently mean "no sandbox": bash would run inside the
    # app process with its environment readable. Refuse to start instead.
    validate_sandbox_mode(settings.sandbox)

    db = Database(settings.database_url)
    # Cache backend (Redis or in-memory) is built before MemoryRepo so the
    # per-user add lock can be injected; it is also shared by limiter + runner.
    cache = get_backend(settings.redis_url, namespace=settings.redis_ns)
    users = UserRepo(db)
    sessions = SessionRepo(db)
    messages = MessageRepo(db)
    memories = MemoryRepo(db, cache=cache)
    runs_repo = RunRepo(db)
    files_repo = FileRepo(db)
    store = ObjectStore(settings)
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

        async def report_retrieval(outcome: str, duration: float) -> None:
            # metrics.retrieval is sync and no-ops when disabled; the async
            # wrapper matches MemoryRepo's callback seam.
            metrics.retrieval(outcome=outcome, duration_s=duration)

        memories = MemoryRepo(
            db,
            vector_store=vector_store,
            embedder=embedder,
            on_embed_usage=on_embed_usage,
            on_retrieval=report_retrieval,
            cache=cache,
        )
        # mask_url: a serverless Milvus URI can embed a token in its hostname
        # section - the startup log lands in journald and must not carry it.
        log.info(
            "vector memory enabled: milvus=%s model=%s",
            mask_url(settings.milvus_uri),
            settings.embedding_model,
        )
    # Tool sources: builtin always; MCP / skills when configured. Warmup happens
    # in lifespan (create_app is sync, and MCP connects spawn child processes).
    providers: list[ToolProvider] = [BuiltinToolProvider()]
    # Enterprise RAG contributes rag_search as its own provider. ToolRegistry
    # merges + dedupes providers into the single list AgentLoop sees, so the
    # tool still passes the policy gate / audit log / tracing / quota path like
    # any builtin - and PI_RAG_ENABLED=0 contributes nothing at all (no enabled
    # flag threaded through the tool list). All RAG wiring lives in
    # pi.rag.integration; pi/tools/__init__.py is deliberately untouched.
    if settings.rag_enabled:
        providers.append(RagToolProvider())
    if settings.mcp_servers:
        providers.append(McpToolProvider(settings.mcp_servers))
    if settings.skills_dir:
        providers.append(SkillToolProvider([Path(settings.skills_dir)]))
    registry = ToolRegistry(providers)
    metrics = Metrics(enabled=settings.metrics_enabled)
    if settings.metrics_enabled and not settings.metrics_token:
        # Said once at boot rather than left to the docs: an open /metrics is
        # fine behind a private network and a risk on a published port.
        log.warning(
            "/metrics is open (no PI_METRICS_TOKEN). Fine behind a private "
            "network, a leak on a published port."
        )
    limiter = RateLimiter(settings.rate_limit_runs_per_min, backend=cache)
    usage_tracker = UsageTracker(db.engine, default_quota=settings.default_quota_tokens)
    # Enterprise document RAG (pi.rag). Assembled here so the runtime shares the
    # server's engine and metering instead of opening a second pool; every
    # backend inside is lazy (Milvus on first use, httpx per call), so
    # create_app stays fast and testable without infra.
    rag_runtime = None
    if settings.rag_enabled:
        rag_runtime = install(
            db=db,
            users=users,
            usage_tracker=usage_tracker,
            metrics=metrics,
            allow_memory_vector=settings.rag_memory_vector,
        )
    else:
        log.info("rag disabled (PI_RAG_ENABLED=0); rag_search will report unavailable")
    # Audit dual-write: jsonl stays the compliance copy, audit_events the
    # structured query mirror (admin console filters). Mirror is best-effort.
    audit_repo = AuditRepo(db)

    async def _audit_to_db(record: dict) -> None:
        await audit_repo.save(record)

    audit = AuditLogger(settings.audit_path, on_record=_audit_to_db, retention_days=settings.audit_retention_days)
    runs = RunManager(
        policy=server_policy(settings.policy_path),
        audit=audit,
        max_concurrent=settings.max_concurrent_runs,
        timeout_seconds=settings.run_timeout_seconds,
        max_turns=settings.max_turns,
        compact_threshold=settings.compact_threshold,
        compact_keep=settings.compact_keep,
        max_cost_usd=settings.max_cost_usd,
        usage=usage_tracker,
        tracer=get_tracer(settings.tracer_backend, metrics=metrics),
        cache=cache,
        sandbox=settings.sandbox,
        sandbox_image=settings.sandbox_image,
        registry=registry,
        metrics=metrics,
        trajectory_path=settings.trajectory_path,
        pool_ttl_s=settings.sandbox_pool_ttl_s,
        pool_ttl_tight_s=settings.sandbox_pool_ttl_tight_s,
        pool_size=settings.sandbox_pool_size,
        pool_pressure_high=settings.sandbox_pool_pressure_high,
        pool_pressure_low=settings.sandbox_pool_pressure_low,
        files_repo=files_repo,
        store=store,
        workspace_max_bytes=settings.workspace_max_bytes,
    )
    runs.sandbox_network = settings.sandbox_net

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init()
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        await store.ensure_buckets()  # object storage buckets（未配置/不可达→best-effort 跳过）
        await registry.warmup()  # preconnect MCP servers / load skill tools
        if rag_runtime is not None:
            # Ingest jobs live only in this process, so any row still pending at
            # startup has no worker behind it - mark it failed instead of
            # leaving a forever-stuck status (users can simply re-upload).
            try:
                n = await rag_runtime.store.mark_stale_pending(
                    "interrupted by server restart"
                )
                if n:
                    log.warning("marked %d stale pending RAG docs as failed", n)
            except Exception:  # noqa: BLE001 - sweep must never block startup
                log.exception("rag stale-pending sweep failed")
        yield
        await registry.close()  # terminate MCP child processes
        await db.dispose()
        if vector_store is not None:
            try:
                await vector_store.close()
            except Exception:  # noqa: BLE001 - teardown must not block shutdown
                log.debug("vector store close failed", exc_info=True)
        if rag_runtime is not None:
            # Close AND unpublish the runtime (never just .close()). A closed-but
            # -still-published runtime would make a later get_runtime() hand back
            # a disposed engine instead of rebuilding one. Unconditional-safe:
            # the hook is a no-op when RAG never booted.
            await shutdown_rag()
        await shutdown_docker_pool()  # destroy warm sandbox containers
        # 会话级沙箱池：关闭前销毁全部常驻 VM，避免孤儿（平台 TTL 兜底）。
        try:
            await runs.shutdown_pool()
        except Exception:  # noqa: BLE001 - teardown must not block shutdown
            log.debug("sandbox pool shutdown failed", exc_info=True)

    app = FastAPI(title="pi-py server", version="0.1.0", docs_url="/docs", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.vector_store = vector_store
    app.state.rag = rag_runtime
    app.state.metrics = metrics

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        response.headers["X-Request-Id"] = request_id
        # RED counters on the route PATTERN (bounded label set), not the raw
        # path - /v1/sessions/{id}/runs would otherwise grow labels unboundedly.
        # Duration is TTFB semantics: an SSE run streams after this point.
        route = ""
        r = request.scope.get("route")
        if r is not None:
            route = str(getattr(r, "path", "") or "")
        try:
            metrics.http_request(
                method=request.method,
                # "unmatched" keeps the label set bounded even under 404 scans
                route=route or "unmatched",
                status=response.status_code,
                duration_s=duration,
            )
        except Exception:  # noqa: BLE001 - metrics must never fail a request
            log.debug("http metrics recording failed", exc_info=True)
        log.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 1),
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

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        """Prometheus text format 0.0.4 for scraping. Out of the OpenAPI doc on
        purpose: a scraper reads the exposition format, not a schema.

        Every series is aggregate - no username, session or prompt is a label -
        but together they do describe traffic volume and model usage, so
        PI_METRICS_TOKEN exists for a published port. A wrong token answers
        404, not 403: "forbidden" tells a scanner the endpoint is there.
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
        if rag_runtime is not None:
            # Informational, exactly like the memory pipeline's milvus probe
            # above - but a different index: RAG owns its own collection, so a
            # healthy memory vector store says nothing about document retrieval.
            # The value set is closed to {"ok", "degraded"} because the check
            # below treats anything else as not-ready (pi.rag.integration.health).
            checks["rag"] = await rag_health(rag_runtime)
        # Sandbox is a HARD check, not "degraded": with the platform down every
        # code-execution turn fails, so this must flip the status to 503 or no
        # probe/monitor/load-balancer can tell the difference. Blocking I/O
        # (HTTP + DNS) runs in a thread - house precedent (PBKDF2 in app.py).
        try:
            checks["sandbox"] = await asyncio.to_thread(sandbox_health, settings.sandbox)
        except Exception as exc:  # noqa: BLE001
            checks["sandbox"] = f"unavailable: {exc.__class__.__name__}"
        healthy = all(v in ("ok", "degraded") for v in checks.values())
        status = 200 if healthy else 503
        return JSONResponse({"status": "ready" if healthy else "not-ready", "checks": checks}, status_code=status)

    @app.get("/v1/auth/register_policy")
    async def register_policy() -> dict:
        """Public: tells the frontend whether to show the register tab."""
        return {"allowed": settings.allow_register}

    @app.get("/v1/models")
    async def list_models() -> dict:
        """Public: available chat models for the frontend selector."""
        if settings.model_list:
            models = [m.strip() for m in settings.model_list.split(",") if m.strip()]
        else:
            models = [settings.default_model] if settings.default_model else []
        return {"models": models, "default": settings.default_model}

    @app.post("/v1/auth/register")
    async def register(body: RegisterIn, request: Request) -> dict:
        """Open signup, always a normal user; admin is granted by editing the DB.

        Unauthenticated by design, so 8300 must not be reachable from the internet.
        Gateable via PI_ALLOW_REGISTER=0 (403) when a demo must face the internet.
        """
        if not settings.allow_register:
            raise HTTPException(status_code=403, detail="registration disabled")
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
        row = await sessions.create(user.id, body.title, model, settings.workspace_root)
        Path(row.cwd).mkdir(parents=True, exist_ok=True)
        return {"id": row.id, "title": row.title, "model": row.model, "cwd": row.cwd}

    # ---- 文件管线：MinIO 预签名直连（服务器只签发+记账，不搬字节）---------

    @app.post("/v1/files")
    async def start_upload(body: FileUploadIn, username: str = Depends(current_user)) -> dict:
        """Initiate a direct upload: dedup on (user_id, sha256), then sign a
        presigned PUT URL. The client PUTs bytes straight to MinIO."""
        if not store.enabled:
            raise HTTPException(status_code=503, detail="object storage not configured")
        if body.size > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"file size {body.size} exceeds limit {settings.max_upload_bytes}",
            )
        user = await users.by_username(username)
        # 去重快速路径：同用户同内容已存在 → 复用，连上传都省了
        existing = await files_repo.by_sha(user.id, body.sha256)
        if existing is not None:
            url = await store.presign_get(existing.object_key, existing.bucket)
            return {"id": existing.id, "deduplicated": True, "url": url, "filename": existing.filename}
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", body.filename)[:200]
        object_key = f"{user.id}/{time.strftime('%Y-%m')}/{uuid.uuid4().hex[:12]}-{safe_name}"
        upload_url = await store.presign_put(object_key, settings.s3_bucket_files)
        return {"object_key": object_key, "upload_url": upload_url, "deduplicated": False}

    @app.post("/v1/files/commit")
    async def commit_upload(body: FileCommitIn, username: str = Depends(current_user)) -> dict:
        """Confirm the direct PUT landed: HEAD the object, dedup (final), record."""
        if not store.enabled:
            raise HTTPException(status_code=503, detail="object storage not configured")
        user = await users.by_username(username)
        if not body.object_key.startswith(f"{user.id}/"):
            raise HTTPException(status_code=404, detail="object not found")
        # 最终去重防线（并发或客户端跳过 presign 去重时兜底）
        existing = await files_repo.by_sha(user.id, body.sha256)
        if existing is not None:
            await store.delete(body.object_key, settings.s3_bucket_files)  # 丢弃冗余对象
            url = await store.presign_get(existing.object_key, existing.bucket)
            return {"id": existing.id, "deduplicated": True, "url": url, "filename": existing.filename}
        head = await store.head(body.object_key, settings.s3_bucket_files)
        if head is None:
            raise HTTPException(status_code=404, detail="object not uploaded (PUT the upload_url first)")
        size, ctype = head
        row = await files_repo.create(
            user_id=user.id,
            object_key=body.object_key,
            bucket=settings.s3_bucket_files,
            filename=body.filename,
            size=size,
            content_type=body.content_type or ctype,
            sha256=body.sha256,
        )
        url = await store.presign_get(row.object_key, row.bucket)
        return {"id": row.id, "filename": row.filename, "size": row.size, "url": url}

    @app.get("/v1/files")
    async def list_files(username: str = Depends(current_user)) -> dict:
        user = await users.by_username(username)
        rows = await files_repo.list_for_user(user.id)
        return {
            "files": [
                {
                    "id": r.id,
                    "filename": r.filename,
                    "size": r.size,
                    "content_type": r.content_type,
                    "sha256": r.sha256,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        }

    @app.get("/v1/files/{file_id}/url")
    async def file_url(file_id: int, username: str = Depends(current_user)) -> dict:
        if not store.enabled:
            raise HTTPException(status_code=503, detail="object storage not configured")
        user = await users.by_username(username)
        row = await files_repo.by_id(file_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(status_code=404, detail="file not found")
        url = await store.presign_get(row.object_key, row.bucket)
        return {"id": row.id, "filename": row.filename, "url": url}

    @app.delete("/v1/files/{file_id}")
    async def delete_file(file_id: int, username: str = Depends(current_user)) -> dict:
        user = await users.by_username(username)
        row = await files_repo.by_id(file_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(status_code=404, detail="file not found")
        if store.enabled:
            await store.delete(row.object_key, row.bucket)
        await files_repo.remove(file_id)
        return {"deleted": file_id}

    # ---- 会话工作区产物（模型写出来的文件）--------------------------------
    # 与上面 /v1/files 是两条**不同的存储**，别混：
    #   /v1/files         用户上传 → 对象存储（MinIO），需要 PI_S3_* 才启用
    #   /v1/sessions/*/files  模型产出 → 会话的 workspace 目录，零依赖
    # 缺口背景：模型 write 出来的文件原本**没有任何用户可见的出口**——前端「文件」
    # 面板走的是对象存储那条（未配置时是空的），于是用户看到 "Wrote N chars to
    # <path>" 却拿不到文件。这条通道就是补这个缺口。
    _WS_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"}
    _WS_MAX_FILES = 500

    @app.get("/v1/sessions/{session_id}/files")
    async def list_session_files(session_id: str, username: str = Depends(current_user)) -> dict:
        """列出该会话工作区里的文件（模型生成的产物）。ACL：仅属主可见。"""
        row = await _owned_session(session_id, username)
        base = Path(row.cwd).resolve()
        files: list[dict] = []
        if base.is_dir():
            for p in base.rglob("*"):
                # 跳过符号链接（可指向工作区之外）与非普通文件
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.relative_to(base)
                if _WS_SKIP_DIRS & set(rel.parts[:-1]):
                    continue
                try:
                    st = p.stat()
                except OSError:  # 竞态：列表期间被删
                    continue
                files.append({"path": str(rel), "size": st.st_size, "mtime": int(st.st_mtime)})
        files.sort(key=lambda f: -f["mtime"])  # 最新的在前（用户通常要刚产出的那个）
        return {
            "files": files[:_WS_MAX_FILES],
            "truncated": len(files) > _WS_MAX_FILES,
            "cwd": str(base),
        }

    @app.get("/v1/sessions/{session_id}/files/{path:path}")
    async def download_session_file(
        session_id: str, path: str, username: str = Depends(current_user)
    ) -> FileResponse:
        """下载会话工作区里的单个文件。"""
        row = await _owned_session(session_id, username)
        base = Path(row.cwd).resolve()
        target = (base / path).resolve()
        # 路径沙箱：解析（含符号链接展开）后必须仍落在工作区内 —— 与 policy 的
        # path_sandbox 同一条纪律，`../` 与 symlink 逃逸一并挡住。缺失这层的话，
        # 一个带 `..` 的请求就能读走服务器上任意文件。
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(
                status_code=403, detail="path escapes the session workspace"
            ) from None
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        # FileResponse 流式发送（不把整个文件读进内存），并带 Content-Disposition
        # 让浏览器按原名下载。
        return FileResponse(
            target, filename=target.name, media_type="application/octet-stream"
        )

    # ---- 企业知识库文档管理（上传 → 异步入库 → 列表/删除）----------------
    # 主流的"上传即入库"体验：POST 立刻返回 pending，后台线程跑解析→切块→
    # embedding→索引，状态写回 rag_docs.status；前端轮询 GET /v1/rag/docs。
    _rag_jobs: set[tuple[int, str]] = set()
    # Ingest jobs are I/O-bound but unbounded concurrency would burst the
    # external OCR/embedding APIs; uploads beyond the cap get 429 up-front.
    _rag_ingest_slots = asyncio.Semaphore(settings.rag_max_concurrent_ingests)

    async def _rag_ingest_job(user_id: int, doc_key: str, tmpdir: str) -> None:
        try:
            async with _rag_ingest_slots:
                await rag_runtime.ingest.ingest_file(
                    Path(tmpdir) / doc_key,
                    user_id=user_id,
                    doc_key=doc_key,
                    source=doc_key,  # 引用里显示原始文件名，而非临时路径
                )
        except Exception:  # noqa: BLE001 - 入库失败要能查，不能静默丢
            log.exception("rag ingest failed user=%s doc=%s", user_id, doc_key)
        finally:
            _rag_jobs.discard((user_id, doc_key))
            shutil.rmtree(tmpdir, ignore_errors=True)

    @app.post("/v1/rag/ingest")
    async def rag_ingest_doc(
        file: UploadFile = File(...), username: str = Depends(current_user)
    ) -> dict:
        """上传一个文档并**异步**入库到企业知识库（解析→切块→embedding→索引）。

        ``doc_key`` = 消毒后的文件名；同名重复上传 = 幂等重入库（旧 chunks/向量先清）。
        返回 ``status=pending``；真正处理在后台进行，前端轮询 ``GET /v1/rag/docs``
        看它变成 ``ready`` / ``failed``。
        """
        if rag_runtime is None:
            raise HTTPException(status_code=503, detail="RAG is disabled (PI_RAG_ENABLED=0)")
        if _rag_ingest_slots.locked():
            raise HTTPException(
                status_code=429,
                detail=(
                    f"too many concurrent ingests "
                    f"(max {settings.rag_max_concurrent_ingests}); retry shortly"
                ),
            )
        user = await users.by_username(username)
        filename = (file.filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
        doc_key = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]", "_", filename)[:200] or "document"
        # Claim the slot BEFORE any await so two same-name uploads cannot both
        # pass the 409 check (the add must stay await-free until the claim).
        if (user.id, doc_key) in _rag_jobs:
            raise HTTPException(status_code=409, detail="该文档正在入库，稍后再试")
        _rag_jobs.add((user.id, doc_key))
        tmpdir = tempfile.mkdtemp(prefix="pi-rag-upload-")
        try:
            total = 0
            with (Path(tmpdir) / doc_key).open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings.rag_max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"document too large (max "
                                f"{settings.rag_max_upload_bytes} bytes)"
                            ),
                        )
                    fh.write(chunk)
        except BaseException:
            # Upload failed (client abort / oversize): release the claim and
            # the temp dir; nothing was enqueued.
            _rag_jobs.discard((user.id, doc_key))
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        asyncio.create_task(_rag_ingest_job(user.id, doc_key, tmpdir))
        return {"doc_key": doc_key, "status": "pending"}

    @app.get("/v1/rag/docs")
    async def rag_list_docs(username: str = Depends(current_user)) -> dict:
        if rag_runtime is None:
            raise HTTPException(status_code=503, detail="RAG is disabled (PI_RAG_ENABLED=0)")
        user = await users.by_username(username)
        return {"docs": await rag_runtime.store.list_docs(user.id)}

    @app.delete("/v1/rag/docs/{doc_key:path}")
    async def rag_delete_doc(doc_key: str, username: str = Depends(current_user)) -> dict:
        if rag_runtime is None:
            raise HTTPException(status_code=503, detail="RAG is disabled (PI_RAG_ENABLED=0)")
        user = await users.by_username(username)
        doc = await rag_runtime.store.get_doc(user.id, doc_key)
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        # 先清向量投影，再删 SQL 真相源；最后失效词法分片（user 级）
        if rag_runtime.vector_store is not None:
            await rag_runtime.vector_store.delete_by_doc(user.id, doc_key)
        chunks = await rag_runtime.store.delete_doc(user.id, doc_key)
        await rag_runtime.lexical.invalidate(user.id)
        return {"doc_key": doc_key, "deleted_chunks": chunks}

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

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str, username: str = Depends(current_user)) -> dict:
        """Delete a session (messages + compactions). Trajectory jsonl and usage
        records remain as append-only history. Cross-user is 404, like reads."""
        user = await users.by_username(username)
        if not await sessions.delete_for_user(user.id, session_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {"deleted": session_id}

    @app.get("/v1/sessions/{session_id}/messages")
    async def get_messages(session_id: str, username: str = Depends(current_user)) -> dict:
        await _owned_session(session_id, username)
        rows = await messages.list_for_session(session_id)

        def _blocks(raw: str) -> list:
            # messages.blocks stores the whole Message JSON ({"role":..,"blocks":[..]});
            # the API contract is that "blocks" IS the array. Defensive against
            # rows written as a bare array too.
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = parsed.get("blocks", [])
            return parsed if isinstance(parsed, list) else []

        return {
            "messages": [
                {"idx": r.idx, "role": r.role, "blocks": _blocks(r.blocks)} for r in rows
            ]
        }

    @app.get("/v1/sessions/{session_id}/trajectory")
    async def get_trajectory(session_id: str, username: str = Depends(current_user)) -> dict:
        """Latest stored run of this session. DB (runs table) first, then the
        jsonl fallback for rows written before the table existed. Ownership
        check first - a 404 leaks nothing."""
        await _owned_session(session_id, username)
        row = await runs_repo.latest_for_session(session_id)
        if row is not None:
            return json.loads(row.trajectory)
        if settings.trajectory_path is None:  # PI_TRAJECTORY_PATH="" disables storage
            raise HTTPException(status_code=404, detail="no trajectory for this session")
        traj = latest_trajectory(settings.trajectory_path, session_id)
        if traj is None:
            raise HTTPException(status_code=404, detail="no trajectory for this session")
        return traj

    @app.get("/v1/trajectory/{run_id}")
    async def get_trajectory_by_run(run_id: str, username: str = Depends(current_user)) -> dict:
        """Replay entry point: fetch one run by id (ownership-checked, 404 cross-user)."""
        user = await users.by_username(username)
        row = await runs_repo.by_id(run_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(status_code=404, detail="run not found")
        return json.loads(row.trajectory)

    @app.get("/v1/admin/trajectory/{run_id}")
    async def admin_trajectory(run_id: str, admin: str = Depends(require_admin)) -> dict:
        """Admin replay: fetch any user's run by id (console/diagnosis)."""
        row = await runs_repo.by_id(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return json.loads(row.trajectory)

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
                run_repo=runs_repo,
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
        limit: int = 100,
        day: str | None = None,
        event: str | None = None,
    ) -> dict:
        """Structured audit query: DB (audit_events) first, jsonl fallback for
        records written before the table existed."""
        if day is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(status_code=400, detail="day must look like 2026-09-27")
        from datetime import datetime, timezone as tz

        day = day or datetime.now(tz.utc).strftime("%Y-%m-%d")
        records = await audit_repo.query(day=day, user=user, tool=tool, event=event, limit=limit)
        # 合并 jsonl 里"只有文件副本"的记录（0006 之前的旧数据、或非 app 直写
        # AuditLogger 的记录）；双写产生的重叠按 (ts,event,user,tool) 去重。
        import json as json_mod

        from pi.security.audit import _daily_path

        path = _daily_path(settings.audit_path, day)
        if path.is_file():

            def key(rec: dict) -> tuple:
                return (
                    rec.get("ts"),
                    rec.get("event"),
                    rec.get("user") or rec.get("username"),
                    rec.get("tool") or rec.get("action"),
                )

            seen = {key(r) for r in records}
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    rec = json_mod.loads(line)
                except json_mod.JSONDecodeError:
                    continue
                if user and rec.get("user") != user and rec.get("username") != user:
                    continue
                if tool and rec.get("tool") != tool:
                    continue
                if event and rec.get("event") != event:
                    continue
                if key(rec) not in seen:
                    seen.add(key(rec))
                    records.append(rec)
                    if len(records) >= min(limit, 500):
                        break
            records.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return {"records": records[: min(limit, 500)]}

    @app.get("/v1/admin/stats")
    async def admin_stats(admin: str = Depends(require_admin)) -> dict:
        """Console overview: today's usage aggregates + fleet sizes."""
        return {
            "today": await usage_tracker.today_summary(),
            "users": await users.count(),
            "sessions": await sessions.count(),
        }

    @app.get("/v1/admin/usage")
    async def admin_usage(admin: str = Depends(require_admin)) -> dict:
        """Console users tab: this month's aggregate per user."""
        return {"users": await usage_tracker.monthly_by_user()}

    @app.get("/v1/usage")
    async def usage_summary(username: str = Depends(current_user)) -> dict:
        user = await users.by_username(username)
        summary = await usage_tracker.monthly_summary(username)
        quota = await usage_tracker.quota_check(user.id, user.quota_tokens)
        summary["quota_tokens"] = quota.quota_tokens
        summary["used_tokens"] = quota.used_tokens
        return summary

    # Zero-build frontend: one static dir of single-file pages (trajectory
    # viewer first, TRAJECTORY_VIEW_DESIGN). Mounted last so it never shadows
    # an API route. 404s and auth are handled inside the pages themselves.
    # HTML is served no-cache: these pages change often and a stale cached copy
    # has repeatedly looked like "the fix isn't there".
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        from starlette.staticfiles import StaticFiles

        class _NoCacheStatic(StaticFiles):
            async def get_response(self, path: str, scope):
                response = await super().get_response(path, scope)
                if path.endswith(".html"):
                    response.headers["Cache-Control"] = "no-cache"
                return response

        app.mount("/ui", _NoCacheStatic(directory=static_dir), name="ui")

    return app
