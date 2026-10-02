"""Shared HTTP POST helper with BOUNDED retries for the remote backends.

Both remote backends (embedding, reranker) sit on the user-visible critical path
when the deployment uses a hosted endpoint. Before this module a single transient
429/502/connection reset ended as "vector channel unavailable" -> BM25-only
answers at a materially lower hit@5, with nothing but a WARNING log to show for
it: a network blip silently became a quality regression. A bounded retry is the
cheapest fix for that, and it is the reason ``EmbeddingConfig.retries`` exists.

Deliberately NOT retried:

* ``httpx.ReadTimeout`` - the endpoint is *slow*, not flaky. Retrying spends
  another full timeout on the caller's critical path. This repo has already paid
  for that lesson: the hardcoded 15s rerank timeout turned a slow local model
  into "rerank failed" on every request.
* 4xx other than 429/408/425 - an invalid key or a malformed request fails
  identically forever; retrying only burns latency.

Connection-level failures ARE retried (no server-side work has started), as are
429 and 5xx. The last failure is re-raised as an ``httpx.HTTPError`` so callers
keep their existing `except httpx.HTTPError` translation into a domain error.
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

log = logging.getLogger("pi.rag.defaults.http")

# Statuses worth a second attempt: rate limiting, request timeout, and the
# server-side faults that are usually momentary.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Connection-level: the request never reached (or never completed at) the model,
# so a retry cannot duplicate work. ReadTimeout is intentionally absent.
RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

MAX_BACKOFF_S = 8.0


def _backoff_s(attempt: int, base_s: float) -> float:
    """Exponential backoff with jitter, capped (attempt is 0-based).

    Jitter matters when several workers hit the same 429 at the same instant -
    synchronized retries would rebuild the very spike that caused the 429.
    """
    return min(base_s * (2.0**attempt), MAX_BACKOFF_S) * (0.6 + 0.8 * random.random())


async def post_json_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    attempts: int,
    base_backoff_s: float = 0.5,
    label: str = "http",
) -> httpx.Response:
    """POST ``payload`` as JSON, retrying transient failures up to ``attempts``.

    Returns the response for ANY status the caller must interpret itself
    (including non-retryable 4xx and, notably, an exhausted retryable status -
    the caller's own status handling stays in charge there). Only transport
    failures that were not retryable, or that ran out of attempts, are raised.
    """
    tries = max(1, int(attempts))
    last: httpx.HTTPError | None = None
    for attempt in range(tries):
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            if not isinstance(exc, RETRYABLE_EXC) or attempt == tries - 1:
                raise
            last = exc
        else:
            if resp.status_code == 200 or resp.status_code not in RETRYABLE_STATUS:
                return resp
            # Retryable status. Keep the response so the caller can still read
            # the vendor body if this was the final attempt.
            last = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
            if attempt == tries - 1:
                return resp
        delay = _backoff_s(attempt, base_backoff_s)
        log.warning(
            "%s attempt %d/%d failed (%s); retrying in %.2fs",
            label, attempt + 1, tries, last, delay,
        )
        await asyncio.sleep(delay)
    raise last if last is not None else RuntimeError(f"{label}: no attempt was made")
