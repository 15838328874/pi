"""RAG kernel configuration - all PI_RAG_* env vars, fail-safe defaults.

House rule (§4.8 of the 对接文档): every new setting is a PI_-prefixed env
var with a fail-safe default. "Fail-safe" here means: unset = capability off
or lexical fallback, never a crash. The kernel must boot and answer queries
(BM25-only) with ZERO configuration; vectors/rerank light up when configured.

Precedence: explicit kwargs > env vars > defaults. ``from_env()`` is the
single entry point used by both the CLI and the pi adapters.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("pi.rag.config")


def _inherited(
    primary: str, fallback: str, default: str = "", borrowed: list[tuple[str, str]] | None = None
) -> str:
    """``$primary``, else ``$fallback`` (the server-level / memory-pipeline var).

    The fallback is convenient - one endpoint really does serve both - but it is
    also the quietest way to break a RAG deployment: inheriting the memory
    pipeline's embedding MODEL silently re-points RAG at a different vector
    space, and the Milvus projection (built with the old model) keeps answering
    with vector-space-drifted neighbours. Nothing errors; recall just rots.

    So the fallback stays, but the borrowing is RECORDED rather than warned
    about here: ``from_env`` only emits the note when the borrowed value can
    actually take effect. Warning unconditionally would fire in every BM25-only
    deployment (no Milvus URI -> vectors off -> the model is unused), and a
    warning that always fires is a warning nobody reads.
    """
    value = os.environ.get(primary, "").strip()
    if value:
        return value
    value = os.environ.get(fallback, "").strip()
    if value:
        if borrowed is not None:
            borrowed.append((primary, fallback))
        return value
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        # Bad env value: keep the safe default but make noise (fail-safe !=
        # fail-silent). Startup misconfig should be visible in logs.
        import logging

        logging.getLogger("pi.rag.config").warning(
            "%s=%r is not an int; using default %d", name, raw, default
        )
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        import logging

        logging.getLogger("pi.rag.config").warning(
            "%s=%r is not a float; using default %s", name, raw, default
        )
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# RRF fusion constant. Standard value from the original RRF paper
# (Cormack et al., k=60); it flattens score differences between the
# vector and BM25 rankings so neither dominates on raw magnitude.
RRF_K = 60


@dataclass
class ChunkingConfig:
    """Semantic chunking defaults (对接文档 §8 #2).

    Numbers are CHARACTER based (Chinese text: 1 token ≈ 1.5 chars, so
    800 chars ≈ 512 tokens for mixed CN/EN). Keep them here so the eval
    A/B sweeps can vary them without touching code.
    """

    max_chars: int = 800  # target chunk size (~512 tokens)
    overlap_chars: int = 120  # ~15% overlap
    min_chars: int = 100  # shorter chunks merge into the previous one
    hard_max_chars: int = 2000  # absolute ceiling: split oversized paragraphs
    contextual_prefix: bool = True  # prepend "title > heading path" to embed_text
    contextual_llm: bool = False  # v1.5: LLM-generated context summary (interface reserved)


@dataclass
class RetrievalConfig:
    vector_k: int = 20  # candidates per channel BEFORE fusion
    bm25_k: int = 20
    rrf_k: int = RRF_K
    # Weight of the LEXICAL opinion inside RRF (vector is always 1.0).
    # 1.0 = the original Cormack et al. form and the shipped default; RRF
    # assumes both rankings are equally trustworthy.
    #
    # WHETHER that assumption holds is CORPUS-DEPENDENT, and measured twice in
    # opposite directions (ARCHITECTURE.md §21.6.1):
    #   v1 (390 chunks, 351 homogeneous CSV rows) - the assumption is false.
    #     Gold's mean rank was 1.53 vector vs 2.63 BM25, BM25 added ZERO
    #     uniquely-found gold over 60 queries, and rank-averaging demoted the
    #     gold on 11 queries while lifting it on 9 (recall@5 0.983 vector-only
    #     -> 0.950 fused). Lowering the weight monotonically HELPED.
    #   v2 (527 chunks, prose-dense medical guidelines) - the assumption holds.
    #     Gold's mean rank was 2.83 vector vs 2.06 BM25 (lexical is STRONGER),
    #     BM25 uniquely found the gold on 5/142 queries, fusion lifted it on 34
    #     queries vs dragging it on 11 (recall@5 0.762 vector-only -> 0.806
    #     fused). Lowering the weight monotonically HURT (1.0: 0.806,
    #     0.5: 0.803, 0.25: 0.799, 0.0: 0.762).
    # So do NOT tune this from a single corpus and do NOT change the shipped
    # neutral default without an evals/reports A/B showing a win.
    #
    # A BM25 score floor is NOT a substitute lever: RRF consumes ranks and
    # never sees magnitudes, so gating hits below 20%/35% of the top score
    # moved recall@5 by exactly 0.000 on BOTH corpora. RRF k is not the lever
    # either (k=10/60/200 spread only 0.017-0.033). The thing that actually
    # fixed retrieval on both corpora is the cross-encoder reranker.
    lexical_weight: float = 1.0
    final_k: int = 5  # returned to the model
    rerank_enabled: bool = True  # only effective when a Reranker is injected
    rerank_candidates: int = 20  # how many RRF results go into rerank
    sql_fallback_k: int = 5
    # Budget for the last-resort SQL LIKE scan (R5). The LIKE '%q%' predicate
    # cannot use an index, so on a big rag_chunks table this query is a full
    # scan - executed exactly when BOTH other channels are already down. A
    # budget keeps "everything is degraded" from also meaning "request hangs":
    # on timeout the retriever logs + reports the failure like any other
    # degradation, it never hangs the caller. 0 disables the fallback outright.
    sql_fallback_timeout_s: float = 5.0
    # Upper bound on how stale a process's in-memory BM25 shard may be (R1).
    # ``MemoryBM25Index.invalidate()`` only clears the index IN THE PROCESS
    # THAT CALLED IT, so with several server workers a `rag ingest` served by
    # worker A leaves workers B..N serving the pre-ingest shard until they
    # restart. That is a correctness-shaped bug (stale answers, silently), and
    # the vector channel does NOT have it - Milvus is shared state, so new
    # chunks are visible to every worker immediately. Asymmetry: after an
    # ingest the two channels disagree about what exists.
    # A TTL bounds the disagreement window at the cost of one
    # list_chunks_for_user + re-tokenize per user per TTL. Rebuilding is
    # SEMANTICALLY IDENTICAL to invalidate-then-build (SQL is the truth), so
    # this can never change a result that was already correct - it can only
    # shorten how long a wrong one survives. 0 disables the TTL (index lives
    # until invalidated or the process exits).
    bm25_ttl_s: float = 300.0


@dataclass
class EmbeddingConfig:
    """DashScope-style endpoint (same contract as pi.llm.embedding).

    When url/key/model are all empty the kernel runs with a deterministic
    FakeEmbedder ONLY if explicitly constructed that way (tests); in
    production wiring, empty config means vector retrieval is disabled and
    the retriever degrades to BM25.
    """

    url: str = ""
    api_key: str = ""
    model: str = ""
    batch_size: int = 16
    timeout_s: float = 30.0
    # Retries apply ONLY to transient failures (connection errors, 429, 5xx) -
    # never to a ReadTimeout, because a slow endpoint is not a flaky one. The
    # vector channel is the only semantic path in the system, so a momentary
    # blip degrading a query to BM25-only costs real answer quality; a bounded
    # retry is much cheaper than that. 0 disables retries (one attempt).
    retries: int = 2
    retry_backoff_s: float = 0.5


@dataclass
class RagConfig:
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    # Storage
    sqlite_path: Path = field(
        default_factory=lambda: Path(os.environ.get("PI_RAG_SQLITE", str(Path.home() / ".pi-py" / "rag.sqlite3")))
    )
    milvus_uri: str = ""  # empty -> InMemoryVectorStore (standalone/tests)
    collection: str = "pi_rag_chunks"
    # Milvus search consistency. Measured on a 527-vector collection, per search:
    # Strong 399ms vs Bounded/Session/Eventually 4ms (identical hits) - i.e. the
    # read-after-write barrier, not the ANN scan, is ~99% of the cost
    # (tools/probe_local_stages.py). Strong is the default because ingest is
    # commonly followed by an immediate query in the same deployment ("why is my
    # just-uploaded doc not searchable?"). Set PI_RAG_MILVUS_CONSISTENCY=Bounded
    # (a few seconds of staleness, the usual production trade) or Session
    # (read-your-own-writes within one client; ingest and query must share it)
    # when query latency matters more than instant post-ingest visibility.
    milvus_consistency: str = "Strong"

    # Rerank endpoint (DashScope gte-rerank or self-hosted bge-reranker)
    rerank_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    # Per-attempt budget for ONE rerank batch (20 chunks ≈ 16k tokens took
    # ~519ms on a hosted endpoint, ~1.4s on a local 0.6B - so 15s is a generous
    # ceiling, not a target). Hardcoded it repeatedly turned a slow endpoint into
    # "rerank failed on every request", which reads like a model problem and is
    # really a timeout problem. Lower it if your endpoint is fast and you would
    # rather degrade than wait.
    rerank_timeout_s: float = 15.0
    # See EmbeddingConfig.retries. Defaults lower than embedding's because a
    # rerank failure degrades gracefully (fused order is kept) and a rerank
    # attempt is the most expensive single call in the query.
    rerank_retries: int = 1

    # Parser guards
    max_pdf_pages: int = 500  # per-file ceiling; bigger files -> needs_heavy_parser
    min_text_density: float = 50.0  # chars/page below this = likely scanned -> needs_heavy_parser

    @classmethod
    def from_env(cls) -> "RagConfig":
        borrowed: list[tuple[str, str]] = []
        cfg = cls(
            chunking=ChunkingConfig(
                max_chars=_env_int("PI_RAG_CHUNK_CHARS", 800),
                overlap_chars=_env_int("PI_RAG_CHUNK_OVERLAP", 120),
                min_chars=_env_int("PI_RAG_CHUNK_MIN", 100),
                contextual_prefix=_env_bool("PI_RAG_CONTEXTUAL", True),
                contextual_llm=_env_bool("PI_RAG_CONTEXTUAL_LLM", False),
            ),
            retrieval=RetrievalConfig(
                vector_k=_env_int("PI_RAG_VECTOR_K", 20),
                bm25_k=_env_int("PI_RAG_BM25_K", 20),
                final_k=_env_int("PI_RAG_TOP_K", 5),
                lexical_weight=_env_float("PI_RAG_LEXICAL_WEIGHT", 1.0),
                sql_fallback_timeout_s=_env_float("PI_RAG_SQL_FALLBACK_TIMEOUT", 5.0),
                bm25_ttl_s=_env_float("PI_RAG_BM25_TTL", 300.0),
                # Needed so a deployment without a rerank endpoint can turn the
                # P5 wiring warning OFF instead of being told to "set it at
                # wiring time" - an instruction env users could not follow.
                rerank_enabled=_env_bool("PI_RAG_RERANK_ENABLED", True),
            ),
            embedding=EmbeddingConfig(
                # Reuse the server-level embedding env vars (same endpoint
                # serves semantic memory); PI_RAG_* can override per-deploy.
                # Borrowing is reported below, not here - see _inherited().
                url=_inherited("PI_RAG_EMBEDDING_URL", "PI_EMBEDDING_URL", borrowed=borrowed),
                api_key=_inherited(
                    "PI_RAG_EMBEDDING_API_KEY", "PI_EMBEDDING_API_KEY", borrowed=borrowed
                ),
                model=_inherited("PI_RAG_EMBEDDING_MODEL", "PI_EMBEDDING_MODEL", borrowed=borrowed),
                batch_size=_env_int("PI_RAG_EMBED_BATCH", 16),
                timeout_s=_env_float("PI_RAG_EMBED_TIMEOUT", 30.0),
                retries=_env_int("PI_RAG_EMBED_RETRIES", 2),
                retry_backoff_s=_env_float("PI_RAG_HTTP_RETRY_BACKOFF", 0.5),
            ),
            milvus_uri=_inherited("PI_RAG_MILVUS_URI", "PI_MILVUS_URI", borrowed=borrowed),
            collection=os.environ.get("PI_RAG_COLLECTION", "pi_rag_chunks"),
            milvus_consistency=(
                os.environ.get("PI_RAG_MILVUS_CONSISTENCY", "Strong").strip() or "Strong"
            ),
            rerank_url=os.environ.get("PI_RAG_RERANK_URL", ""),
            rerank_api_key=os.environ.get("PI_RAG_RERANK_API_KEY", ""),
            rerank_model=os.environ.get("PI_RAG_RERANK_MODEL", ""),
            rerank_timeout_s=_env_float("PI_RAG_RERANK_TIMEOUT", 15.0),
            rerank_retries=_env_int("PI_RAG_RERANK_RETRIES", 1),
            max_pdf_pages=_env_int("PI_RAG_MAX_PDF_PAGES", 500),
            min_text_density=_env_float("PI_RAG_MIN_TEXT_DENSITY", 50.0),
        )
        # Report inheritance only when it can actually bite: without a Milvus
        # URI the vector channel is off and the borrowed model is never used, so
        # warning would just be noise in every BM25-only deployment (and a
        # warning that always fires is a warning nobody reads).
        if cfg.milvus_uri:
            for primary, fallback in borrowed:
                _log.warning(
                    "rag: %s is unset, inheriting %s from the server/memory pipeline. "
                    "Confirm it matches the model the %s projection was built with, or "
                    "the vectors will drift silently (see tools/rebuild_eval_index.py "
                    "--check).",
                    primary,
                    fallback,
                    cfg.collection,
                )
        return cfg

    def vector_enabled(self) -> bool:
        return bool(self.embedding.url and self.embedding.api_key and self.embedding.model)

    def rerank_enabled(self) -> bool:
        return bool(self.retrieval.rerank_enabled and self.rerank_url)
