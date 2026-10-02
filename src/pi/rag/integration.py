"""Server-side integration: everything that couples ``pi.rag`` to the pi server.

This is the ONLY file in ``pi.rag`` allowed to import ``pi.*`` (same rule as
``adapters.py``). It exists so the host tree stays nearly untouched:

  * ``pi/tools/__init__.py``  <- NOT edited: rag_search arrives as its own
    ToolRegistry *provider* instead of being appended to ``all_tools()``.
  * ``pi/tools/base.py``      <- NOT edited: no ``ToolContext.rag`` field. The
    runtime is published to the process singleton that ``pi.tools.rag`` already
    knows how to fall back on, so no new injection seam is needed.
  * ``pi/server/runner.py``   <- NOT edited: nothing to inject per turn.

Why a provider is *better* than appending to ``all_tools()``: ``ToolRegistry``
merges providers, dedupes by name and hands the merged list to AgentLoop - so
``rag_search`` passes through the same policy gate, audit log, tracing and
quota path as any builtin tool (see ``pi.tools.registry`` docstring), and
``PI_RAG_ENABLED=0`` simply contributes no provider instead of requiring an
``enabled`` flag threaded through the tool constructor.

What the host must still do (2 call sites, ~6 lines total):
  1. ``providers.append(RagToolProvider())`` when ``settings.rag_enabled``.
  2. ``rag_runtime = install(...)`` after the usage tracker exists (sync - see
     below), and ``await shutdown_rag()`` in lifespan teardown.
"""

from __future__ import annotations

import logging
from typing import Any

from pi.rag.config import RagConfig
from pi.tools.registry import ToolProvider

log = logging.getLogger("pi.rag.integration")


class RagToolProvider(ToolProvider):
    """Contributes ``rag_search``. Contributes nothing when RAG is off.

    ``tools()`` returns the tool unconditionally: the *provider* is what gets
    omitted (``PI_RAG_ENABLED=0`` -> app.py never appends us), so the tool
    itself needs no enabled flag and no constructor plumbing.
    """

    name = "rag"

    async def tools(self) -> list[Any]:
        # Imported here, not at module scope: keeps ``create_app`` cheap and
        # importable on a host without the optional RAG deps installed.
        from pi.tools.rag import RagTool

        return [RagTool()]


def install(
    *,
    db: Any,
    users: Any,
    usage_tracker: Any,
    metrics: Any,
    allow_memory_vector: bool = False,
) -> Any:
    """Build the RAG runtime, publish it, and return it.

    Sync on purpose: ``create_app`` is sync and ``build_runtime`` does no I/O
    (the Milvus client and the HTTP pools are all lazy).

    Shares the server's engine (``db``) and metering (``usage_tracker``) rather
    than opening a second pool. Every backend inside is lazy - Milvus connects
    on first use, httpx per call - so ``create_app`` stays fast.

    Fail-safe by construction (对接文档 §9 #4): missing embedding/Milvus config
    never raises, it just leaves the vector channel unconfigured so retrieval
    degrades to BM25 with a warning. A RAG outage cannot stop the server booting.
    """
    from pi.rag.adapters import ServerUsageHooks, build_runtime, set_runtime

    cfg = RagConfig.from_env()

    async def on_embed_usage(user_id: int, tokens: int, kind: str = "embedding") -> None:
        """Bill embedding AND rerank spend against the user quota.

        ``kind`` is why this is a 3-arg callback instead of reusing the memory
        one: rerank tokens billed as "embedding/<model>" would make the
        per-model monthly breakdown lie about where the money went.
        """
        row = await users.by_id(user_id)
        if row is None:
            return
        model = (
            cfg.rerank_model if kind == "rerank" and cfg.rerank_model
            else cfg.embedding.model
        )
        await usage_tracker.record(
            user_id=user_id,
            username=row.username,
            session_id="",
            model=f"{'rerank' if kind == 'rerank' else 'embedding'}/{model}",
            input_tokens=tokens,
            output_tokens=0,
            turns=0,
        )

    async def on_retrieval(outcome: str, duration: float) -> None:
        metrics.retrieval(outcome=f"rag_{outcome}", duration_s=duration)

    runtime = build_runtime(
        cfg,
        db=db,
        hooks=ServerUsageHooks(on_embed_usage=on_embed_usage, on_retrieval=on_retrieval),
        # In-process vector fallback stays OFF for the server: under multiple
        # workers an in-memory index looks healthy while returning nothing.
        allow_memory_vector=allow_memory_vector,
    )
    # Published so RagTool finds it without a per-turn ctx injection.
    set_runtime(runtime)
    log.info(
        "rag enabled: store=%s vector=%s rerank=%s lexical_weight=%s",
        runtime.backend,
        type(runtime.vector_store).__name__ if runtime.vector_store else "off",
        type(runtime.reranker).__name__ if runtime.reranker else "off",
        cfg.retrieval.lexical_weight,
    )
    return runtime


async def health(runtime: Any) -> str:
    """RAG's line in ``/readyz``. One word, never raises.

    The value set is a closed ``{"ok", "degraded"}`` on purpose: ``/readyz``
    computes readiness as ``all(v in ("ok", "degraded") ...)``, so anything else
    - including a helpful ``"degraded: TimeoutError"`` - would flip the WHOLE
    server to 503. RAG must not be able to do that: a Milvus outage costs vector
    recall and degrades retrieval to the lexical channel, which is a quality
    regression, not a reason to stop serving.

    ``degraded`` therefore covers both "no vector channel configured" (a valid
    BM25-only deployment) and "Milvus unreachable". To a caller they mean the
    same thing: no vector recall. Which one it is lives in the boot log
    (``install``) rather than in a per-probe string, because ``/readyz`` is
    polled often enough that detailed values become either log spam or a lying
    readiness gate.

    Takes the runtime rather than resolving the singleton: a health probe must
    never be the thing that first constructs the runtime.
    """
    vector = getattr(runtime, "vector_store", None)
    if vector is None:
        return "degraded"
    try:
        return "ok" if await vector.ping() else "degraded"
    except Exception:  # noqa: BLE001 - ping() promises not to raise
        log.debug("rag health probe raised", exc_info=True)
        return "degraded"


async def shutdown_rag() -> None:
    """Close the published runtime AND clear the singleton. Never raises.

    Closing without clearing is a trap: ``get_runtime()`` would keep returning
    the closed runtime (it is not None), so a later ``rag_search`` would reuse a
    disposed engine / closed Milvus client and fail confusingly instead of
    rebuilding. ``reset_runtime()`` is the only correct teardown.

    Safe to call when RAG never booted (nothing published -> no-op), which is
    why the host can call it unconditionally in lifespan teardown without
    tracking whether ``install`` ran.
    """
    from pi.rag.adapters import reset_runtime

    try:
        await reset_runtime()
    except Exception:  # noqa: BLE001 - teardown must not block shutdown
        log.debug("rag runtime shutdown failed", exc_info=True)
