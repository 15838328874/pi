"""HttpEmbedder wire-format tests (OpenAI-compatible AND DashScope-native).

Uses httpx.MockTransport - no network, no real API key. This is the layer
that talks to the Aliyun MaaS endpoint the user configured, so both wire
shapes must be proven correct before we ever call it for real.

House discipline: tests assert the REQUEST body shape too (not just that a
parse succeeded), because sending the wrong shape is exactly the failure
mode we hit when the vendor offers two APIs on two paths.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from pi.rag.defaults.http_embedder import (
    STYLE_DASHSCOPE,
    STYLE_OPENAI,
    EmbeddingError,
    HttpEmbedder,
    _infer_style,
)

OPENAI_URL = "https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/embeddings"
DASHSCOPE_URL = "https://example.cn-beijing.maas.aliyuncs.com/api/v1/services/embeddings"
KEY = "***"

seen: dict[str, object] = {}


def _openai_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    seen["auth"] = request.headers.get("Authorization")
    seen["body"] = body
    texts = body.get("input")
    if not isinstance(texts, list):  # wrong shape -> prove we sent a list
        return httpx.Response(400, json={"error": {"message": "input must be a list"}})
    data = [
        {"index": i, "object": "embedding", "embedding": [float(i) / 10, 0.5, -1.0]}
        for i in range(len(texts))
    ]
    return httpx.Response(
        200,
        json={"object": "list", "model": body.get("model"), "data": data,
              "usage": {"prompt_tokens": 7, "total_tokens": 7}},
    )


def _dashscope_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    seen["auth"] = request.headers.get("Authorization")
    seen["body"] = body
    texts = body.get("input", {}).get("texts")
    if not isinstance(texts, list):
        return httpx.Response(400, json={"message": "input.texts must be a list"})
    # Deliberately return OUT OF ORDER to prove we re-sort by text_index.
    entries = [
        {"text_index": i, "embedding": [float(i) / 10, 0.5, -1.0]} for i in range(len(texts))
    ]
    entries.reverse()
    return httpx.Response(
        200,
        json={"output": {"embeddings": entries}, "usage": {"total_tokens": 12}},
    )


def _err_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "Invalid API-key provided."}})


def _count_mismatch_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})


def _nonlist_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": ["nan"]}]})


def _empty_vec_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"index": 0, "embedding": []}]})


def _dup_index_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}]},
    )


def test_style_inference_from_url():
    assert _infer_style(OPENAI_URL) == STYLE_OPENAI
    assert _infer_style(DASHSCOPE_URL) == STYLE_DASHSCOPE
    # explicit style wins over URL inference
    e = HttpEmbedder(OPENAI_URL, KEY, "m", style="dashscope")
    assert e.style == STYLE_DASHSCOPE
    e2 = HttpEmbedder(DASHSCOPE_URL, KEY, "m", style="openai")
    assert e2.style == STYLE_OPENAI
    # auto resolves at construction, not per-call
    assert HttpEmbedder(OPENAI_URL, KEY, "m").style == STYLE_OPENAI


def test_openai_wire_request_and_response():
    async def main():
        seen.clear()
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "qwen3.7-text-embedding",
            transport=httpx.MockTransport(_openai_handler),
        )
        res = await emb.embed(["衣服的质量杠杠的", "混合检索RRF融合"])
        # request shape: {"model":..., "input": [str,...]} + Bearer auth
        assert seen["body"]["model"] == "qwen3.7-text-embedding"
        assert seen["body"]["input"] == ["衣服的质量杠杠的", "混合检索RRF融合"]
        assert seen["auth"] == f"Bearer {KEY}"
        # response: order preserved, usage counted for quota metering
        assert len(res.vectors) == 2
        assert res.vectors[0][0] == 0.0 and res.vectors[1][0] == 0.1
        assert res.usage_tokens == 7

    asyncio.run(main())


def test_dashscope_wire_resorts_by_text_index():
    async def main():
        seen.clear()
        emb = HttpEmbedder(
            DASHSCOPE_URL, KEY, "m", style="dashscope",
            transport=httpx.MockTransport(_dashscope_handler),
        )
        res = await emb.embed(["a", "b", "c"])
        # request shape: {"model":..., "input": {"texts": [...]}}
        assert seen["body"]["input"] == {"texts": ["a", "b", "c"]}
        # handler returned entries reversed; we must re-sort by text_index
        assert [v[0] for v in res.vectors] == [0.0, 0.1, 0.2]
        assert res.usage_tokens == 12

    asyncio.run(main())


def test_batching_splits_by_batch_size():
    async def main():
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(len(body["input"]))
            data = [{"index": i, "embedding": [0.1]} for i in range(len(body["input"]))]
            return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 5}})

        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", batch_size=2, transport=httpx.MockTransport(handler)
        )
        res = await emb.embed(["t1", "t2", "t3", "t4", "t5"])
        assert calls == [2, 2, 1], "5 texts at batch_size=2 -> 2+2+1 requests"
        assert len(res.vectors) == 5
        assert res.usage_tokens == 15, "usage accumulates across batches"

    asyncio.run(main())


def test_empty_input_short_circuits():
    async def main():
        def boom(request):  # must never be called
            raise AssertionError("network call for empty input")

        emb = HttpEmbedder(OPENAI_URL, KEY, "m", transport=httpx.MockTransport(boom))
        res = await emb.embed([])
        assert res.vectors == [] and res.usage_tokens == 0

    asyncio.run(main())


def test_embed_query_single():
    async def main():
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", transport=httpx.MockTransport(_openai_handler)
        )
        res = await emb.embed_query("什么是文本排序模型")
        assert len(res.vectors) == 1 and res.usage_tokens == 7

    asyncio.run(main())


def test_http_error_carries_vendor_message():
    """invalid_api_key is the #1 support question - the message must surface."""

    async def main():
        emb = HttpEmbedder(OPENAI_URL, KEY, "m", transport=httpx.MockTransport(_err_handler))
        try:
            await emb.embed(["x"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError as exc:
            msg = str(exc)
            assert "401" in msg and "Invalid API-key provided" in msg

    asyncio.run(main())


def test_malformed_responses_raise_embedding_error():
    """Every malformed shape must raise EmbeddingError (never a KeyError that
    would escape as a crash) so the retriever can degrade instead of dying."""

    async def main():
        cases = [
            (_count_mismatch_handler, "count mismatch"),
            (_nonlist_handler, "not a list of floats"),
            (_empty_vec_handler, "empty vector"),
            (_dup_index_handler, "duplicate indices"),
        ]
        for handler, label in cases:
            emb = HttpEmbedder(OPENAI_URL, KEY, "m", transport=httpx.MockTransport(handler))
            try:
                await emb.embed(["a", "b"])
                raise AssertionError(f"expected EmbeddingError for {label}")
            except EmbeddingError:
                pass  # correct: typed error, retriever will degrade

    asyncio.run(main())


def test_non_json_body_raises_embedding_error():
    async def main():
        def handler(request):
            return httpx.Response(200, text="<html>gateway 502</html>")

        emb = HttpEmbedder(OPENAI_URL, KEY, "m", transport=httpx.MockTransport(handler))
        try:
            await emb.embed(["x"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError as exc:
            assert "not JSON" in str(exc)

    asyncio.run(main())


def test_transport_error_raises_embedding_error():
    """Network failure (dead proxy / DNS / connect refused) -> typed error."""

    async def main():
        def handler(request):
            raise httpx.ConnectError("connection refused")

        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=0, transport=httpx.MockTransport(handler)
        )
        try:
            await emb.embed(["x"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError as exc:
            assert "embedding request failed" in str(exc)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Retries (defaults/_http.py). The remote endpoint is on the critical path: one
# transient 429 used to end as "vector channel unavailable" -> BM25-only answers
# at a materially lower hit@5, with only a log line to show for it.
# ---------------------------------------------------------------------------


def _flaky_handler(statuses: list[int], calls: list[int]):
    """Reply with ``statuses`` in order (last one repeats), counting calls."""

    def handler(request: httpx.Request) -> httpx.Response:
        i = len(calls)
        calls.append(1)
        status = statuses[min(i, len(statuses) - 1)]
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        body = json.loads(request.content)
        data = [{"index": k, "embedding": [0.1]} for k in range(len(body["input"]))]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 3}})

    return handler


def test_transient_5xx_is_retried_and_then_succeeds():
    """A 503 blip must NOT degrade the query: attempt, retry, succeed."""

    async def main():
        calls: list[int] = []
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=2, retry_backoff_s=0,
            transport=httpx.MockTransport(_flaky_handler([503, 200], calls)),
        )
        res = await emb.embed(["a"])
        assert len(calls) == 2, f"expected 1 retry, saw {len(calls)} attempts"
        assert len(res.vectors) == 1 and res.usage_tokens == 3

    asyncio.run(main())


def test_429_is_retried():
    async def main():
        calls: list[int] = []
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=2, retry_backoff_s=0,
            transport=httpx.MockTransport(_flaky_handler([429, 200], calls)),
        )
        await emb.embed(["a"])
        assert len(calls) == 2

    asyncio.run(main())


def test_retryable_status_gives_up_after_the_budget():
    """Exhausted retries must still surface the vendor body, not a bare error."""

    async def main():
        calls: list[int] = []
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=1, retry_backoff_s=0,
            transport=httpx.MockTransport(_flaky_handler([502], calls)),
        )
        try:
            await emb.embed(["a"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError as exc:
            assert "502" in str(exc)
        assert len(calls) == 2, "retries=1 -> exactly 2 attempts"

    asyncio.run(main())


def test_bad_key_is_not_retried():
    """A 401 fails identically forever - retrying only burns user latency."""

    async def main():
        calls: list[int] = []
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=3, retry_backoff_s=0,
            transport=httpx.MockTransport(_flaky_handler([401], calls)),
        )
        try:
            await emb.embed(["a"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError:
            pass
        assert len(calls) == 1, "4xx (other than 408/425/429) must not be retried"

    asyncio.run(main())


def test_read_timeout_is_not_retried():
    """A SLOW endpoint is not a flaky one: retrying doubles the wait."""

    async def main():
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("endpoint too slow")

        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=3, retry_backoff_s=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            await emb.embed(["a"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError:
            pass
        assert len(calls) == 1, "ReadTimeout must not be retried"

    asyncio.run(main())


def test_connect_error_exhausts_retries_then_raises():
    async def main():
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError("connection refused")

        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=2, retry_backoff_s=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            await emb.embed(["a"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError:
            pass
        assert len(calls) == 3, "retries=2 -> 3 attempts total"

    asyncio.run(main())


def test_retries_zero_means_single_attempt():
    async def main():
        calls: list[int] = []
        emb = HttpEmbedder(
            OPENAI_URL, KEY, "m", retries=0, retry_backoff_s=0,
            transport=httpx.MockTransport(_flaky_handler([503], calls)),
        )
        try:
            await emb.embed(["a"])
            raise AssertionError("expected EmbeddingError")
        except EmbeddingError:
            pass
        assert len(calls) == 1

    asyncio.run(main())


def test_unknown_style_rejected_at_construction():
    try:
        HttpEmbedder(OPENAI_URL, KEY, "m", style="bert")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
