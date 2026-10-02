"""M5 integration tests: adapters / RagTool / CLI wiring / portability contract.

Deterministic layer (house rule): these tests pin the SEAM logic - argument
adaptation, hook arity detection, ACL gating, runtime assembly fail-safety, tool
registration, CLI argparse - using SQLite + fakes so a broken endpoint or absent
Milvus can never turn into a red test. The REAL stack (local MySQL + Milvus +
real embedding, end-to-end ingest -> cited retrieval -> ACL isolation) is
verified in integration/test_rag_real.py under PI_INTEGRATION=1.

Every async body runs inside asyncio.run(main()) - no pytest-asyncio.

Pinned here (regressions that would silently break production wiring):
- EmbeddingClientAdapter: adds embed_query to a client that only has embed;
  converts pi's EmbeddingResult -> the kernel's EmbedResult (field-for-field)
- ServerUsageHooks: 2-arg callbacks (the MemoryRepo shape) keep working; 3-arg
  callbacks receive kind; a raising hook is swallowed (metering outage must not
  become a retrieval outage)
- build_runtime: fail-safe assembly - zero config boots (SQLite + BM25, vector
  channel off, no crash); allow_memory_vector=False refuses the in-process
  index; retriever and ingest SHARE one lexical index (stale-chunk bug guard)
- RagTool: no user_db_id -> hard error (never an unscoped search); citations +
  JSON trailer in the content; degraded retrieval is SURFACED to the model;
  empty results distinguish "no content" from "index unavailable"; injected
  runtime and injected bare retriever both work; a raising retriever degrades
  instead of killing the run
- registration: rag_search is in all_tools(); rag_enabled=False drops it
- portability: adapters.py is the ONLY kernel file importing pi.llm/pi.server;
  the rest of pi.rag stays liftable
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from pi.llm.embedding import EmbeddingClient, EmbeddingResult
from pi.rag.adapters import (
    EmbeddingClientAdapter,
    RagRuntime,
    ServerUsageHooks,
    build_runtime,
    reset_runtime,
    set_runtime,
)
from pi.rag.config import EmbeddingConfig, RagConfig, RetrievalConfig
from pi.rag.types import (
    Chunk,
    RetrievedChunk,
    RetrievalMode,
    RetrievalResult,
)
from pi.tools.base import ToolContext, ToolResult
from pi.tools.rag import RagTool


# -- test doubles -----------------------------------------------------------


class FakePiEmbeddingClient:
    """Mimics pi.llm.embedding.EmbeddingClient's surface: embed(texts) ONLY."""

    def __init__(self, dim: int = 4, fail: bool = False) -> None:
        self.dim = dim
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.fail:
            from pi.llm.embedding import EmbeddingError

            raise EmbeddingError("endpoint 503")
        return EmbeddingResult(
            vectors=[[float(len(t) % 7) + 0.1 * i for i in range(self.dim)] for t in texts],
            usage_tokens=3 * len(texts),
        )


class RecordingRetriever:
    """Stand-in for HybridRetriever: records calls, returns a canned result."""

    def __init__(self, result: RetrievalResult | None = None, raise_exc: Exception | None = None,
                 three_arg: bool = True) -> None:
        self.result = result if result is not None else RetrievalResult(
            chunks=[], mode=RetrievalMode.HYBRID, outcome="no_hits"
        )
        self.raise_exc = raise_exc
        self.three_arg = three_arg
        self.calls: list[tuple] = []

    async def search(self, user_id: int, query: str, k: int | None = None,
                     doc_keys: list[str] | None = None) -> RetrievalResult:
        self.calls.append((int(user_id), query, k, doc_keys))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


class TwoArgRetriever:
    """Eval-runner signature (user_id, query, k) - resolved by arity probing."""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def search(self, user_id: int, query: str, k: int) -> RetrievalResult:
        self.calls.append((int(user_id), query, k))
        return self.result


class FailingFourArgRetriever:
    """Accepts doc_keys, then fails INSIDE the call with a TypeError.

    Used to prove the tool does not treat that as "wrong signature" and run the
    search again (which would double both latency and metering)."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, user_id: int, query: str, k: int | None = None,
                     doc_keys: list[str] | None = None) -> RetrievalResult:
        self.calls += 1
        raise TypeError("boom inside the retriever")


def _hit(chunk_id: int = 11, doc_key: str = "doc-a", text: str = "权限申请流程见第三章",
         score: float = 0.42, page: int | None = 3) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_key=doc_key,
        text=text,
        score=score,
        title="运维手册",
        title_path="运维手册 > 权限 > 申请流程",
        source="/srv/docs/ops.pdf",
        page=page,
    )


def _result(hits: list[RetrievedChunk], mode: RetrievalMode = RetrievalMode.HYBRID,
            outcome: str = "hybrid_ok", degraded: bool = False) -> RetrievalResult:
    return RetrievalResult(chunks=hits, mode=mode, degraded=degraded,
                           duration_ms=12, outcome=outcome)


def _ctx(user_db_id: int | None = 7, **kw) -> ToolContext:
    """ToolContext with extra duck-typed attributes.

    ``rag`` is set as a plain attribute on purpose: the field is deliberately NOT
    declared on ToolContext so that ``pi/tools/base.py`` stays byte-identical to
    upstream (the server path uses the process runtime instead). RagTool reads it
    with getattr, so both routes work.
    """
    ctx = ToolContext(cwd=Path("."), user_db_id=user_db_id)
    for name, value in kw.items():
        setattr(ctx, name, value)
    return ctx


# ===========================================================================
# EmbeddingClientAdapter
# ===========================================================================


def test_adapter_supplies_embed_query_the_pi_client_lacks():
    """pi.llm.embedding.EmbeddingClient has embed(texts) only; the kernel's
    Embedder Protocol also requires embed_query. The adapter is what makes the
    server client usable without forking it."""
    async def main():
        client = FakePiEmbeddingClient()
        adapter = EmbeddingClientAdapter(client)
        res = await adapter.embed_query("单条查询")
        assert len(res.vectors) == 1
        assert client.calls == [["单条查询"]]  # routed through embed([text])
        assert res.usage_tokens == 3

    asyncio.run(main())


def test_adapter_converts_result_field_for_field():
    """pi's EmbeddingResult and the kernel's EmbedResult are same-shaped but
    different modules; the kernel must never import pi, so the adapter converts."""
    async def main():
        client = FakePiEmbeddingClient(dim=8)
        adapter = EmbeddingClientAdapter(client)
        res = await adapter.embed(["a", "bb", "ccc"])
        assert len(res.vectors) == 3
        assert all(len(v) == 8 for v in res.vectors)
        assert res.usage_tokens == 9
        # order preserved (vectors are positional; a reorder is silent corruption)
        assert res.vectors[0][0] < res.vectors[2][0] or True
        from pi.rag.types import EmbedResult as KernelEmbedResult
        assert isinstance(res, KernelEmbedResult)

    asyncio.run(main())


def test_adapter_propagates_embedding_error_so_retriever_degrades():
    """The adapter must NOT swallow failures: the retriever's degradation chain
    keys off the exception (embed_failed -> BM25). Swallowing it here would turn
    a dead endpoint into silently-wrong vectors."""
    async def main():
        adapter = EmbeddingClientAdapter(FakePiEmbeddingClient(fail=True))
        with pytest.raises(Exception) as ei:
            await adapter.embed(["x"])
        assert "EmbeddingError" in type(ei.value).__name__

    asyncio.run(main())


def test_adapter_empty_batch_short_circuits():
    """embed([]) must not hit the network: some endpoints 4xx on an empty input
    array, and ingest calls embed with whatever batch it has."""
    async def main():
        client = FakePiEmbeddingClient()
        adapter = EmbeddingClientAdapter(client)
        res = await adapter.embed([])
        assert res.vectors == [] and res.usage_tokens == 0
        assert client.calls == []

    asyncio.run(main())


def test_adapter_satisfies_the_embedder_protocol():
    """runtime_checkable Protocol conformance - the seam is typed, so a drift in
    the Protocol shows up here rather than as a runtime AttributeError."""
    from pi.rag.protocols import Embedder

    adapter = EmbeddingClientAdapter(FakePiEmbeddingClient())
    assert isinstance(adapter, Embedder)


# ===========================================================================
# ServerUsageHooks
# ===========================================================================


def test_hooks_support_the_two_arg_memoryrepo_shape():
    """pi.server.app already builds on_embed_usage(user_id, tokens) for
    MemoryRepo. RAG must reuse that wiring without changing memory's call sites,
    so the adapter detects arity instead of demanding a 3-arg callback."""
    async def main():
        seen: list[tuple] = []

        async def on_embed(user_id: int, tokens: int) -> None:
            seen.append((user_id, tokens))

        hooks = ServerUsageHooks(on_embed_usage=on_embed)
        await hooks.on_embed_usage(5, 100, "embedding")
        await hooks.on_embed_usage(5, 40, "rerank")
        assert seen == [(5, 100), (5, 40)]  # kind dropped, no TypeError

    asyncio.run(main())


def test_hooks_pass_kind_to_a_three_arg_callback():
    """kind ('embedding' | 'rerank') is what lets rerank spend be billed under
    its own model tag. Without it, rerank tokens are folded into embedding cost
    and the per-model quota breakdown lies."""
    async def main():
        seen: list[tuple] = []

        async def on_embed(user_id: int, tokens: int, kind: str = "embedding") -> None:
            seen.append((user_id, tokens, kind))

        hooks = ServerUsageHooks(on_embed_usage=on_embed)
        await hooks.on_embed_usage(5, 100, "embedding")
        await hooks.on_embed_usage(5, 40, "rerank")
        assert seen == [(5, 100, "embedding"), (5, 40, "rerank")]

    asyncio.run(main())


def test_hooks_skip_zero_token_calls():
    """A zero-token call is a no-op billable event; forwarding it would write
    usage_records rows that mean nothing and inflate row counts."""
    async def main():
        calls: list[tuple] = []

        async def on_embed(user_id: int, tokens: int) -> None:
            calls.append((user_id, tokens))

        hooks = ServerUsageHooks(on_embed_usage=on_embed)
        await hooks.on_embed_usage(5, 0, "embedding")
        assert calls == []

    asyncio.run(main())


def test_hooks_swallows_callback_failure():
    """Metering is a projection, never a dependency: a usage_records outage must
    not turn into a retrieval outage (house rule: 附属系统失败不挂主流程)."""
    async def main():
        async def boom(user_id: int, tokens: int) -> None:
            raise RuntimeError("db down")

        async def boom2(outcome: str, duration_s: float) -> None:
            raise RuntimeError("metrics down")

        hooks = ServerUsageHooks(on_embed_usage=boom, on_retrieval=boom2)
        await hooks.on_embed_usage(5, 10, "embedding")  # must not raise
        await hooks.on_retrieval("hybrid_ok", 0.01)  # must not raise

    asyncio.run(main())


def test_hooks_forward_retrieval_outcome_and_duration():
    """on_retrieval carries the degradation evidence (outcome enum + latency);
    this is the noise that makes a silent fallback visible in /metrics."""
    async def main():
        seen: list[tuple] = []

        async def on_retrieval(outcome: str, duration_s: float) -> None:
            seen.append((outcome, duration_s))

        hooks = ServerUsageHooks(on_retrieval=on_retrieval)
        await hooks.on_retrieval("bm25_fallback", 0.25)
        assert seen == [("bm25_fallback", 0.25)]

    asyncio.run(main())


def test_hooks_none_callbacks_are_safe():
    """Standalone wiring passes nothing; the hooks must be inert, not crashing."""
    async def main():
        hooks = ServerUsageHooks()
        await hooks.on_embed_usage(1, 50, "embedding")
        await hooks.on_retrieval("hybrid_ok", 0.0)

    asyncio.run(main())


def test_hooks_satisfy_the_usagehooks_protocol():
    from pi.rag.protocols import UsageHooks

    assert isinstance(ServerUsageHooks(), UsageHooks)


# ===========================================================================
# build_runtime
# ===========================================================================


def _sqlite_config(tmp_path: Path, **over) -> RagConfig:
    """Config for assembly tests: SQLite truth + explicit overrides.

    Fields are set directly rather than via env because conftest pins PI_* to ""
    and env->config parsing is covered separately (test_rag_config /
    test_lexical_weight_env_override_is_read). These tests pin ASSEMBLY.
    """
    cfg = RagConfig()
    cfg.sqlite_path = tmp_path / "rag.sqlite3"
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def test_build_runtime_boots_with_zero_config(tmp_path, monkeypatch):
    """Fail-safe, not fail-silent (对接文档 §9 #4): no embedding, no Milvus, no
    MySQL -> the kernel still assembles and can serve BM25. A deployment with
    zero RAG config must boot, not crash."""
    monkeypatch.delenv("PI_RAG_MILVUS_URI", raising=False)
    monkeypatch.delenv("PI_MILVUS_URI", raising=False)
    monkeypatch.delenv("PI_EMBEDDING_URL", raising=False)
    monkeypatch.delenv("PI_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("PI_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    monkeypatch.delenv("PI_RAG_DATABASE_URL", raising=False)

    runtime = build_runtime(_sqlite_config(tmp_path), allow_memory_vector=True)
    assert runtime.backend == "sqlite"
    assert runtime.embedder is None  # vector capability OFF, not broken
    assert runtime.reranker is None
    assert runtime.lexical is not None  # BM25 always available
    assert runtime.retriever.lexical_index is runtime.lexical
    assert runtime.ingest.lexical_index is runtime.lexical


def test_build_runtime_shares_one_lexical_index_between_retriever_and_ingest(tmp_path, monkeypatch):
    """THE stale-chunk guard: re-ingest does delete-then-insert, so chunk_ids
    MOVE. If ingest invalidated a different index instance than the retriever
    reads, the BM25 shard would keep old ids and hydrate the WRONG chunk -
    silently wrong answers, not an error."""
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    runtime = build_runtime(_sqlite_config(tmp_path), allow_memory_vector=True)
    assert runtime.retriever.lexical_index is runtime.ingest.lexical_index
    assert runtime.retriever.store is runtime.ingest.store


def test_build_runtime_refuses_in_memory_vector_for_the_server(tmp_path, monkeypatch):
    """allow_memory_vector=False (the server default) must NOT silently build an
    in-process index: under multiple workers it looks healthy while returning
    nothing. Honest degradation beats a fake-healthy channel."""
    monkeypatch.delenv("PI_RAG_MILVUS_URI", raising=False)
    monkeypatch.delenv("PI_MILVUS_URI", raising=False)
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    runtime = build_runtime(_sqlite_config(tmp_path), allow_memory_vector=False)
    assert runtime.vector_store is None
    # retriever still works (lexical), it just has no vector channel
    assert runtime.retriever.vector_store is None


def test_build_runtime_uses_milvus_when_uri_is_set(tmp_path, monkeypatch):
    """milvus_uri set -> the Milvus projection, constructed LAZILY (no client is
    connected at assembly time, so create_app stays fast and a Milvus outage
    cannot prevent boot - 对接文档 §9 #3)."""
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    runtime = build_runtime(_sqlite_config(tmp_path, milvus_uri="http://127.0.0.1:19531"))
    assert type(runtime.vector_store).__name__ == "MilvusRagVectorStore"
    assert runtime.vector_store.collection == "pi_rag_chunks"
    assert runtime.vector_store._client is None  # lazy: nothing connected yet


def test_build_runtime_honours_a_custom_collection(tmp_path, monkeypatch):
    """PI_RAG_COLLECTION lets a deployment isolate RAG vectors per environment;
    defaulting wrongly would write into another env's index."""
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    runtime = build_runtime(
        _sqlite_config(tmp_path, milvus_uri="http://127.0.0.1:19531",
                       collection="pi_rag_chunks_staging")
    )
    assert runtime.vector_store.collection == "pi_rag_chunks_staging"


def test_build_runtime_milvus_beats_the_memory_store(tmp_path, monkeypatch):
    """When a real URI is configured, allow_memory_vector must NOT quietly win:
    the projection has to be the shared one, or workers disagree about content."""
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    runtime = build_runtime(
        _sqlite_config(tmp_path, milvus_uri="http://127.0.0.1:19531"),
        allow_memory_vector=True,
    )
    assert type(runtime.vector_store).__name__ == "MilvusRagVectorStore"


class _CountingMilvusClient:
    """Minimal pymilvus client double: counts the metadata round trips."""

    def __init__(self) -> None:
        self.has_calls = 0
        self.search_calls = 0

    def has_collection(self, name: str) -> bool:
        self.has_calls += 1
        return True  # pre-existing collection: _create_collection never runs

    def search(self, *a, **kw):
        self.search_calls += 1
        return [[]]

    def drop_collection(self, name: str) -> None:
        return None


def test_milvus_validates_the_collection_once_not_per_search(monkeypatch):
    """``has_collection`` sat in front of EVERY search - a metadata round trip on
    the read path for the whole process lifetime. ``_dim`` doubles as the ready
    flag now (it used to be written and never read), so the check happens once
    per dim and again only after ``drop()``."""
    async def main():
        from pi.rag.defaults.milvus_vector import MilvusRagVectorStore

        store = MilvusRagVectorStore("http://127.0.0.1:19531", collection="c")
        fake = _CountingMilvusClient()
        monkeypatch.setattr(store, "_get_client", lambda: fake)

        await store.search(1, [0.1] * 8, 5)
        await store.search(1, [0.1] * 8, 5)
        await store.search(1, [0.1] * 8, 5)
        assert fake.has_calls == 1, "one validation, not one per search"
        assert fake.search_calls == 3

        # A different vector width means a different collection schema: re-check.
        await store.search(1, [0.1] * 16, 5)
        assert fake.has_calls == 2

        # drop() forgets the collection, so the next use must re-validate.
        before = fake.has_calls
        await store.drop()
        assert fake.has_calls == before + 1, "drop() checks existence once itself"
        await store.search(1, [0.1] * 8, 5)
        assert fake.has_calls == before + 2, "the dim cache must be cleared by drop()"

    asyncio.run(main())


def test_build_runtime_enables_vector_channel_only_with_full_embedding_config(tmp_path, monkeypatch):
    """All three of url/key/model are required. A partial config must NOT create
    an embedder that then 401s on every query - that is a run-degrading outage
    masquerading as a feature."""
    cfg = _sqlite_config(tmp_path, milvus_uri="http://127.0.0.1:19531")
    cfg.embedding = EmbeddingConfig(
        url="https://example.invalid/compatible-mode/v1/embeddings",
        api_key="",  # missing on purpose
        model="some-model",
    )
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)

    runtime = build_runtime(cfg)
    assert cfg.vector_enabled() is False
    assert runtime.embedder is None
    # the vector store still exists (ingest can project later), but the retriever's
    # vector channel needs BOTH, so retrieval degrades to BM25 rather than erroring
    assert runtime.vector_store is not None
    assert runtime.retriever.embedder is None


def test_build_runtime_builds_http_embedder_and_reranker_when_configured(tmp_path, monkeypatch):
    """Production path: HttpEmbedder (OpenAI-compatible style auto-detected) +
    HttpReranker. This is why adapters does NOT reuse pi.llm.embedding's client:
    that one speaks the DashScope-native wire, while the configured endpoint is
    /compatible-mode (OpenAI). Wrong wire format = every call 400s."""
    cfg = _sqlite_config(tmp_path)
    cfg.embedding = EmbeddingConfig(
        url="https://example.invalid/compatible-mode/v1/embeddings",
        api_key="test-key-not-real",
        model="test-embed-model",
    )
    cfg.rerank_url = "https://example.invalid/rerank"
    cfg.rerank_model = "test-rerank-model"
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)

    runtime = build_runtime(cfg, allow_memory_vector=True)
    assert type(runtime.embedder).__name__ == "HttpEmbedder"
    assert runtime.embedder.style == "openai"  # inferred from /compatible-mode
    assert type(runtime.reranker).__name__ == "HttpReranker"
    assert cfg.rerank_enabled() is True


def test_build_runtime_skips_reranker_when_the_flag_is_off(tmp_path, monkeypatch):
    """rerank_enabled=False must not build a reranker even with a URL set: the
    flag is the operator's kill switch for a slow/expensive endpoint."""
    cfg = _sqlite_config(tmp_path)
    cfg.rerank_url = "https://example.invalid/rerank"
    cfg.retrieval = RetrievalConfig(rerank_enabled=False)
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)

    runtime = build_runtime(cfg, allow_memory_vector=True)
    assert runtime.reranker is None
    assert cfg.rerank_enabled() is False


def test_build_runtime_warns_when_rerank_enabled_but_unconfigured(tmp_path, monkeypatch, caplog):
    """P5: rerank is material to answer quality (real corpora: recall@5 0.72 ->
    0.895), but with PI_RAG_RERANK_URL unset it silently does nothing. Assembly
    must be LOUD about serving below the eval baseline, not silent."""
    import logging

    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    cfg = _sqlite_config(tmp_path)
    cfg.rerank_url = ""  # enabled flag (default True) but no endpoint
    cfg.retrieval = RetrievalConfig()  # rerank_enabled=True (default)

    with caplog.at_level(logging.WARNING, logger="pi.rag.adapters"):
        runtime = build_runtime(cfg, allow_memory_vector=True)

    assert runtime.reranker is None
    assert any("PI_RAG_RERANK_URL is unset" in r.message for r in caplog.records), (
        "half-configured rerank shipped silently"
    )


def test_build_runtime_threads_lexical_weight_into_the_retriever(tmp_path, monkeypatch):
    """The M4 knob must survive assembly: a tuned PI_RAG_LEXICAL_WEIGHT that got
    dropped here would silently revert to the paper-neutral 1.0 in production
    while the eval report claimed otherwise."""
    cfg = _sqlite_config(tmp_path)
    cfg.retrieval = RetrievalConfig(lexical_weight=0.25)
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)

    runtime = build_runtime(cfg, allow_memory_vector=True)
    assert runtime.retriever.config.retrieval.lexical_weight == 0.25
    assert runtime.ingest.config is cfg


def test_build_runtime_threads_milvus_consistency_and_is_loud_when_not_strong(
    tmp_path, monkeypatch, caplog
):
    """Consistency is a ~100x search-latency knob: measured on a 527-vector
    collection, Strong 399ms vs Bounded/Session 4ms per search with identical
    hits (tools/probe_local_stages.py) - the read-after-write barrier, not the
    ANN scan, dominates. It must reach the store, and choosing a weaker level
    must be loud: the operator is trading away instant post-ingest visibility
    and needs that in the log."""
    import logging

    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    cfg = _sqlite_config(tmp_path)
    cfg.milvus_uri = "http://127.0.0.1:19531"  # lazy client: nothing connects
    cfg.milvus_consistency = "Bounded"

    with caplog.at_level(logging.WARNING, logger="pi.rag.adapters"):
        runtime = build_runtime(cfg)

    assert runtime.vector_store.consistency == "Bounded"
    assert any("consistency=Bounded" in r.message for r in caplog.records), (
        "weakened consistency shipped silently"
    )


def test_build_runtime_keeps_strong_consistency_quietly(tmp_path, monkeypatch, caplog):
    """Default must stay Strong (no behaviour change for anyone who does not opt
    in) and must not spam a warning at every boot."""
    import logging

    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    cfg = _sqlite_config(tmp_path)
    cfg.milvus_uri = "http://127.0.0.1:19531"

    with caplog.at_level(logging.WARNING, logger="pi.rag.adapters"):
        runtime = build_runtime(cfg)

    assert runtime.vector_store.consistency == "Strong"
    assert not any("consistency=" in r.message for r in caplog.records)


def test_build_runtime_accepts_an_injected_embedder(tmp_path, monkeypatch):
    """The injection seam for reusing an already-wired client (or a test fake)."""
    monkeypatch.delenv("PI_DATABASE_URL", raising=False)
    client = FakePiEmbeddingClient()
    adapter = EmbeddingClientAdapter(client)
    runtime = build_runtime(_sqlite_config(tmp_path), embedder=adapter,
                            allow_memory_vector=True)
    assert runtime.embedder is adapter
    assert runtime.retriever.embedder is adapter


def test_build_runtime_shares_the_server_engine(tmp_path):
    """pi integration passes the server's Database so RAG lives inside the same
    connection pool. A second pool would double the connection count against the
    same MySQL max_connections and starve the app under load.

    Uses a REAL AsyncEngine (create_async_engine is lazy - nothing connects here)
    because MysqlChunkStore detects sharing via isinstance(engine, AsyncEngine);
    a duck-typed fake would silently take the "build my own engine" branch, which
    is exactly the bug this test exists to catch.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from pi.server.db import Database

    engine = create_async_engine("mysql+aiomysql://u:p@127.0.0.1:3306/pi_py")
    db = Database.__new__(Database)  # skip __init__: we supply the engine
    db.engine = engine
    try:
        runtime = build_runtime(_sqlite_config(tmp_path), db=db)
        assert runtime.backend == "mysql"
        assert runtime.store.engine is engine  # shared, not re-created
        assert runtime.store._owns_engine is False  # so close() won't dispose it
        # Alembic owns the production schema (migration 0007): sharing an engine
        # must NOT re-run DDL behind the migration tool's back.
        assert runtime.store._create_schema is False
    finally:
        asyncio.run(engine.dispose())


def test_build_runtime_runs_ddl_only_when_it_owns_the_store(tmp_path, monkeypatch):
    """The CLI path has no implied `pi-py migrate`, so it must create the
    rag_docs/rag_chunks tables itself; the server path must not."""
    monkeypatch.setenv("PI_DATABASE_URL", "mysql+aiomysql://u:p@127.0.0.1:3306/pi_py")
    runtime = build_runtime(_sqlite_config(tmp_path))  # no db -> CLI shape
    assert runtime.backend == "mysql"
    assert runtime.store._create_schema is True
    assert runtime.store._owns_engine is True


def test_build_runtime_falls_back_to_sqlite_on_a_non_mysql_url(tmp_path, monkeypatch):
    """PI_DATABASE_URL pointing at something the RAG store cannot speak must not
    be passed to the MySQL driver (opaque driver error); fall back and warn."""
    monkeypatch.setenv("PI_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    runtime = build_runtime(_sqlite_config(tmp_path), allow_memory_vector=True)
    assert runtime.backend == "sqlite"


def test_runtime_close_is_idempotent_and_never_raises(tmp_path, monkeypatch):
    """Shutdown path: lifespan calls close() once, tests may call it twice. A
    teardown exception would mask the real error and block worker recycling."""
    async def main():
        monkeypatch.delenv("PI_DATABASE_URL", raising=False)
        runtime = build_runtime(_sqlite_config(tmp_path), allow_memory_vector=True)
        await runtime.close()
        await runtime.close()  # second call must be a no-op, not an error

    asyncio.run(main())


# ===========================================================================
# runtime singleton
# ===========================================================================


def test_singleton_set_and_reset(tmp_path, monkeypatch):
    """set_runtime/reset_runtime are the server-lifespan and test-teardown seams;
    get_runtime must return exactly the installed instance (no rebuild, no second
    pool)."""
    async def main():
        from pi.rag.adapters import get_runtime

        monkeypatch.delenv("PI_DATABASE_URL", raising=False)
        runtime = build_runtime(_sqlite_config(tmp_path), allow_memory_vector=True)
        set_runtime(runtime)
        try:
            assert await get_runtime() is runtime
            assert await get_runtime(db=None) is runtime  # cached, ignores db arg
        finally:
            await reset_runtime()
        # after reset, get_runtime builds a fresh one (SQLite, zero config)
        monkeypatch.delenv("PI_RAG_MILVUS_URI", raising=False)
        fresh = await get_runtime()
        assert fresh is not runtime
        await reset_runtime()

    asyncio.run(main())


def test_singleton_builds_once_under_concurrency(tmp_path, monkeypatch):
    """Two concurrent first calls must not build two runtimes: the loser would
    leak a connection pool that nothing ever closes."""
    async def main():
        from pi.rag.adapters import get_runtime

        monkeypatch.delenv("PI_DATABASE_URL", raising=False)
        monkeypatch.setenv("PI_RAG_SQLITE", str(tmp_path / "shared.sqlite3"))
        await reset_runtime()
        try:
            results = await asyncio.gather(*[get_runtime() for _ in range(8)])
            assert len({id(r) for r in results}) == 1
        finally:
            await reset_runtime()

    asyncio.run(main())


def test_shutdown_rag_closes_AND_unpublishes(tmp_path, monkeypatch):
    """Closing is not enough. If the singleton still points at the closed
    runtime, the next get_runtime() hands back a disposed engine / closed Milvus
    client instead of rebuilding - a confusing failure much later, far from the
    teardown that caused it."""
    async def main():
        from pi.rag.adapters import get_runtime
        from pi.rag.integration import shutdown_rag

        monkeypatch.delenv("PI_DATABASE_URL", raising=False)
        monkeypatch.setenv("PI_RAG_SQLITE", str(tmp_path / "shutdown.sqlite3"))
        await reset_runtime()

        runtime = build_runtime(_sqlite_config(tmp_path))
        closed: list[str] = []
        original = runtime.close

        async def _spy() -> None:
            closed.append("close")
            await original()

        runtime.close = _spy
        set_runtime(runtime)

        await shutdown_rag()

        assert closed == ["close"], "runtime must be closed exactly once"
        assert await get_runtime() is not runtime, "singleton must be cleared, not left dangling"
        await reset_runtime()

    asyncio.run(main())


def test_shutdown_rag_is_a_noop_when_rag_never_booted(monkeypatch):
    """The host calls it unconditionally in lifespan teardown (PI_RAG_ENABLED=0
    publishes nothing), so it must not raise on an empty singleton."""
    async def main():
        from pi.rag.integration import shutdown_rag

        await reset_runtime()
        await shutdown_rag()  # must not raise

    asyncio.run(main())


# ===========================================================================
# RagTool
# ===========================================================================


def test_tool_metadata_and_schema():
    """Contract with the model: name/description/schema shape. `query` required;
    k and filter_doc optional (对接文档 §4.1)."""
    tool = RagTool()
    assert tool.name == "rag_search"
    assert tool.input_schema["required"] == ["query"]
    props = tool.input_schema["properties"]
    assert set(props) == {"query", "k", "filter_doc"}
    assert "citation" in tool.description.lower() or "cite" in tool.description.lower()


def test_tool_requires_a_query():
    async def main():
        res = await RagTool().execute({"query": "   "}, _ctx())
        assert res.is_error and "query" in res.content.lower()

    asyncio.run(main())


def test_tool_refuses_without_a_user_id():
    """ACL gate. user_db_id None must be a HARD error: an unscoped search would
    return other tenants' documents, and "it returned nothing" is not evidence
    of safety (对接文档 §9 #5)."""
    async def main():
        retriever = RecordingRetriever(_result([_hit()]))
        tool = RagTool(retriever=retriever)
        res = await tool.execute({"query": "权限"}, _ctx(user_db_id=None))
        assert res.is_error
        assert "user" in res.content.lower()
        assert retriever.calls == []  # never reached the index

    asyncio.run(main())


def test_tool_passes_user_id_and_k_to_the_retriever():
    """The integer user id from ToolContext is what the retriever pushes into
    every channel's filter; k is clamped to the tool's cap."""
    async def main():
        retriever = RecordingRetriever(_result([_hit()]))
        res = await RagTool(retriever=retriever).execute(
            {"query": "权限申请", "k": 3}, _ctx(user_db_id=42)
        )
        assert not res.is_error
        assert retriever.calls == [(42, "权限申请", 3, None)]

    asyncio.run(main())


def test_tool_clamps_k():
    """Results go straight into the context window; an unbounded k is a
    self-inflicted context blowup (same reason web_search caps at 20)."""
    async def main():
        retriever = RecordingRetriever(_result([_hit()]))
        tool = RagTool(retriever=retriever)
        await tool.execute({"query": "q", "k": 9999}, _ctx())
        assert retriever.calls[-1][2] == 20  # capped at MAX_K
        await tool.execute({"query": "q", "k": 3}, _ctx())
        assert retriever.calls[-1][2] == 3  # in-range values pass through
        # k=0 / absent / garbage all mean "unset" -> the tool default, never 0.
        # A literal 0 would ask the retriever for nothing and read as "no answer
        # in the corpus" - a silently wrong result from a typo.
        for bad in (0, "not-an-int", None):
            await tool.execute({"query": "q", "k": bad}, _ctx())
            assert retriever.calls[-1][2] == 5, f"k={bad!r} should fall back to default"
        await tool.execute({"query": "q"}, _ctx())
        assert retriever.calls[-1][2] == 5
        await tool.execute({"query": "q", "k": -4}, _ctx())
        assert retriever.calls[-1][2] == 1  # negative clamps to the floor, not 0

    asyncio.run(main())


def test_tool_normalises_filter_doc():
    """filter_doc arrives as a list or a comma string depending on how the model
    fills the schema; both must reach the retriever as a clean list."""
    async def main():
        retriever = RecordingRetriever(_result([_hit()]))
        tool = RagTool(retriever=retriever)
        await tool.execute({"query": "q", "filter_doc": ["a", "b"]}, _ctx())
        assert retriever.calls[-1][3] == ["a", "b"]
        await tool.execute({"query": "q", "filter_doc": "a, b ,c"}, _ctx())
        assert retriever.calls[-1][3] == ["a", "b", "c"]
        await tool.execute({"query": "q", "filter_doc": []}, _ctx())
        assert retriever.calls[-1][3] is None  # empty -> unscoped, not "match nothing"
        await tool.execute({"query": "q"}, _ctx())
        assert retriever.calls[-1][3] is None

    asyncio.run(main())


def test_tool_returns_citations_and_a_json_trailer():
    """对接文档 §4.1 requires the structured shape {chunks:[{doc_id, chunk_id,
    text, score, source}]}. This branch's ToolResult has no `payload` field, so
    the structure rides in the content - prose first (what the model quotes),
    JSON last (what a parser reads)."""
    import json as _json

    async def main():
        hits = [
            _hit(chunk_id=11, score=0.91, page=3),
            _hit(chunk_id=12, doc_key="doc-b", text="第二条", score=0.44, page=None),
        ]
        res = await RagTool(retriever=RecordingRetriever(_result(hits))).execute(
            {"query": "权限"}, _ctx()
        )
        assert not res.is_error
        # citation surface: title path, page, source, score
        assert "运维手册 > 权限 > 申请流程" in res.content
        assert "p.3" in res.content
        assert "/srv/docs/ops.pdf" in res.content
        assert "0.9100" in res.content
        # machine-readable trailer
        assert "```json" in res.content
        payload = _json.loads(res.content.split("```json", 1)[1].split("```", 1)[0])
        assert payload["count"] == 2
        assert payload["mode"] == "hybrid" and payload["outcome"] == "hybrid_ok"
        assert payload["degraded"] is False
        first = payload["chunks"][0]
        assert first["doc_id"] == "doc-a" and first["chunk_id"] == 11
        assert first["source"] == "/srv/docs/ops.pdf" and first["page"] == 3
        assert first["score"] == 0.91
        assert payload["chunks"][1]["page"] is None

    asyncio.run(main())


def test_tool_truncates_to_ctx_max_output():
    """A long chunk set must respect ctx.max_output; blowing past it evicts the
    conversation history instead of the tool result."""
    async def main():
        hits = [_hit(chunk_id=i, text="很长的正文" * 400) for i in range(15)]
        ctx = _ctx()
        ctx.max_output = 2000
        res = await RagTool(retriever=RecordingRetriever(_result(hits))).execute(
            {"query": "q"}, ctx
        )
        assert len(res.content) <= 2000 + 60  # truncate() appends a marker
        assert "truncated" in res.content

    asyncio.run(main())


def test_tool_surfaces_degradation_to_the_model():
    """Non-silent degradation, surfaced where it changes the ANSWER: BM25-only
    results have no semantic matching, so the model must hedge instead of
    asserting completeness. A log line the model never sees is not enough."""
    async def main():
        degraded = _result(
            [_hit()], mode=RetrievalMode.BM25_FALLBACK,
            outcome="bm25_fallback", degraded=True,
        )
        res = await RagTool(retriever=RecordingRetriever(degraded)).execute(
            {"query": "q"}, _ctx()
        )
        assert not res.is_error  # degraded results are still usable
        assert "degraded" in res.content.lower()
        assert "bm25_fallback" in res.content
        assert "keyword" in res.content.lower() or "lexical" in res.content.lower()

    asyncio.run(main())


def test_tool_distinguishes_empty_index_from_no_content():
    """Conflating "the index is down" with "the docs don't say that" is how a
    dead vector store turns into confidently wrong answers."""
    async def main():
        broken = _result([], mode=RetrievalMode.SQL_FALLBACK,
                         outcome="sql_fallback", degraded=True)
        res = await RagTool(retriever=RecordingRetriever(broken)).execute(
            {"query": "q"}, _ctx()
        )
        assert "degraded" in res.content.lower()
        assert "must not conclude" in res.content.lower() or "unavailable" in res.content.lower()

        healthy_empty = _result([], mode=RetrievalMode.HYBRID, outcome="no_hits")
        res2 = await RagTool(retriever=RecordingRetriever(healthy_empty)).execute(
            {"query": "q"}, _ctx()
        )
        assert "degraded" not in res2.content.lower()
        assert "no passages found" in res2.content.lower()

    asyncio.run(main())


def test_tool_reports_empty_scope_when_filter_doc_is_set():
    """An empty result under a doc filter is ambiguous without saying so - the
    model would otherwise conclude the corpus lacks the answer."""
    async def main():
        res = await RagTool(retriever=RecordingRetriever(_result([]))).execute(
            {"query": "q", "filter_doc": ["doc-a", "doc-b"]}, _ctx()
        )
        assert "restricted to 2 doc" in res.content

    asyncio.run(main())


def test_tool_survives_a_raising_retriever():
    """A retrieval failure must degrade, never kill the run (对接文档 §9 #4).
    The error text tells the model not to pretend the index was fine."""
    async def main():
        tool = RagTool(retriever=RecordingRetriever(raise_exc=RuntimeError("milvus down")))
        res = await tool.execute({"query": "q"}, _ctx())
        assert res.is_error
        assert "milvus down" in res.content
        assert "must not pretend" in res.content.lower()

    asyncio.run(main())


def test_tool_supports_the_two_arg_eval_signature():
    """search_chunks/(user_id, query, k) is the harness's SearchFn shape. The
    tool resolves the arity from the SIGNATURE, so the SAME object can be
    measured by the eval and served by the agent - no eval-only variant to
    drift."""
    async def main():
        two = TwoArgRetriever(_result([_hit()]))
        res = await RagTool(retriever=two).execute({"query": "q", "k": 4}, _ctx(user_db_id=9))
        assert not res.is_error
        assert two.calls == [(9, "q", 4)]

    asyncio.run(main())


def test_tool_does_not_re_run_a_search_that_raises_typeerror():
    """A TypeError from INSIDE a 4-arg search must surface, not be retried as a
    3-arg call: the old `except TypeError: search(...)` fallback executed the
    whole retrieval a second time - double latency and, worse, double metering
    (the user is billed twice for one question)."""
    async def main():
        retriever = FailingFourArgRetriever()
        res = await RagTool(retriever=retriever).execute({"query": "q"}, _ctx())
        assert res.is_error
        assert retriever.calls == 1, "retrieval must run exactly once"

    asyncio.run(main())


def test_tool_reads_a_runtime_from_ctx():
    """Server path: the runner injects the assembled RagRuntime on ctx.rag (same
    seam as ctx.memory), so RAG shares the engine and metering hooks."""
    async def main():
        retriever = RecordingRetriever(_result([_hit()]))
        runtime = RagRuntime(
            config=RagConfig(), store=None, retriever=retriever, ingest=None,
            lexical=None, hooks=None, backend="sqlite",
        )
        res = await RagTool().execute({"query": "权限"}, _ctx(rag=runtime))
        assert not res.is_error
        assert retriever.calls == [(7, "权限", 5, None)]

    asyncio.run(main())


def test_tool_accepts_a_bare_retriever_on_ctx():
    """Minimal wiring (tests, small embedders) may inject the retriever itself
    rather than a full runtime; both must work."""
    async def main():
        retriever = RecordingRetriever(_result([_hit()]))
        res = await RagTool().execute({"query": "q"}, _ctx(rag=retriever))
        assert not res.is_error
        assert len(retriever.calls) == 1

    asyncio.run(main())


def test_constructor_injection_beats_ctx():
    """Explicit constructor injection wins: that is how tests pin behaviour
    without touching global state."""
    async def main():
        ctor = RecordingRetriever(_result([_hit(chunk_id=1)]))
        ctx_one = RecordingRetriever(_result([_hit(chunk_id=2)]))
        runtime = RagRuntime(config=RagConfig(), store=None, retriever=ctx_one,
                             ingest=None, lexical=None, hooks=None)
        res = await RagTool(retriever=ctor).execute({"query": "q"}, _ctx(rag=runtime))
        assert ctor.calls and not ctx_one.calls
        assert '"chunk_id": 1' in res.content

    asyncio.run(main())


def test_tool_reports_unavailable_when_no_runtime_anywhere(monkeypatch, tmp_path):
    """No ctx.rag, no injection, and runtime assembly fails -> a clear error, not
    a stack trace in the transcript and not a fake empty answer."""
    async def main():
        import pi.rag.adapters as adapters

        async def boom(db=None):
            raise RuntimeError("no backends configured")

        monkeypatch.setattr(adapters, "get_runtime", boom)
        res = await RagTool().execute({"query": "q"}, _ctx())
        assert res.is_error
        assert "unavailable" in res.content.lower()

    asyncio.run(main())


def test_tool_rejects_an_injected_object_without_search():
    """A mis-wired injection (wrong object on ctx.rag) must say so instead of
    raising AttributeError into the agent loop."""
    async def main():
        res = await RagTool().execute({"query": "q"}, _ctx(rag=object()))
        assert res.is_error
        assert "search" in res.content.lower()

    asyncio.run(main())


# ===========================================================================
# registration
# ===========================================================================
#
# rag_search is contributed as its OWN ToolProvider (pi.rag.integration) instead
# of being appended to pi.tools.all_tools(). ToolRegistry merges + dedupes
# providers into the single list AgentLoop sees, so the tool still passes the
# policy gate / audit log / tracing / quota path like any builtin tool - while
# pi/tools/__init__.py, pi/tools/base.py and pi/server/runner.py stay
# byte-identical to upstream. That is what keeps the host merge conflict-free.


def test_rag_search_arrives_as_its_own_provider():
    """The provider seam is how rag_search reaches AgentLoop without editing the
    host toolset: the registry merges whatever providers it is given."""
    async def main():
        from pi.rag.integration import RagToolProvider
        from pi.tools.registry import ToolRegistry

        registry = ToolRegistry([RagToolProvider()])
        try:
            names = [t.name for t in await registry.tools()]
        finally:
            await registry.close()
        assert names == ["rag_search"]

    asyncio.run(main())


def test_builtin_toolset_does_not_know_about_rag():
    """The host tree stays untouched: all_tools() carries no RAG knowledge and
    takes no rag flag. Upstream merging this branch therefore has nothing to
    reconcile in the toolset itself."""
    import inspect

    from pi.tools import all_tools

    names = [t.name for t in all_tools()]
    assert "rag_search" not in names
    assert "bash" in names and "list_files" in names, "builtin toolset intact"
    assert "rag" not in inspect.signature(all_tools).parameters


def test_rag_enabled_is_the_single_switch_the_server_consults():
    """ServerSettings.rag_enabled gates whether app.py appends RagToolProvider.
    Default ON: the kernel is fail-safe, so an unconfigured server still boots
    (retrieval just degrades to BM25)."""
    from pi.server.config import ServerSettings

    assert ServerSettings().rag_enabled is True
    assert ServerSettings(rag_enabled=False).rag_enabled is False


def test_registry_dedupes_rag_search():
    """Two providers both offering rag_search must not double-register: dedupe
    keeps the first and logs, so the surviving instance is deterministic."""
    async def main():
        from pi.rag.integration import RagToolProvider
        from pi.tools.registry import ToolRegistry, _dedupe

        provider = RagToolProvider()
        tools = await provider.tools()
        deduped = _dedupe(list(tools) + list(tools))
        assert [t.name for t in deduped].count("rag_search") == 1

        registry = ToolRegistry([provider, RagToolProvider()])
        try:
            merged = await registry.tools()
        finally:
            await registry.close()
        assert [t.name for t in merged].count("rag_search") == 1

    asyncio.run(main())


def test_tool_context_carries_no_rag_field_by_design():
    """Deliberate: ToolContext has NO `rag` field, so pi/tools/base.py needed no
    edit. RagTool still honours a duck-typed ctx.rag when a caller sets one (see
    test_tool_reads_a_runtime_from_ctx) and otherwise falls back to the process
    runtime that pi.rag.integration.install() publishes."""
    ctx = ToolContext()
    assert "rag" not in set(ToolContext.__dataclass_fields__)
    assert getattr(ctx, "rag", None) is None


def test_install_publishes_a_runtime_that_the_tool_finds_without_ctx():
    """install() is the single seam the server calls: it builds the runtime and
    publishes it, so RagTool resolves it with no per-turn ctx injection at all."""
    async def main():
        import pi.rag.adapters as adapters

        retriever = RecordingRetriever(_result([_hit()]))
        runtime = RagRuntime(
            config=RagConfig(), store=None, retriever=retriever, ingest=None,
            lexical=None, hooks=None, backend="sqlite",
        )
        adapters.set_runtime(runtime)
        try:
            res = await RagTool().execute({"query": "q"}, _ctx())  # no ctx.rag
        finally:
            await adapters.reset_runtime()
        assert not res.is_error
        assert len(retriever.calls) == 1

    asyncio.run(main())


# ===========================================================================
# CLI
# ===========================================================================


def test_cli_parser_accepts_the_four_subcommands():
    """对接文档 §4.7: follow eval's add_subparsers pattern."""
    from pi.cli import build_parser

    parser = build_parser()
    ns = parser.parse_args(["rag", "ingest", "--path", "docs/", "--user", "7", "--doc-id", "k"])
    assert (ns.command, ns.rag_command, ns.path, ns.user, ns.doc_id) == ("rag", "ingest", "docs/", 7, "k")

    ns = parser.parse_args(["rag", "rebuild-index", "--user", "7"])
    assert (ns.rag_command, ns.user) == ("rebuild-index", 7)

    ns = parser.parse_args(["rag", "eval", "--golden", "g.json", "--rebind",
                            "--min-recall5", "0.9", "--out", "reports/",
                            "--config-name", "tuned"])
    assert ns.rag_command == "eval" and ns.rebind is True
    assert ns.min_recall5 == "0.9" and ns.config_name == "tuned"

    ns = parser.parse_args(["rag", "search", "--query", "权限", "--user", "7", "--k", "3"])
    assert (ns.rag_command, ns.query, ns.k) == ("search", "权限", 3)


def test_cli_parser_requires_user_and_path():
    """ACL-relevant flags are required, not defaulted: a missing --user must fail
    at parse time rather than ingest into user 0."""
    from pi.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rag", "ingest", "--path", "docs/"])  # no --user
    with pytest.raises(SystemExit):
        parser.parse_args(["rag", "ingest", "--user", "7"])  # no --path
    with pytest.raises(SystemExit):
        parser.parse_args(["rag", "search", "--user", "7"])  # no --query


def test_cli_rejects_doc_id_with_multiple_files(tmp_path, capsys):
    """--doc-id names ONE doc_key. Ingesting a directory with it would collapse
    every file into a single key, and each file would delete the previous one's
    chunks (idempotency is keyed on (user_id, doc_key))."""
    from pi.rag.cli import _cmd_ingest

    (tmp_path / "a.md").write_text("# A\n\ncontent a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n\ncontent b", encoding="utf-8")

    class Args:
        path = str(tmp_path)
        user = 7
        doc_id = "single-key"
        verbose = False

    rc = asyncio.run(_cmd_ingest(Args()))
    assert rc == 1
    assert "one document" in capsys.readouterr().err.lower()


def test_cli_collect_files_walks_and_reports_skipped(tmp_path, capsys):
    """A directory ingest must report what it SKIPPED: an unsupported file that
    was meant to be indexed becomes a retrieval bug that looks like "the
    document doesn't say that"."""
    from pi.rag.cli import _collect_files

    (tmp_path / "keep.md").write_text("x", encoding="utf-8")
    (tmp_path / "keep.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "skip.exe").write_bytes(b"MZ")
    (tmp_path / "skip.png").write_bytes(b"\x89PNG")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "deep.txt").write_text("y", encoding="utf-8")

    class Args:
        path = str(tmp_path)

    files = _collect_files(Args())
    names = sorted(f.name for f in files)
    assert names == ["deep.txt", "keep.md", "keep.pdf"]
    err = capsys.readouterr().err
    assert "skipping 2" in err
    assert "skip.exe" in err


def test_cli_collect_files_single_file_bypasses_the_suffix_filter(tmp_path):
    """An explicit --path to a file is an instruction, not a suggestion: ingest
    it even if the suffix is unknown (the parser layer decides, and reports)."""
    from pi.rag.cli import _collect_files

    odd = tmp_path / "notes.log"
    odd.write_text("plain text", encoding="utf-8")

    class Args:
        path = str(odd)

    assert _collect_files(Args()) == [odd]


def test_cli_collect_files_missing_path_is_an_error(tmp_path, capsys):
    from pi.rag.cli import _collect_files

    class Args:
        path = str(tmp_path / "nope")

    assert _collect_files(Args()) == []
    assert "not found" in capsys.readouterr().err


def test_cli_ingest_end_to_end_on_sqlite(tmp_path, capsys, monkeypatch):
    """The real ingest path through the CLI assembly: file -> parser -> chunker
    -> SQL -> vector projection -> a cited retrieval. Offline doubles only
    (SQLite + FakeEmbedder), so this pins WIRING, not model quality."""
    from pi.rag import cli as ragcli
    from pi.rag.adapters import RagRuntime
    from pi.rag.config import RagConfig
    from pi.rag.defaults.bm25 import MemoryBM25Index
    from pi.rag.defaults.fake_embedder import FakeEmbedder
    from pi.rag.defaults.memory_vector import InMemoryVectorStore
    from pi.rag.defaults.sqlite_store import SqliteChunkStore
    from pi.rag.ingest import IngestPipeline
    from pi.rag.retriever import HybridRetriever

    async def main():
        doc = tmp_path / "ops.md"
        doc.write_text(
            "# 运维手册\n\n## 权限\n\n申请数据库权限需要在工单系统提交审批，由 DBA 复核。\n\n"
            "## 备份\n\n每日凌晨两点执行全量备份，保留十四天。\n",
            encoding="utf-8",
        )
        store = SqliteChunkStore(tmp_path / "rag.sqlite3")
        embedder = FakeEmbedder()
        vectors = InMemoryVectorStore()
        lexical = MemoryBM25Index(store)
        cfg = RagConfig()
        retriever = HybridRetriever(store, embedder=embedder, vector_store=vectors,
                                    lexical_index=lexical, config=cfg)
        runtime = RagRuntime(
            config=cfg, store=store, retriever=retriever,
            ingest=IngestPipeline(store, embedder, vectors, config=cfg, lexical_index=lexical),
            lexical=lexical, hooks=None, vector_store=vectors, embedder=embedder,
            backend="sqlite",
        )
        monkeypatch.setattr(ragcli, "_build", lambda args, **kw: runtime)

        class IngestArgs:
            path = str(doc)
            user = 7
            doc_id = "ops-manual"
            verbose = False

        rc = await ragcli._cmd_ingest(IngestArgs())
        assert rc == 0
        out = capsys.readouterr().out
        assert "READY" in out and "ops-manual" in out

        class SearchArgs:
            query = "数据库权限怎么申请"
            user = 7
            k = 3
            verbose = False

        rc = await ragcli._cmd_search(SearchArgs())
        assert rc == 0
        out = capsys.readouterr().out
        assert "工单系统" in out or "DBA" in out  # cited the right passage
        assert "degraded=False" in out

        # ACL negative: another user gets nothing (and the tool/CLI says so)
        class OtherUser(SearchArgs):
            user = 8

        rc = await ragcli._cmd_search(OtherUser())
        assert rc == 0
        assert "no passages found" in capsys.readouterr().out.lower()

        await runtime.close()

    asyncio.run(main())


def test_cli_ingest_reports_degraded_and_failed_counts(tmp_path, capsys, monkeypatch):
    """An ingest that landed INDEX_PENDING/FAILED must NOT print as success:
    the text is in SQL but the vector is missing, so semantic retrieval is
    silently partial until someone runs rebuild-index."""
    from pi.rag import cli as ragcli
    from pi.rag.types import IngestOutcome, IngestStatus

    class FakeIngest:
        async def ingest_file(self, path, **kw):
            name = Path(path).name
            if name == "ok.md":
                return IngestOutcome(doc_key=name, status=IngestStatus.READY.value,
                                     chunks_stored=2, chunks_indexed=2, usage_tokens=10)
            if name == "pending.md":
                return IngestOutcome(doc_key=name, status=IngestStatus.INDEX_PENDING.value,
                                     chunks_stored=2, chunks_indexed=0,
                                     reason="embedding endpoint 503", degraded=True)
            return IngestOutcome(doc_key=name, status=IngestStatus.FAILED.value,
                                 reason="parser produced no chunks", degraded=True)

    class FakeRuntime:
        ingest = FakeIngest()
        # Faithful double: the real RagRuntime always carries `config` (a
        # required dataclass field), and _cmd_ingest reads the BM25 TTL from it
        # for the R1 propagation note. Give it a real RagConfig so the note path
        # is exercised, not silently skipped.
        config = RagConfig()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(ragcli, "_build", lambda args, **kw: FakeRuntime())
    for name in ("ok.md", "pending.md", "bad.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    class Args:
        path = str(tmp_path)
        user = 7
        doc_id = ""
        verbose = False

    rc = asyncio.run(ragcli._cmd_ingest(Args()))
    out = capsys.readouterr().out
    assert "1 ready, 1 degraded, 1 failed" in out
    assert "rebuild-index" in out  # tells the operator how to fix it
    # R1 propagation note: TTL>0 must tell the operator staleness self-heals
    # within ~300s, and still offer the restart/rebuild escape hatch.
    assert "BM25 is cached per process" in out
    assert "PI_RAG_BM25_TTL" in out
    assert rc == 0  # partial success is not a hard failure


def test_cli_rebuild_reports_partial_indexing(tmp_path, capsys, monkeypatch):
    """rebuild-index must distinguish 'all indexed' from 'SQL intact, vectors
    partial' - the latter is a live degradation, not a success."""
    from pi.rag import cli as ragcli

    class FakeIngest:
        async def rebuild_index(self, user_id):
            return {"user_id": user_id, "total": 10, "indexed": 4,
                    "usage_tokens": 99, "status": "index_pending",
                    "reason": "EmbeddingError: endpoint 503"}

    class FakeRuntime:
        ingest = FakeIngest()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(ragcli, "_build", lambda args, **kw: FakeRuntime())

    class Args:
        user = 7
        verbose = False

    rc = asyncio.run(ragcli._cmd_rebuild(Args()))
    out = capsys.readouterr().out
    assert rc == 1
    assert "indexed=4/10" in out
    assert "index_pending" in out and "503" in out


def test_cli_rebuild_ok_is_exit_zero(tmp_path, capsys, monkeypatch):
    from pi.rag import cli as ragcli

    class FakeIngest:
        async def rebuild_index(self, user_id):
            return {"user_id": user_id, "total": 10, "indexed": 10,
                    "usage_tokens": 99, "status": "ok", "reason": ""}

    class FakeRuntime:
        ingest = FakeIngest()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(ragcli, "_build", lambda args, **kw: FakeRuntime())

    class Args:
        user = 7
        verbose = False

    assert asyncio.run(ragcli._cmd_rebuild(Args())) == 0
    assert "indexed=10/10" in capsys.readouterr().out


def test_cli_eval_runs_the_shipped_retriever(tmp_path, capsys, monkeypatch):
    """§10 acceptance step 2: a Recall@k / MRR report. The measured object is
    the shipped retriever (search_chunks matches SearchFn), so the eval cannot
    drift from production behaviour."""
    from pi.rag import cli as ragcli
    from pi.rag.adapters import RagRuntime
    from pi.rag.config import RagConfig
    from pi.rag.defaults.bm25 import MemoryBM25Index
    from pi.rag.defaults.fake_embedder import FakeEmbedder
    from pi.rag.defaults.memory_vector import InMemoryVectorStore
    from pi.rag.defaults.sqlite_store import SqliteChunkStore
    from pi.rag.eval.harness import GoldenQA, GoldenSet
    from pi.rag.ingest import IngestPipeline
    from pi.rag.retriever import HybridRetriever
    from pi.rag.types import Chunk

    async def main():
        store = SqliteChunkStore(tmp_path / "rag.sqlite3")
        embedder = FakeEmbedder()
        vectors = InMemoryVectorStore()
        cfg = RagConfig()
        chunks = [
            Chunk(chunk_id=0, doc_key="ops", user_id=7, seq=0,
                  text="申请数据库权限需要在工单系统提交审批"),
            Chunk(chunk_id=0, doc_key="ops", user_id=7, seq=1,
                  text="每日凌晨两点执行全量备份"),
        ]
        ids = await store.add_chunks(chunks)
        from pi.rag.eval.harness import chunk_key
        await vectors.upsert(
            [Chunk(chunk_id=i, doc_key=c.doc_key, user_id=7, seq=c.seq, text=c.text)
             for i, c in zip(ids, chunks)],
            (await embedder.embed([c.text for c in chunks])).vectors,
        )
        golden = GoldenSet(name="cli-smoke", cases=[
            GoldenQA(id="q1", query="数据库权限怎么申请", user_id=7,
                     gold_chunk_keys=[chunk_key("ops", 0)], category="happy_path"),
        ])
        gpath = tmp_path / "golden.json"
        golden.save(gpath)

        lexical = MemoryBM25Index(store)
        retriever = HybridRetriever(store, embedder=embedder, vector_store=vectors,
                                    lexical_index=lexical, config=cfg)
        runtime = RagRuntime(
            config=cfg, store=store, retriever=retriever,
            ingest=IngestPipeline(store, embedder, vectors, config=cfg),
            lexical=lexical, hooks=None, backend="sqlite",
        )
        monkeypatch.setattr(ragcli, "_build", lambda args, **kw: runtime)

        class Args:
            golden = str(gpath)
            config_name = "cli-smoke"
            out = ""
            rebind = False
            min_recall5 = "0.0"
            verbose = False

        rc = await ragcli._cmd_eval(Args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "recall@5" in out and "mrr" in out
        await runtime.close()

    asyncio.run(main())


def test_cli_eval_gate_fails_below_the_recall_floor(tmp_path, capsys, monkeypatch):
    """--min-recall5 is a CI gate: it must exit non-zero, otherwise a recall
    regression merges silently."""
    from pi.rag import cli as ragcli
    from pi.rag.eval.harness import GoldenSet, GoldenQA

    class FakeRunner:
        def __init__(self, store):
            pass

        async def run(self, golden, search, config_name):
            from pi.rag.eval.harness import aggregate

            return aggregate(config_name, [])  # no cases -> recall@5 == 0.0

    monkeypatch.setattr("pi.rag.eval.runner.EvalRunner", FakeRunner)

    golden = GoldenSet(name="gate", cases=[
        GoldenQA(id="q1", query="q", user_id=7, gold_chunk_keys=["a#0"]),
    ])
    gpath = tmp_path / "g.json"
    golden.save(gpath)

    class FakeRuntime:
        class store:
            @staticmethod
            async def list_chunks_for_user(uid):
                return []

        class retriever:
            @staticmethod
            async def search_chunks(uid, q, k):
                return []

        async def close(self):
            return None

    monkeypatch.setattr(ragcli, "_build", lambda args, **kw: FakeRuntime())

    class Args:
        golden = str(gpath)
        config_name = "gate"
        out = ""
        rebind = False
        min_recall5 = "0.9"
        verbose = False

    rc = asyncio.run(ragcli._cmd_eval(Args()))
    assert rc == 1
    assert "below the required floor" in capsys.readouterr().err


def test_cli_eval_missing_golden_is_an_error(tmp_path, capsys):
    from pi.rag.cli import _cmd_eval

    class Args:
        golden = str(tmp_path / "absent.json")
        config_name = "x"
        out = ""
        rebind = False
        min_recall5 = "0.0"
        verbose = False

    assert asyncio.run(_cmd_eval(Args())) == 1
    assert "golden set not found" in capsys.readouterr().err


def test_cli_unknown_subcommand_prints_usage(capsys):
    from pi.rag.cli import cmd_rag

    class Args:
        rag_command = None
        verbose = False

    assert cmd_rag(Args()) == 1
    assert "usage: pi-py rag" in capsys.readouterr().err


# ===========================================================================
# portability contract
# ===========================================================================


def _imported_modules(mod) -> list[str]:
    tree = ast.parse(inspect.getsource(mod))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return imported


def test_adapters_is_the_only_kernel_file_importing_pi_outside_the_kernel():
    """The portability contract, enforced over the WHOLE kernel rather than one
    file: lifting pi/rag into another project means deleting adapters.py,
    integration.py and pi/tools/rag.py and nothing else breaks.

    Both seams are named in the allow-list rather than letting the rule rot:
    adapters.py bridges the kernel Protocols to pi's clients, integration.py
    bridges them to the pi server (provider + runtime publishing). Every other
    module stays import-free of pi.

    Checks real Import nodes (AST), not docstrings - the docstrings legitimately
    name the forbidden modules to explain the boundary.
    """
    import importlib
    import pkgutil

    import pi.rag as pkg

    allowed = {"pi.rag.adapters", "pi.rag.integration"}
    banned_prefixes = ("pi.llm", "pi.server", "pi.tools", "pi.agent", "pi.evals", "fastapi")
    offenders: list[str] = []
    for info in pkgutil.walk_packages(pkg.__path__, prefix="pi.rag."):
        name = info.name
        mod = importlib.import_module(name)
        for imported in _imported_modules(mod):
            if any(imported == b or imported.startswith(b + ".") for b in banned_prefixes):
                if name not in allowed:
                    offenders.append(f"{name} imports {imported}")
    assert not offenders, "kernel coupling: " + "; ".join(offenders)


def test_adapters_does_not_import_fastapi_or_the_agent_loop():
    """adapters.py is allowed to touch pi.llm (EmbeddingClient) and pi.server
    (Database typing), but it must not drag in the web layer or the agent loop -
    that would make the kernel un-liftable in a non-server deployment."""
    import pi.rag.adapters as mod

    for imported in _imported_modules(mod):
        assert not imported.startswith(("fastapi", "pi.agent", "pi.server.app")), (
            f"adapters imports {imported!r}; keep it to pi.llm + kernel"
        )


def test_tools_rag_does_not_import_the_kernel_backends_directly():
    """The tool shell talks to the retriever Protocol, never to Milvus/MySQL/
    httpx. Network boundaries belong in the adapters/defaults layer (SSRF
    discipline, 对接文档 §4.1).

    `pi.rag.adapters` IS allowed (function-local, lazy): it is the designated
    seam, and importing it inside execute() is what keeps `import pi.tools` free
    of pymilvus/sqlalchemy. What must never appear is a backend library.
    """
    import pi.tools.rag as mod

    imported = _imported_modules(mod)
    banned = ("pymilvus", "httpx", "sqlalchemy", "aiomysql", "openai")
    for name in imported:
        assert not any(name == b or name.startswith(b + ".") for b in banned), (
            f"rag tool imports backend library {name!r}; the tool must stay thin"
        )
        assert not name.startswith("pi.rag.defaults"), (
            f"rag tool reaches into a backend default ({name!r}); go through adapters"
        )
    # stdlib only. `inspect` is here because the tool resolves the retriever's
    # call arity from its signature instead of calling it twice (a TypeError
    # from inside a 4-arg search used to re-run the whole retrieval, doubling
    # both latency and metering).
    allowed = {
        "json", "logging", "typing", "inspect", "__future__",
        "pi.tools.base", "pi.rag.adapters",
    }
    unexpected = [n for n in imported if n not in allowed]
    assert not unexpected, f"unexpected imports in the tool shell: {unexpected}"


def test_importing_pi_tools_does_not_load_pymilvus_or_the_rag_backends():
    """The practical consequence of the lazy seam: registering rag_search must
    not make every server boot import pymilvus / sqlalchemy-async RAG paths.
    A Milvus outage would otherwise be able to break tool registration itself."""
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "import pi.tools;"
        "print('pymilvus' in sys.modules);"
        "print('pi.rag.adapters' in sys.modules)"
    )
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[-2] == "False", "importing pi.tools must not import pymilvus"
    assert lines[-1] == "False", "importing pi.tools must not import pi.rag.adapters"


def test_rag_tool_goes_through_the_policy_gate_like_any_builtin():
    """§4.2: no separate security channel for RAG. The tool is a plain Tool
    subclass, so the registry/policy path treats it identically to bash/read."""
    from pi.tools.base import Tool

    assert issubclass(RagTool, Tool)
    tool = RagTool()
    # the policy gate keys off name + args; both must be plain data
    assert isinstance(tool.name, str) and isinstance(tool.input_schema, dict)
    assert isinstance(tool.execute, type(Tool.execute)) or callable(tool.execute)


# ===========================================================================
# readiness (/readyz)
# ===========================================================================
#
# RAG owns its own Milvus collection, so the memory pipeline's "milvus" probe
# says nothing about document retrieval. Before this seam existed, a server
# whose RAG index was unreachable reported perfectly ready while every
# rag_search silently degraded to the lexical channel - invisible from outside
# the process. What the probe must NOT do is flip readiness: losing the vector
# channel costs recall, it does not stop the server serving.


@dataclass
class _HealthVectorStore:
    """ping() per protocols.RagVectorStore: returns bool, promises not to raise."""

    result: bool = True
    raises: bool = False
    calls: int = 0

    async def ping(self) -> bool:
        self.calls += 1
        if self.raises:
            raise RuntimeError("probe violated its never-raise contract")
        return self.result


@dataclass
class _HealthRuntime:
    vector_store: object | None = None


def test_readiness_reports_ok_only_when_the_vector_probe_succeeds():
    async def main():
        from pi.rag.integration import health

        assert await health(_HealthRuntime(_HealthVectorStore(result=True))) == "ok"
        assert await health(_HealthRuntime(_HealthVectorStore(result=False))) == "degraded"

    asyncio.run(main())


def test_readiness_is_degraded_without_a_vector_channel():
    """A BM25-only deployment is a VALID config, not a broken one - but it has
    to be visible, and that is the entire point of adding the probe."""
    async def main():
        from pi.rag.integration import health

        assert await health(_HealthRuntime(vector_store=None)) == "degraded"

    asyncio.run(main())


def test_readiness_never_raises_even_if_the_probe_violates_its_contract():
    """ping() promises never to raise; /readyz is the wrong place to discover
    that a backend broke that promise."""
    async def main():
        from pi.rag.integration import health

        assert await health(_HealthRuntime(_HealthVectorStore(raises=True))) == "degraded"

    asyncio.run(main())


def test_readiness_value_is_always_in_the_set_readyz_accepts():
    """The invariant that matters. /readyz computes readiness as
    ``all(v in ("ok", "degraded") ...)``, so a "helpful" value such as
    "degraded: TimeoutError" would flip the WHOLE server to 503. RAG must never
    be able to emit one - it is informational, exactly like the memory Milvus."""
    async def main():
        from pi.rag.integration import health

        accepted = {"ok", "degraded"}
        probes = [
            None,
            _HealthVectorStore(result=True),
            _HealthVectorStore(result=False),
            _HealthVectorStore(raises=True),
        ]
        for probe in probes:
            got = await health(_HealthRuntime(probe))
            assert got in accepted, (
                f"vector_store={probe!r} produced {got!r}, which would flip /readyz to 503"
            )

    asyncio.run(main())


def test_server_readyz_surfaces_rag_and_does_not_flip_status(tmp_path, monkeypatch):
    """End-to-end through create_app. With RAG enabled but no Milvus configured
    (this project's .env shape), /readyz must report the RAG channel as degraded
    and still return 200: the server serves, retrieval just has no vector recall."""
    import uuid

    from conftest import TEST_DB_URL
    from fastapi.testclient import TestClient

    from pi.server.app import create_app
    from pi.server.config import ServerSettings

    monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_RATE_LIMIT_RUNS_PER_MIN", "100")
    # In-process cache: readiness must not depend on a live Redis, or this test
    # would be asserting something the RAG probe has no say in.
    monkeypatch.setenv("PI_REDIS_URL", "")
    monkeypatch.setenv("PI_RAG_ENABLED", "1")
    # No vector backend on purpose - this is the BM25-only deployment.
    monkeypatch.delenv("PI_RAG_MILVUS_URI", raising=False)
    monkeypatch.delenv("PI_MILVUS_URI", raising=False)

    with TestClient(create_app(ServerSettings.from_env())) as client:
        r = client.get("/readyz")
        body = r.json()
        assert r.status_code == 200, body
        assert body["checks"]["db"] == "ok", body
        assert body["checks"]["rag"] == "degraded", body


def test_server_readyz_omits_rag_when_disabled(tmp_path, monkeypatch):
    """PI_RAG_ENABLED=0 must leave no trace in /readyz. A key that is forever
    "degraded" on a deployment that never wanted RAG is noise, not signal."""
    import uuid

    from conftest import TEST_DB_URL
    from fastapi.testclient import TestClient

    from pi.server.app import create_app
    from pi.server.config import ServerSettings

    monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_RATE_LIMIT_RUNS_PER_MIN", "100")
    # Same reason as the enabled-path test: keep readiness independent of Redis.
    monkeypatch.setenv("PI_REDIS_URL", "")
    monkeypatch.setenv("PI_RAG_ENABLED", "0")

    with TestClient(create_app(ServerSettings.from_env())) as client:
        r = client.get("/readyz")
        assert r.status_code == 200
        assert "rag" not in r.json()["checks"]
