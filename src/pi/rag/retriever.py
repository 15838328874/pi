"""Hybrid retriever: vector + BM25 -> RRF -> rerank -> hydrate (M3).

Read-path counterpart of ``ingest.py``. It owns exactly two things the stores
must not:

1. **Fusion.** The two channels rank the same corpus on incomparable scales
   (cosine similarity vs BM25 term weight). RRF (``Σ 1/(k + rank)``, k=60 per
   Cormack et al.) merges them by RANK, so neither channel's raw magnitude can
   dominate - the constant lives in config with its citation.

2. **Degradation.** House rule (对接文档 §7 #4): 附属系统失败不挂主流程，但要发噪音.
   A dead Milvus or a dead embedding endpoint must never炸 the run: retrieval
   steps down hybrid -> vector_only -> bm25_fallback -> sql_fallback -> empty,
   and EVERY step is reported through ``UsageHooks.on_retrieval`` plus a WARNING
   log. Silent degradation is how a RAG system rots - answers get worse, nobody
   notices, and 归因 becomes impossible (先定性再归因 needs the outcome string to
   tell 真崩溃 / 自报错 / 配置未开 apart).

   Two distinctions the outcome vocabulary must preserve:
     - **errored vs empty**: a healthy channel returning zero hits is an answer
       ("nothing is semantically close"), NOT a failure. Only an errored or
       unconfigured channel triggers a fallback; otherwise we would invent hits
       and mislabel a clean miss as a degradation.
     - **embed_failed vs bm25_fallback**: the former bills nothing and means the
       endpoint died; the latter means the vector capability was absent/broken.

Truth-source discipline: the vector index is a disposable projection, so a
vector hit is only an ID. Every result is hydrated through ``ChunkStore``
(SQL) - text and citations always come from the truth, and a stale vector can
at worst return nothing, never wrong bytes. Hydration also re-checks
``user_id``: the 0-泄漏 guarantee must not rest on a single layer behaving.

Portability: no ``pi.tools`` / ``pi.server`` / ``fastapi`` imports here. The pi
integration injects real backends via ``pi.rag.adapters`` (M5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pi.rag.config import RRF_K, RagConfig, RetrievalConfig
from pi.rag.protocols import (
    ChunkStore,
    Embedder,
    LexicalIndex,
    RagVectorStore,
    Reranker,
    UsageHooks,
)
from pi.rag.types import Chunk, RetrievedChunk, RetrievalMode, RetrievalResult

log = logging.getLogger("pi.rag.retriever")

# ---------------------------------------------------------------------------
# Outcome vocabulary reported to UsageHooks.on_retrieval. Constants so the set
# is greppable and stays aligned with the semantic-memory precedent
# (pi/server/db.py: vector_hit / embed_failed / lexical_fallback / no_hits).
# ---------------------------------------------------------------------------
OUTCOME_HYBRID_OK = "hybrid_ok"
OUTCOME_VECTOR_ONLY = "vector_only"
OUTCOME_BM25_FALLBACK = "bm25_fallback"
OUTCOME_EMBED_FAILED = "embed_failed"
OUTCOME_SQL_FALLBACK = "sql_fallback"
OUTCOME_RERANK_FAILED = "rerank_failed"
OUTCOME_NO_HITS = "no_hits"
OUTCOME_ERROR = "error"

# Channel health (internal; never reported verbatim - mapped to outcomes above).
_CH_OK = "ok"
_CH_UNCONFIGURED = "unconfigured"
_CH_EMBED_FAILED = "embed_failed"
_CH_STORE_FAILED = "store_failed"


def rrf_fuse(
    rankings: list[list[int]],
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over any number of ranked id lists.

    ``score(d) = Σ_rankings w_i / (k + rank_i(d))``, rank 1-based. A doc missing
    from a ranking contributes nothing, so channels of different lengths fuse
    sanely.

    ``weights`` defaults to all-1.0, which is the original Cormack et al. form
    and the shipped behaviour. The knob exists because RRF's central assumption
    - that every ranking is a roughly equally trustworthy opinion - is measurably
    false on a real corpus: M4 found the gold's mean rank was 1.53 in the vector
    channel vs 2.63 in BM25, BM25 uniquely found the gold on 0 of 60 queries, and
    because fusion averages ranks the weaker second opinion demoted the gold on 11
    queries while lifting it on 9 (recall@5 0.983 vector-only -> 0.950 fused).
    Down-weighting is the only fusion-side lever that addresses this; a score floor
    does NOT, because RRF consumes ranks and never looks at magnitudes (measured:
    gating BM25 hits below 20%/35% of the top score changed recall@5 by exactly
    0.000).

    Do not change the default without an evals/reports A/B showing a win: the
    right weight is corpus-dependent, and 1.0 is the neutral, literature value.

    Ties break on ASCENDING chunk_id rather than insertion order: eval runs and
    A/B sweeps must be reproducible, and dict-iteration order would make
    Recall@k jitter between runs on tied scores.
    """
    if k <= 0:
        raise ValueError(f"rrf k must be positive, got {k}")
    if weights is not None:
        if len(weights) != len(rankings):
            raise ValueError(
                f"rrf weights must match rankings: {len(weights)} vs {len(rankings)}"
            )
        if any(float(w) < 0 for w in weights):
            raise ValueError(f"rrf weights must be non-negative, got {list(weights)}")
    scores: dict[int, float] = {}
    for i, ranking in enumerate(rankings):
        w = 1.0 if weights is None else float(weights[i])
        if w == 0.0:
            continue  # a channel weighted to zero must not touch the ranking
        for rank, cid in enumerate(ranking, start=1):
            cid = int(cid)
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass
class _Plan:
    """What the two channels produced and which pipeline will serve it."""

    mode: RetrievalMode = RetrievalMode.EMPTY
    outcome: str = OUTCOME_NO_HITS
    degraded: bool = False
    ranked: list[tuple[int, float]] = field(default_factory=list)
    need_sql: bool = False  # both channels unusable -> SQL LIKE last resort


class HybridRetriever:
    """Two-channel hybrid retrieval with a non-silent degradation chain.

    Everything is injected (Protocol-typed), so the same class serves:
      - production: MysqlChunkStore + MilvusRagVectorStore + HttpEmbedder
        + HttpReranker + MemoryBM25Index,
      - standalone: SqliteChunkStore + InMemoryVectorStore, no reranker,
      - tests: fakes with zero infra.

    ``search_chunks`` matches the eval runner's ``SearchFn`` signature
    (``(user_id, query, k) -> list[RetrievedChunk]``) so the shipped retriever
    is literally the measured one - that is deliberate, not a convenience.
    """

    def __init__(
        self,
        store: ChunkStore,
        *,
        embedder: Embedder | None = None,
        vector_store: RagVectorStore | None = None,
        lexical_index: LexicalIndex | None = None,
        reranker: Reranker | None = None,
        config: RagConfig | None = None,
        hooks: UsageHooks | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.vector_store = vector_store
        self.lexical_index = lexical_index
        self.reranker = reranker
        self.config = config or RagConfig()
        self.hooks = hooks

    # -- public API ---------------------------------------------------------

    async def search(
        self,
        user_id: int,
        query: str,
        k: int | None = None,
        doc_keys: list[str] | None = None,
    ) -> RetrievalResult:
        """Retrieve top-k chunks for ONE user, with citations. Never raises.

        ACL: ``user_id`` is untrusted caller input (pi: ToolContext.user_db_id);
        it is pushed into every channel (Milvus int filter, BM25 shard, SQL
        WHERE) and re-verified at hydration.

        ``doc_keys`` optionally narrows to a subset of the user's docs. The
        vector channel filters natively; the lexical/SQL channels have no doc
        dimension, so their results are post-filtered (with over-fetch so
        filtering cannot silently truncate the answer).
        """
        t0 = time.perf_counter()
        uid = int(user_id)
        rc: RetrievalConfig = self.config.retrieval
        final_k = max(1, int(k or rc.final_k))
        q = (query or "").strip()
        if not q:
            return await self._finish([], RetrievalMode.EMPTY, OUTCOME_NO_HITS, False, t0)

        # 1) run both channels CONCURRENTLY - latency of the slower, not the sum.
        #    Neither channel raises (both return a health state), so gather is
        #    safe here; sequential awaits would have paid the remote embedding
        #    round trip AND the local BM25 scan back to back.
        (vec_hits, vec_state, vec_err), (lex_hits, lex_state, lex_err) = await asyncio.gather(
            self._vector_channel(uid, q, rc, doc_keys),
            self._lexical_channel(uid, q, rc),
        )
        if vec_err:
            log.warning(
                "rag vector channel unavailable (user=%s state=%s): %s", uid, vec_state, vec_err
            )
        if lex_err:
            log.warning("rag lexical channel unavailable (user=%s state=%s): %s",
                        uid, lex_state, lex_err)

        # 2) decide what served, and whether the truth (SQL) is the last resort
        plan = self._plan(vec_hits, vec_state, lex_hits, lex_state, rc)

        if plan.need_sql:
            sql_hits = await self._sql_fallback(uid, q, rc)
            if sql_hits is None:  # even the truth is unreachable
                return await self._finish([], RetrievalMode.EMPTY, OUTCOME_ERROR, True, t0)
            hits = self._scope(sql_hits, doc_keys)[:final_k]
            # Always sql_fallback + degraded, even with zero hits: we only get
            # here because BOTH ranked channels were unusable, and that is the
            # operator-actionable signal. Reporting "no_hits" instead would
            # hide an outage behind an innocent-looking empty result (归因
            # needs 真崩溃 / 自报错 / 配置未开 to stay distinguishable).
            return await self._finish(
                hits, RetrievalMode.SQL_FALLBACK, OUTCOME_SQL_FALLBACK, True, t0
            )

        if not plan.ranked:
            return await self._finish([], plan.mode, plan.outcome, plan.degraded, t0)

        # 3) hydrate ids -> chunks through SQL (truth + defensive ACL).
        #    With doc_keys we hydrate the whole fused window (<= vector_k +
        #    bm25_k ids, one IN query) because post-filtering a truncated list
        #    would silently under-deliver.
        cap = len(plan.ranked) if doc_keys else self._hydrate_cap(rc, final_k)
        scored = dict(plan.ranked)
        candidates = await self._hydrate(uid, [cid for cid, _ in plan.ranked[:cap]], scored)
        candidates = self._scope(candidates, doc_keys)
        if not candidates:
            # Ids existed in an index but are gone from SQL (stale projection)
            # or filtered out by doc_keys: a miss, not a crash.
            return await self._finish([], plan.mode, OUTCOME_NO_HITS, plan.degraded, t0)

        # 4) optional cross-encoder rerank (precision stage)
        hits, outcome, degraded = await self._maybe_rerank(
            uid, q, candidates, rc, final_k, plan.outcome, plan.degraded
        )
        if not hits:
            outcome = OUTCOME_NO_HITS
        return await self._finish(hits, plan.mode, outcome, degraded, t0)

    async def search_chunks(
        self,
        user_id: int,
        query: str,
        k: int | None = None,
        doc_keys: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Hits only - the exact shape ``EvalRunner`` consumes."""
        res = await self.search(user_id, query, k, doc_keys=doc_keys)
        return res.chunks

    # -- channels -----------------------------------------------------------

    async def _vector_channel(self, uid: int, query: str, rc: RetrievalConfig,
                              doc_keys: list[str] | None):
        """-> (hits, state, error_str). Never raises."""
        if self.embedder is None or self.vector_store is None:
            # Capability off by configuration, not a failure -> INFO, not WARNING.
            log.info(
                "rag vector channel not configured (embedder=%s vector_store=%s)",
                self.embedder is not None, self.vector_store is not None,
            )
            return [], _CH_UNCONFIGURED, ""
        try:
            emb = await self.embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001 - degrade, never炸 the run
            return [], _CH_EMBED_FAILED, f"{type(exc).__name__}: {exc}"
        await self._report_usage(uid, getattr(emb, "usage_tokens", 0) or 0, "embedding")
        vectors = list(getattr(emb, "vectors", None) or [])
        if not vectors or not vectors[0]:
            return [], _CH_EMBED_FAILED, "embedder returned no vector"
        try:
            hits = await self.vector_store.search(
                uid, vectors[0], max(1, int(rc.vector_k)), doc_keys
            )
        except Exception as exc:  # noqa: BLE001
            return [], _CH_STORE_FAILED, f"{type(exc).__name__}: {exc}"
        return [(int(cid), float(s)) for cid, s in (hits or [])], _CH_OK, ""

    async def _lexical_channel(self, uid: int, query: str, rc: RetrievalConfig):
        """-> (hits, state, error_str). Never raises."""
        if self.lexical_index is None:
            log.info("rag lexical channel not configured")
            return [], _CH_UNCONFIGURED, ""
        try:
            hits = await self.lexical_index.search(uid, query, max(1, int(rc.bm25_k)))
            return [(int(cid), float(s)) for cid, s in (hits or [])], _CH_OK, ""
        except Exception as exc:  # noqa: BLE001
            return [], _CH_STORE_FAILED, f"{type(exc).__name__}: {exc}"

    # -- planning -----------------------------------------------------------

    def _plan(self, vec_hits, vec_state, lex_hits, lex_state, rc: RetrievalConfig) -> _Plan:
        """Map channel health -> mode/outcome/fused ranking.

        Decision table (both channels are attempted whenever configured):

        | vector | lexical | serves            | outcome                  |
        |--------|---------|-------------------|--------------------------|
        | ok     | ok      | RRF fusion        | hybrid_ok                |
        | ok     | down    | vector only       | vector_only              |
        | down   | ok      | bm25 only         | embed_failed/bm25_fallback|
        | down   | down    | SQL LIKE          | sql_fallback             |

        "down" = errored OR unconfigured. A healthy-but-empty channel does NOT
        count as down, so a clean miss stays a miss instead of triggering a
        fallback that would fabricate relevance.
        """
        k = int(rc.rrf_k or RRF_K)
        vec_ok = vec_state == _CH_OK
        lex_ok = lex_state == _CH_OK
        both_ok = vec_ok and lex_ok

        if both_ok:
            if vec_hits or lex_hits:
                # lexical_weight is clamped rather than validated: a bad env
                # value must degrade fusion, never炸 a user's query. RRF raises on
                # negatives, and retrieval must not raise.
                w = min(max(float(rc.lexical_weight), 0.0), 10.0)
                ranked = rrf_fuse(
                    [[cid for cid, _ in vec_hits], [cid for cid, _ in lex_hits]],
                    k=k,
                    weights=[1.0, w],
                )
                return _Plan(RetrievalMode.HYBRID, OUTCOME_HYBRID_OK, False, ranked)
            # both healthy, nothing matched -> a real "no hits", not degraded
            return _Plan(RetrievalMode.EMPTY, OUTCOME_NO_HITS, False, [])

        if vec_ok:  # lexical down -> vector carries it
            if vec_hits:
                return _Plan(
                    RetrievalMode.VECTOR_ONLY, OUTCOME_VECTOR_ONLY, True, list(vec_hits)
                )
            return _Plan(RetrievalMode.EMPTY, OUTCOME_NO_HITS, True, [], need_sql=True)

        if lex_ok and lex_hits:  # vector down -> 词法兜底
            outcome = (
                OUTCOME_EMBED_FAILED if vec_state == _CH_EMBED_FAILED else OUTCOME_BM25_FALLBACK
            )
            return _Plan(RetrievalMode.BM25_FALLBACK, outcome, True, list(lex_hits))

        # nothing usable from either channel
        return _Plan(RetrievalMode.SQL_FALLBACK, OUTCOME_SQL_FALLBACK, True, [], need_sql=True)

    def _hydrate_cap(self, rc: RetrievalConfig, final_k: int) -> int:
        if self._will_rerank(rc):
            return max(final_k, int(rc.rerank_candidates))
        return final_k

    async def _sql_fallback(self, uid: int, query: str,
                            rc: RetrievalConfig) -> list[RetrievedChunk] | None:
        """Last resort: SQL LIKE against the truth. None = store also failed.

        Budgeted (R5): LIKE '%q%' cannot use an index, so this is a full scan
        executed exactly when both real channels are already down - without a
        deadline, "everything is degraded" would also mean "request hangs".
        A timeout is logged and reported like any other failure.
        """
        budget = float(getattr(rc, "sql_fallback_timeout_s", 5.0) or 0)
        try:
            if budget <= 0:
                # 0 = fallback disabled by config - same reporting path as a
                # store failure, never a silent empty result.
                log.warning("rag SQL fallback disabled (sql_fallback_timeout_s<=0, user=%s)", uid)
                return None
            hits = await asyncio.wait_for(
                self.store.search_text(uid, query, max(1, int(rc.sql_fallback_k))),
                timeout=budget,
            )
            return list(hits or [])
        except asyncio.TimeoutError:
            log.error("rag SQL fallback timed out after %.1fs (user=%s) - "
                      "LIKE full-scan on a large rag_chunks?", budget, uid)
            return None
        except Exception as exc:  # noqa: BLE001 - nothing left to degrade to
            log.error("rag SQL fallback failed (user=%s): %s", uid, exc, exc_info=True)
            return None

    # -- hydration / scoping ------------------------------------------------

    async def _hydrate(self, uid: int, chunk_ids: list[int],
                       scores: dict[int, float]) -> list[RetrievedChunk]:
        """chunk_ids -> RetrievedChunk with citations, ranking order preserved.

        Three guarantees this method exists to provide:
          - text comes from SQL (truth), never from the vector index;
          - ids that vanished from SQL are DROPPED (a stale projection yields
            fewer hits, never wrong hits);
          - ``user_id`` is re-verified per row, so a cross-tenant leak would
            need BOTH the channel filter AND this check to fail.
        """
        ids = [int(c) for c in chunk_ids if c is not None]
        if not ids:
            return []
        try:
            chunks: list[Chunk] = await self.store.get_chunks_by_ids(ids)
        except Exception as exc:  # noqa: BLE001
            log.error("rag hydration failed (user=%s): %s", uid, exc, exc_info=True)
            return []

        by_id: dict[int, Chunk] = {}
        leaked: list[int] = []
        for c in chunks:
            if int(c.user_id) == uid:
                by_id[int(c.chunk_id)] = c
            else:
                leaked.append(int(c.chunk_id))
        if leaked:
            # Unreachable unless a channel's ACL filter broke. Loud on purpose:
            # the alternative is a silent multi-tenant breach.
            log.error(
                "rag ACL VIOLATION blocked at hydration: user=%s got chunks %s", uid, leaked
            )

        docs = await self._doc_citations(uid, {c.doc_key for c in by_id.values()})
        out: list[RetrievedChunk] = []
        for cid in ids:  # iterate the RANKING, not the store's row order
            ch = by_id.get(cid)
            if ch is None:
                continue
            meta = docs.get(ch.doc_key, {})
            out.append(
                RetrievedChunk(
                    chunk_id=int(ch.chunk_id),
                    doc_key=ch.doc_key,
                    text=ch.text,
                    score=float(scores.get(cid, 0.0)),
                    title=meta.get("title", ""),
                    title_path=ch.title_path or "",
                    source=meta.get("source_path") or "",
                    page=ch.page,
                )
            )
        return self._fill_scores(out)

    @staticmethod
    def _fill_scores(hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Give zero-scored hits a decreasing positional score.

        RetrievalResult.score is documented as comparable only WITHIN one
        result, so a positional value is honest - and it keeps downstream
        sorting (and any caller that re-sorts by score) stable instead of
        collapsing ties to 0.0. Real scores (RRF / rerank) are never touched.
        """
        n = len(hits)
        for i, h in enumerate(hits):
            if not h.score:
                h.score = float(n - i)
        return hits

    async def _doc_citations(self, uid: int, doc_keys: set[str]) -> dict[str, dict]:
        """doc_key -> {title, source_path} for 引用溯源.

        One ``get_doc`` per DISTINCT doc in the window (typically 1-3), gathered
        concurrently. Deliberately not ``list_docs`` (would pull the user's
        whole corpus to cite five chunks) and not a per-chunk lookup (N+1).
        Best-effort: a failed doc row degrades to empty citation fields, never
        to a failed retrieval.
        """
        keys = sorted(k for k in doc_keys if k)
        if not keys:
            return {}

        async def one(dk: str) -> tuple[str, dict]:
            try:
                row = await self.store.get_doc(uid, dk)
            except Exception:  # noqa: BLE001 - citations are a nicety
                log.warning("rag citation lookup failed for doc_key=%s", dk, exc_info=True)
                return dk, {}
            if not row:
                return dk, {}
            return dk, {
                "title": row.get("title") or "",
                "source_path": row.get("source_path") or "",
            }

        return dict(await asyncio.gather(*(one(dk) for dk in keys)))

    @staticmethod
    def _scope(hits: list[RetrievedChunk], doc_keys: list[str] | None) -> list[RetrievedChunk]:
        """Post-filter to allowed doc_keys (lexical/SQL channels are doc-blind)."""
        if not doc_keys:
            return hits
        allow = set(doc_keys)
        return [h for h in hits if h.doc_key in allow]

    # -- rerank -------------------------------------------------------------

    def _will_rerank(self, rc: RetrievalConfig) -> bool:
        return bool(self.reranker is not None and rc.rerank_enabled)

    async def _maybe_rerank(self, uid: int, query: str, hits: list[RetrievedChunk],
                            rc: RetrievalConfig, final_k: int, outcome: str,
                            degraded: bool) -> tuple[list[RetrievedChunk], str, bool]:
        """Cross-encoder precision stage. Failure keeps RRF order; never raises.

        Rerank is a refinement, not a channel: when the endpoint dies the right
        behavior is to serve the fused ranking unchanged and SAY SO
        (outcome=rerank_failed) - not to fall back further and lose hits the
        user could have had.
        """
        if not self._will_rerank(rc):
            return hits[:final_k], outcome, degraded

        window = hits[: max(final_k, int(rc.rerank_candidates))]
        try:
            scored = await self.reranker.rerank(query, window)
        except Exception as exc:  # noqa: BLE001 - 附属系统失败不挂主流程
            log.warning(
                "rag rerank failed (user=%s), serving fused order: %s",
                uid, f"{type(exc).__name__}: {exc}",
            )
            # rerank_failed becomes the headline only when nothing worse
            # happened; a retrieval-path degradation is more useful for 归因.
            headline = outcome if degraded else OUTCOME_RERANK_FAILED
            return hits[:final_k], headline, True

        await self._report_usage(
            uid, int(getattr(getattr(self.reranker, "last_usage", None), "usage_tokens", 0) or 0),
            "rerank",
        )
        out = list(scored or [])[:final_k]
        if not out:
            # Reranker returned nothing usable - keep the fused hits rather than
            # serving empty (a degradation must not lose data).
            log.warning("rag rerank returned no results (user=%s), keeping fused order", uid)
            return hits[:final_k], OUTCOME_RERANK_FAILED, True
        return out, outcome, degraded

    # -- plumbing -----------------------------------------------------------

    async def _finish(self, hits: list[RetrievedChunk], mode: RetrievalMode, outcome: str,
                      degraded: bool, t0: float) -> RetrievalResult:
        """Build the result and emit exactly ONE on_retrieval event."""
        duration = time.perf_counter() - t0
        await self._report_retrieval(outcome, duration)
        if degraded:
            log.warning(
                "rag retrieval DEGRADED (mode=%s outcome=%s hits=%d %.0fms)",
                mode.value, outcome, len(hits), duration * 1000,
            )
        return RetrievalResult(
            chunks=hits,
            mode=mode,
            degraded=degraded,
            duration_ms=int(duration * 1000),
            outcome=outcome,
        )

    async def _report_retrieval(self, outcome: str, duration_s: float) -> None:
        if self.hooks is None:
            return
        try:
            await self.hooks.on_retrieval(outcome, duration_s)
        except Exception:  # noqa: BLE001 - hooks never raise into retrieval
            log.debug("on_retrieval hook failed", exc_info=True)

    async def _report_usage(self, uid: int, tokens: int, kind: str) -> None:
        if self.hooks is None or tokens <= 0:
            return
        try:
            await self.hooks.on_embed_usage(uid, int(tokens), kind)
        except Exception:  # noqa: BLE001
            log.debug("on_embed_usage hook failed", exc_info=True)
