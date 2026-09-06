"""Cross-encoder reranking for the second stage of memory retrieval.

Vector recall ranks by the angle between two independently-embedded texts. That is
cheap but too weak to gate on: against the live embedding model an off-topic query
scored 0.294 against "后端框架是 FastAPI", while a genuinely relevant query about
testing conventions scored only 0.447 against its own fact. The distributions
overlap, so no cosine threshold separates them on its own.

A reranker reads query and document together instead of comparing their vectors. Over
20 stored facts and 7 queries it scored off-topic pairs at most 0.044 against
relevant ones from 0.30 up - a clean gap, which is what makes the second stage worth
its measured ~135ms and ~630 tokens per turn. The visible effect: single-stage
retrieval injected 5 irrelevant facts for an off-topic prompt, two-stage injected
none.

It is a filter, not an oracle. On that same sample it ranked two merely
convention-shaped facts above the fact actually about testing conventions, so the
ordering *within* the kept set is approximate. That is acceptable because retrieve()
injects a top-k set under a "treat as background" header rather than one best answer
- but nothing downstream should assume rank #1 is the most relevant fact.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from pi.models import Usage

log = logging.getLogger("pi.memory.rerank")


class Reranker(Protocol):
    async def rerank(
        self, query: str, documents: Sequence[str]
    ) -> tuple[list[float], Usage]:
        """One relevance score per document, in input order, plus token usage.

        Scores are in the model's own range and are only comparable to each other,
        so the caller gates on a configured threshold rather than on cosine's scale.
        """

    async def close(self) -> None:
        """Release the underlying HTTP client."""


class DashScopeReranker:
    """Alibaba MaaS / DashScope native rerank endpoint.

    Not OpenAI-compatible, so it cannot reuse the embedder's client: the request
    nests the payload under `input` and the scores come back under `output.results`,
    keyed by the document's original index and sorted by score. `top_n` is set to the
    document count so nothing is silently dropped before the caller has scored it.
    """

    def __init__(
        self,
        url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 5.0,
    ):
        self.url = url
        self.model = model
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key or os.environ.get('OPENAI_API_KEY', '')}",
                "Content-Type": "application/json",
            },
        )

    async def rerank(
        self, query: str, documents: Sequence[str]
    ) -> tuple[list[float], Usage]:
        docs = list(documents)
        if not docs:
            return [], Usage()
        resp = await self._client.post(
            self.url,
            json={
                "model": self.model,
                "input": {"query": query, "documents": docs},
                "parameters": {"return_documents": False, "top_n": len(docs)},
            },
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        results = (body.get("output") or {}).get("results") or []

        # Scatter by index rather than trusting the response order: the API sorts by
        # score, and a caller that zipped it against `documents` would silently
        # attach every score to the wrong fact.
        scores = [0.0] * len(docs)
        for item in results:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(scores):
                score = item.get("relevance_score")
                scores[idx] = float(score) if isinstance(score, (int, float)) else 0.0

        usage = body.get("usage") or {}
        tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        return scores, Usage(input_tokens=int(tokens), output_tokens=0)

    async def close(self) -> None:
        await self._client.aclose()


def get_reranker(url: str, model: str) -> Reranker | None:
    """None when not configured - retrieval then runs on vector recall alone.

    Reranking costs a round trip on the per-turn hot path, so a deployment that has
    not opted in pays nothing and still works; this is an upgrade, not a dependency.
    """
    if not url or not model:
        return None
    return DashScopeReranker(url, model)
