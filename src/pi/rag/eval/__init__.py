"""Evaluation harness: golden sets, retrieval metrics, A/B reports.

铁律：先建评测，再调检索。Build the golden set and prove the rig against a
mock retriever (M0) before writing real retrieval code (M3).
"""

from pi.rag.eval.harness import (
    KS,
    MAX_ANY_OF_GOLD,
    CaseScore,
    EvalReport,
    GoldenQA,
    GoldenSet,
    ab_markdown,
    aggregate,
    chunk_key,
    hit_at_k,
    reciprocal_rank,
    recall_at_k,
)
from pi.rag.eval.runner import EvalRunner

__all__ = [
    "KS",
    "MAX_ANY_OF_GOLD",
    "CaseScore",
    "EvalReport",
    "EvalRunner",
    "GoldenQA",
    "GoldenSet",
    "ab_markdown",
    "aggregate",
    "chunk_key",
    "hit_at_k",
    "reciprocal_rank",
    "recall_at_k",
]
