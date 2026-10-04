"""pi integration for the RAG kernel - THE ONLY file allowed to import pi.*.

Portability contract (see ``pi/rag/__init__.py``): everything under ``pi.rag``
talks through the six Protocols in ``protocols.py`` and the backends in
``defaults/``. This module is the single seam where the kernel meets the rest of
pi - the server's ``EmbeddingClient``, the ``Database`` engine, and the two
metering callbacks that make RAG spend hit the user quota. Lift ``pi/rag`` into
another project and you delete this file plus ``pi/tools/rag.py``; nothing else
changes.

What lives here, and why:

``EmbeddingClientAdapter``
    ``pi.llm.embedding.EmbeddingClient`` only has ``embed(texts)``. The kernel's
    ``Embedder`` Protocol also wants ``embed_query(text)`` so an asymmetric model
    (query vs passage prefixes) can be served. This adapter supplies the default
    ``embed([text])`` implementation and converts ``EmbeddingResult`` ->
    ``pi.rag.types.EmbedResult`` (same shape, different module, so the kernel
    never imports pi).

    Note: ``defaults.HttpEmbedder`` is the better production choice when the
    endpoint is OpenAI-compatible (it auto-detects the wire style and batches).
    This adapter exists for deployments that want to REUSE the already-wired
    server client rather than open a second connection pool.

``ServerUsageHooks``
    Bridges the kernel's ``UsageHooks`` (``on_embed_usage(user_id, tokens,
    kind)`` / ``on_retrieval(outcome, duration_s)``) onto the two callbacks
    ``pi.server.app`` already builds for ``MemoryRepo``. The ``kind`` argument
    ('embedding' | 'rerank') is what lets rerank spend be metered under its own
    model tag instead of being silently folded into embedding cost. Two-arg
    callbacks (the existing MemoryRepo shape) are detected and supported, so
    wiring RAG never forces a change to memory's call sites.

``build_runtime`` / ``get_runtime``
    Assembly: env config -> concrete backends -> ``HybridRetriever`` +
    ``IngestPipeline`` sharing ONE lexical index (ingest must invalidate the
    retriever's cached BM25 shard, or re-ingest leaves stale chunk_ids that
    hydrate the WRONG chunk). ``get_runtime`` is the process-level lazy
    singleton used by ``RagTool`` and the CLI.

Fail-safe, not fail-silent (对接文档 §9 #4): unset config means the capability is
OFF (vector channel unconfigured -> retrieval degrades to BM25 with a logged
warning), never a crash. Misconfiguration that WOULD break the main path is
raised at assembly time instead.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pi.llm.embedding import EmbeddingClient, EmbeddingResult
from pi.rag.config import RagConfig
from pi.rag.defaults.bm25 import MemoryBM25Index
from pi.rag.defaults.http_embedder import HttpEmbedder
from pi.rag.defaults.http_reranker import HttpReranker, NoopHooks
from pi.rag.defaults.memory_vector import InMemoryVectorStore
from pi.rag.defaults.mysql_store import MysqlChunkStore
from pi.rag.defaults.sqlite_store import SqliteChunkStore
from pi.rag.ingest import IngestPipeline
from pi.rag.protocols import ChunkStore, Embedder, HeavyParser, RagVectorStore, Reranker, UsageHooks
from pi.rag.retriever import HybridRetriever
from pi.rag.types import EmbedResult

log = logging.getLogger("pi.rag.adapters")

__all__ = [
    "EmbeddingClientAdapter",
    "ServerUsageHooks",
    "RagRuntime",
    "build_runtime",
    "get_runtime",
    "set_runtime",
    "reset_runtime",
]


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


class EmbeddingClientAdapter:
    """Adapt ``pi.llm.embedding.EmbeddingClient`` to the kernel's ``Embedder``.

    Failures propagate as ``EmbeddingError`` - the retriever catches them and
    degrades to BM25 (never raises into a run).
    """

    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client

    @staticmethod
    def _convert(res: EmbeddingResult) -> EmbedResult:
        return EmbedResult(
            vectors=[list(v) for v in (res.vectors or [])],
            usage_tokens=int(res.usage_tokens or 0),
        )

    async def embed(self, texts: list[str]) -> EmbedResult:
        if not texts:
            return EmbedResult()
        return self._convert(await self._client.embed(list(texts)))

    async def embed_query(self, text: str) -> EmbedResult:
        """Symmetric default. Override in a subclass if the endpoint needs a
        distinct query-side prefix (some embedding models are trained that way).
        """
        return await self.embed([text])


# ---------------------------------------------------------------------------
# Usage hooks
# ---------------------------------------------------------------------------


class ServerUsageHooks:
    """Kernel ``UsageHooks`` -> the server's two metering callbacks.

    ``on_embed_usage`` may take either ``(user_id, tokens)`` (the MemoryRepo
    shape already in ``pi.server.app``) or ``(user_id, tokens, kind)``. The arity
    is inspected ONCE at construction so a 3-arg wiring can bill rerank tokens
    under their own model tag, while existing 2-arg call sites keep working
    unchanged.

    Hook bodies must never raise into the retrieval path; the retriever already
    wraps every call, and this class swallows too (defence in depth - a metrics
    outage must not turn into a retrieval outage).
    """

    def __init__(
        self,
        on_embed_usage: Callable[..., Awaitable[None]] | None = None,
        on_retrieval: Callable[[str, float], Awaitable[None]] | None = None,
    ) -> None:
        self._embed = on_embed_usage
        self._retrieval = on_retrieval
        self._embed_wants_kind = False
        if on_embed_usage is not None:
            try:
                sig = inspect.signature(on_embed_usage)
                # 3+ accepted params (or a **kwargs sink) -> pass kind through.
                positional = [
                    p
                    for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                self._embed_wants_kind = len(positional) >= 3 or any(
                    p.kind is p.VAR_KEYWORD for p in sig.parameters.values()
                )
            except (TypeError, ValueError):  # builtins/C callables: stay 2-arg
                self._embed_wants_kind = False

    async def on_embed_usage(self, user_id: int, tokens: int, kind: str = "embedding") -> None:
        if self._embed is None or not tokens:
            return
        try:
            if self._embed_wants_kind:
                await self._embed(user_id, tokens, kind)
            else:
                await self._embed(user_id, tokens)
        except Exception:  # noqa: BLE001 - metering failure must not break retrieval
            log.warning("rag on_embed_usage hook failed (user=%s kind=%s)", user_id, kind,
                        exc_info=True)

    async def on_retrieval(self, outcome: str, duration_s: float) -> None:
        if self._retrieval is None:
            return
        try:
            await self._retrieval(outcome, duration_s)
        except Exception:  # noqa: BLE001
            log.warning("rag on_retrieval hook failed (outcome=%s)", outcome, exc_info=True)


# ---------------------------------------------------------------------------
# Runtime assembly
# ---------------------------------------------------------------------------


@dataclass
class RagRuntime:
    """The assembled kernel: one retriever + one ingest pipeline, sharing parts.

    ``lexical`` is deliberately shared between the two: ingest calls
    ``lexical.invalidate(user_id)`` after a re-ingest, because delete-then-insert
    reassigns chunk_ids and a stale BM25 shard would hydrate the WRONG chunk.
    """

    config: RagConfig
    store: ChunkStore
    retriever: HybridRetriever
    ingest: IngestPipeline
    lexical: MemoryBM25Index
    hooks: UsageHooks
    vector_store: RagVectorStore | None = None
    embedder: Embedder | None = None
    reranker: Reranker | None = None
    backend: str = ""  # "mysql" | "sqlite" - surfaced by the CLI for audit

    async def close(self) -> None:
        """Release backends. Safe to call twice; never raises."""
        # embedder/reranker hold pooled HTTP connections (R8) - close them
        # too, or the process leaks sockets on every runtime rebuild.
        for obj in (self.embedder, self.reranker, self.vector_store, self.store):
            closer = getattr(obj, "aclose", None) or getattr(obj, "close", None) \
                or getattr(obj, "dispose", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001 - teardown must not block shutdown
                    log.debug("rag runtime close failed for %s", type(obj).__name__, exc_info=True)


def _make_store(config: RagConfig, db: Any, *, create_schema: bool) -> tuple[ChunkStore, str]:
    """SQL source of truth. Three cases, in priority order:

    1. ``db`` injected (pi server) -> share its AsyncEngine, so RAG lives inside
       the same pool / connection limits as the rest of the app. Alembic owns
       that schema (migration 0008_rag, which chains after upstream 0007_files),
       so DDL is NOT re-run here.
    2. ``PI_DATABASE_URL`` set (CLI against real infra) -> own engine, own
       idempotent DDL, because no alembic run is implied by a CLI invocation.
    3. neither -> SQLite at ``config.sqlite_path`` (zero-config standalone).

    Dialect is decided by the URL prefix exactly like
    ``pi.server.db.engine_kwargs`` - no second dialect-detection scheme.
    """
    engine = getattr(db, "engine", None)
    if engine is not None:
        # create_schema=False when the caller says alembic already ran; the
        # store still tolerates True (the DDL is IF NOT EXISTS).
        return MysqlChunkStore(engine, create_schema=create_schema), "mysql"

    import os

    db_url = (os.environ.get("PI_RAG_DATABASE_URL") or os.environ.get("PI_DATABASE_URL") or "").strip()
    if db_url.startswith("mysql"):
        return MysqlChunkStore(db_url, create_schema=create_schema), "mysql"
    if db_url:
        log.warning("PI_DATABASE_URL=%s is not a mysql:// URL; falling back to SQLite",
                    db_url.split("://", 1)[0] + "://")
    return SqliteChunkStore(config.sqlite_path), "sqlite"


def build_runtime(
    config: RagConfig | None = None,
    *,
    db: Any = None,
    hooks: UsageHooks | None = None,
    embedder: Embedder | None = None,
    allow_memory_vector: bool = False,
    create_schema: bool = True,
) -> RagRuntime:
    """Assemble a runtime from config + injected pi objects.

    Parameters mirror the fail-safe rule: everything is optional, and an absent
    capability degrades instead of crashing.

    ``db``
        A ``pi.server.db.Database`` (or anything exposing ``.engine``). Its
        AsyncEngine is shared with ``MysqlChunkStore``. None -> standalone.
    ``hooks``
        Metering. None -> ``NoopHooks`` (standalone: count, don't bill).
    ``embedder``
        Override the embedding path, e.g.
        ``EmbeddingClientAdapter(server_embedding_client)`` to reuse the
        already-wired client. None -> ``HttpEmbedder`` when the env is set.
    ``allow_memory_vector``
        Permit the in-process vector store when ``PI_RAG_MILVUS_URI`` is unset.
        True for the CLI/tests; **False for the server**, where an in-process
        index would look healthy while returning nothing across workers.
    """
    cfg = config or RagConfig.from_env()
    # create_schema stays True even when db is injected (server mode): the DDL is
    # idempotent (CREATE TABLE IF NOT EXISTS), so a fresh DB where alembic 0008_rag
    # never ran self-heals on first use instead of silently serving 500s.
    store, backend = _make_store(cfg, db, create_schema=create_schema)

    if hooks is None:
        hooks = NoopHooks()

    if embedder is None and cfg.vector_enabled():
        embedder = HttpEmbedder(
            cfg.embedding.url,
            cfg.embedding.api_key,
            cfg.embedding.model,
            timeout=cfg.embedding.timeout_s,
            batch_size=cfg.embedding.batch_size,
            retries=cfg.embedding.retries,
            retry_backoff_s=cfg.embedding.retry_backoff_s,
        )
    if embedder is not None and not cfg.vector_enabled():
        # An injected embedder with no endpoint config is a wiring bug, not a
        # degradation: refuse loudly rather than silently never calling it.
        log.warning("embedder injected but PI_*EMBEDDING_* config is incomplete; "
                    "vector channel will still be attempted")

    vector_store: RagVectorStore | None = None
    if cfg.milvus_uri:
        from pi.rag.defaults.milvus_vector import MilvusRagVectorStore  # lazy: pymilvus

        vector_store = MilvusRagVectorStore(
            cfg.milvus_uri, cfg.collection, consistency=cfg.milvus_consistency
        )
        if cfg.milvus_consistency != "Strong":
            # Loud on purpose: this trades post-ingest visibility for latency
            # (~399ms -> ~4ms per search). An operator who set it by accident,
            # or who then wonders why a brand-new doc is briefly unsearchable,
            # needs this line in the log.
            log.warning(
                "rag Milvus consistency=%s (not Strong): a just-ingested chunk may "
                "be invisible for a short window; search latency drops ~100x",
                cfg.milvus_consistency,
            )
    elif allow_memory_vector:
        vector_store = InMemoryVectorStore()
        log.info("rag vector store: in-memory (PI_RAG_MILVUS_URI unset)")
    else:
        log.info("rag vector channel disabled (PI_RAG_MILVUS_URI unset)")

    # A vector store without an embedder cannot serve queries; keep it for the
    # ingest projection path but the retriever's channel check needs BOTH.
    reranker: Reranker | None = None
    if cfg.rerank_enabled():
        reranker = HttpReranker(
            cfg.rerank_url,
            cfg.rerank_api_key,
            cfg.rerank_model,
            timeout=cfg.rerank_timeout_s,
            retries=cfg.rerank_retries,
            retry_backoff_s=cfg.embedding.retry_backoff_s,
        )
    elif cfg.retrieval.rerank_enabled:
        # P5: rerank is ON by default and material to answer quality, but it
        # silently does nothing without a RERANK_URL. On the real corpora the
        # cross-encoder was THE thing that fixed retrieval (recall@5 0.72 ->
        # 0.895 on both v1/v2). Shipping with it half-configured means the
        # deployment answers materially worse than the eval baseline with no
        # error anywhere - exactly the silent degradation the house rule bans.
        log.warning(
            "rag rerank is enabled but PI_RAG_RERANK_URL is unset - retrieval will "
            "serve fused (pre-rerank) order and answer materially below the eval "
            "baseline (recall@5 ~0.72 vs ~0.895 with rerank). Set PI_RAG_RERANK_URL "
            "(+ PI_RAG_RERANK_API_KEY / PI_RAG_RERANK_MODEL), or set "
            "PI_RAG_RERANK_ENABLED=0 if this deployment deliberately runs without "
            "rerank."
        )

    # ttl_s: bound how long a worker may answer from a shard built before
    # another worker's `rag ingest`. invalidate() cannot reach other processes,
    # and unlike the vector channel (Milvus = shared state) a stale shard here
    # means silently wrong answers. Rebuilding reads SQL, i.e. the truth, so
    # this is a freshness bound - never a behaviour change for correct shards.
    lexical = MemoryBM25Index(store, ttl_s=cfg.retrieval.bm25_ttl_s)

    retriever = HybridRetriever(
        store,
        embedder=embedder,
        vector_store=vector_store,
        lexical_index=lexical,
        reranker=reranker,
        config=cfg,
        hooks=hooks,
    )
    # External OCR service for scans/images (optional; None = v1 behaviour:
    # flag needs_heavy_parser, don't OCR).
    heavy_parser: HeavyParser | None = None
    if cfg.heavy_parser:
        from pi.rag.heavy import build_heavy_parser  # lazy: pulls in httpx

        heavy_parser = build_heavy_parser(cfg)

    ingest = IngestPipeline(
        store,
        embedder,
        vector_store,
        config=cfg,
        hooks=hooks,
        lexical_index=lexical,
        heavy_parser=heavy_parser,
    )
    return RagRuntime(
        config=cfg,
        store=store,
        retriever=retriever,
        ingest=ingest,
        lexical=lexical,
        hooks=hooks,
        vector_store=vector_store,
        embedder=embedder,
        reranker=reranker,
        backend=backend,
    )


# -- process-level singleton ------------------------------------------------

_runtime: RagRuntime | None = None
_runtime_lock = asyncio.Lock()


async def get_runtime(db: Any = None) -> RagRuntime:
    """Lazy process-level runtime, shared by RagTool and the CLI.

    Built on first use (never at import, so ``create_app`` stays fast and
    importable without infra). Double-checked under a lock: two concurrent first
    queries must not build two runtimes and leak a connection pool.
    """
    global _runtime
    if _runtime is not None:
        return _runtime
    async with _runtime_lock:
        if _runtime is None:
            _runtime = build_runtime(db=db)
            log.info(
                "rag runtime ready (store=%s vector=%s rerank=%s)",
                _runtime.backend,
                type(_runtime.vector_store).__name__ if _runtime.vector_store else "off",
                type(_runtime.reranker).__name__ if _runtime.reranker else "off",
            )
    return _runtime


def set_runtime(runtime: RagRuntime | None) -> None:
    """Install a runtime explicitly (server lifespan, tests). None clears it."""
    global _runtime
    _runtime = runtime


async def reset_runtime() -> None:
    """Close and clear the singleton (server shutdown / test teardown)."""
    global _runtime
    runtime, _runtime = _runtime, None
    if runtime is not None:
        await runtime.close()
