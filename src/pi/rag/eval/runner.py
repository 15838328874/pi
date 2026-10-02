"""Eval runner: push a golden set through any retriever, produce metrics.

The runner is retriever-agnostic on purpose: it takes a plain async
``search(user_id, query, k) -> list[RetrievedChunk]`` callable plus a
ChunkStore (to map chunk_id -> stable chunk_key). That means the SAME
runner scores:
  - a real HybridRetriever (M3),
  - a deliberately-crippled variant (vector-only / bm25-only) for A/B,
  - a mock in unit tests (M0),
without the runner knowing or caring which.

This is how we honor "先建评测再调检索": the measurement rig exists and is
proven against a mock BEFORE any real retrieval code is written.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from pi.rag.eval.harness import (
    CaseScore,
    EvalReport,
    GoldenSet,
    aggregate,
    chunk_key,
    hit_at_k,
    reciprocal_rank,
    KS,
    recall_at_k,
)
from pi.rag.protocols import ChunkStore
from pi.rag.types import RetrievedChunk

log = logging.getLogger("pi.rag.eval.runner")

SearchFn = Callable[[int, str, int], Awaitable[list[RetrievedChunk]]]


class EvalRunner:
    def __init__(self, store: ChunkStore, k_max: int = max(KS)) -> None:
        self.store = store
        self.k_max = k_max

    async def _keys_for(self, hits: list[RetrievedChunk]) -> list[str]:
        """Map ranked hits -> ranked chunk_keys (doc_key#seq), order preserved."""
        if not hits:
            return []
        ids = [h.chunk_id for h in hits]
        chunks = await self.store.get_chunks_by_ids(ids)
        by_id = {c.chunk_id: chunk_key(c.doc_key, c.seq) for c in chunks}
        # Preserve ranking; drop ids that vanished from the store (stale index).
        return [by_id[h.chunk_id] for h in hits if h.chunk_id in by_id]

    async def run(self, golden: GoldenSet, search: SearchFn, config_name: str) -> EvalReport:
        scores: list[CaseScore] = []
        for case in golden.cases:
            error: str | None = None
            ranked: list[str] = []
            try:
                hits = await search(case.user_id, case.query, self.k_max)
                ranked = await self._keys_for(hits)
            except Exception as exc:  # noqa: BLE001 - a broken config must score 0, not crash the eval
                error = f"{type(exc).__name__}: {exc}"
                log.warning("eval case %s errored: %s", case.id, error)

            gold = set(case.gold_chunk_keys)
            scores.append(
                CaseScore(
                    case_id=case.id,
                    category=case.category,
                    ranked_keys=ranked,
                    recall_at={k: recall_at_k(ranked, gold, k) for k in KS},
                    hit_at={k: hit_at_k(ranked, gold, k) for k in KS},
                    rr=reciprocal_rank(ranked, gold),
                    error=error,
                    # tags must travel with the score or per_tag slicing is empty
                    tags=list(case.tags),
                )
            )
        return aggregate(config_name, scores)
