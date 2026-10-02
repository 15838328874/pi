"""HttpReranker wire-format tests (DashScope text-rerank shape).

MockTransport, no network. The request shape mirrors the vendor curl the
user provided 1:1 (input.query/documents + parameters.top_n/return_documents),
and the failure semantics are the house rule: any error -> RerankError ->
retriever skips rerank, keeps RRF order, reports rerank_failed.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from pi.rag.defaults.http_reranker import HttpReranker, RerankError
from pi.rag.types import RetrievedChunk

RR_URL = "https://example.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
KEY = "***"

seen: dict[str, object] = {}


def _chunks(n: int = 3) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=100 + i,
            doc_key=f"d{i}",
            text=f"doc text {i}",
            score=1.0 - i * 0.1,  # incoming RRF order
            title=f"t{i}",
            source=f"/src/{i}.pdf",
            page=i,
        )
        for i in range(n)
    ]


def _ok_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    seen["auth"] = request.headers.get("Authorization")
    seen["body"] = body
    n = len(body["input"]["documents"])
    # Reverse relevance: the LAST document is the best match.
    results = [{"index": i, "relevance_score": i / 10} for i in range(n)]
    return httpx.Response(
        200, json={"output": {"results": results}, "usage": {"total_tokens": 42}}
    )


def _err_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "Invalid API-key"}})


def _bad_index_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"output": {"results": [{"index": 99, "relevance_score": 1.0}]}})


def _malformed_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"unexpected": True})


def test_request_shape_matches_vendor_curl():
    async def main():
        seen.clear()
        rr = HttpReranker(RR_URL, KEY, "qwen3.7-text-rerank",
                          transport=httpx.MockTransport(_ok_handler))
        await rr.rerank("什么是文本排序模型", _chunks(3))
        body = seen["body"]
        assert body["model"] == "qwen3.7-text-rerank"
        assert body["input"]["query"] == "什么是文本排序模型"
        assert body["input"]["documents"] == ["doc text 0", "doc text 1", "doc text 2"]
        assert body["parameters"] == {"top_n": 3, "return_documents": False}
        assert seen["auth"] == f"Bearer {KEY}"

    asyncio.run(main())


def test_cross_encoder_sees_the_title_path_like_both_retrieval_channels():
    """Channel symmetry, the third leg: rerank must not be shown LESS than
    the channels that produced its candidates.

    Regression guard for the M4 ``edge_case`` loss. The vector channel embeds
    ``text_to_embed()`` (contextual prefix carries title_path) and BM25 indexes
    ``text_to_index()`` (title_path + body). This reranker used to send the bare
    ``text``, so a query whose answer LIVES IN THE HEADING ("LangSmith 被放在
    哪个章节路径下") was unanswerable by the cross-encoder: it fell back to
    body-level similarity and promoted same-document NEIGHBOURS over the gold.
    Measured cost: 5 LOST cases, all edge_case, 3 of them 1.000 -> 0.20-0.50.
    """
    async def main():
        seen.clear()
        rr = HttpReranker(RR_URL, KEY, "m", transport=httpx.MockTransport(_ok_handler))
        chunks = [
            RetrievedChunk(chunk_id=1, doc_key="rag-eval", text="支持多种评估方式",
                           score=1.0, title_path="二.RAG评估方法 > 3.LangSmith"),
            RetrievedChunk(chunk_id=2, doc_key="rag-eval", text="支持多种评估方式",
                           score=0.9, title_path="二.RAG评估方法 > 4.Ragas"),
        ]
        await rr.rerank("LangSmith被放在哪个章节路径下", chunks)
        docs = seen["body"]["input"]["documents"]
        # the heading path reaches the model, so the two same-body chunks
        # become distinguishable at all
        assert docs == [
            "二.RAG评估方法 > 3.LangSmith\n支持多种评估方式",
            "二.RAG评估方法 > 4.Ragas\n支持多种评估方式",
        ]
        assert all("LangSmith" in docs[0] for _ in [0])
        # the citation text itself is NOT rewritten - only the scoring payload
        assert chunks[0].text == "支持多种评估方式"

    asyncio.run(main())


def test_title_path_is_not_duplicated_when_the_body_already_carries_it():
    """Mirrors the guard in ``Chunk.text_to_index()``: some parsers emit the
    heading as the first body line. Prepending it again would double-weight
    that string and inflate the rerank token bill for no signal."""
    async def main():
        seen.clear()
        rr = HttpReranker(RR_URL, KEY, "m", transport=httpx.MockTransport(_ok_handler))
        chunks = [
            RetrievedChunk(chunk_id=1, doc_key="d", score=1.0,
                           title_path="3.2 空指针异常",
                           text="3.2 空指针异常\n本节讨论 NPE 的成因"),
            RetrievedChunk(chunk_id=2, doc_key="d", score=0.9, title_path="",
                           text="无标题路径的块"),
        ]
        await rr.rerank("q", chunks)
        assert seen["body"]["input"]["documents"] == [
            "3.2 空指针异常\n本节讨论 NPE 的成因",
            "无标题路径的块",
        ]

    asyncio.run(main())


def test_reorders_by_relevance_without_mutating_input():
    async def main():
        rr = HttpReranker(RR_URL, KEY, "m", transport=httpx.MockTransport(_ok_handler))
        chunks = _chunks(3)
        out = await rr.rerank("q", chunks)
        # handler scores index 2 highest -> output order 2,1,0
        assert [c.chunk_id for c in out] == [102, 101, 100]
        assert out[0].score == 0.2 and out[2].score == 0.0
        # citation fields survive the rerank copy
        assert out[0].source == "/src/2.pdf" and out[0].page == 2
        # caller's chunks untouched (no in-place score mutation)
        assert [c.score for c in chunks] == [1.0, 0.9, 0.8]
        # usage captured for metering
        assert rr.last_usage is not None and rr.last_usage.usage_tokens == 42

    asyncio.run(main())


def test_empty_chunks_short_circuits():
    async def main():
        def boom(request):
            raise AssertionError("network call for empty chunks")

        rr = HttpReranker(RR_URL, KEY, "m", transport=httpx.MockTransport(boom))
        assert await rr.rerank("q", []) == []

    asyncio.run(main())


def test_errors_raise_rerank_error():
    """Every failure -> RerankError so the retriever can skip-and-degrade."""

    async def main():
        cases = [
            (_err_handler, "401"),
            (_bad_index_handler, "out of range"),
            (_malformed_handler, "malformed"),
        ]
        for handler, label in cases:
            rr = HttpReranker(RR_URL, KEY, "m", transport=httpx.MockTransport(handler))
            try:
                await rr.rerank("q", _chunks(3))
                raise AssertionError(f"expected RerankError for {label}")
            except RerankError:
                pass

        # transport-level failure
        def dead(request):
            raise httpx.ConnectError("refused")

        rr = HttpReranker(RR_URL, KEY, "m", retries=0,
                          transport=httpx.MockTransport(dead))
        try:
            await rr.rerank("q", _chunks(1))
            raise AssertionError("expected RerankError for transport failure")
        except RerankError as exc:
            assert "rerank request failed" in str(exc)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Timeout knob + retry policy. The 15s timeout was hardcoded and turned a slow
# endpoint into "rerank failed on every request" - a symptom that reads like a
# bad model. It is now PI_RAG_RERANK_TIMEOUT.
# ---------------------------------------------------------------------------


def test_configured_timeout_reaches_the_http_client():
    async def main():
        rr = HttpReranker(RR_URL, KEY, "m", timeout=2.5,
                          transport=httpx.MockTransport(_ok_handler))
        assert rr.timeout == 2.5
        client = rr._client()
        # httpx applies the scalar to every phase of the request.
        assert client.timeout.read == 2.5 and client.timeout.connect == 2.5
        await rr.aclose()

    asyncio.run(main())


def test_transient_503_is_retried_then_succeeds():
    async def main():
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            i = len(calls)
            calls.append(1)
            if i == 0:
                return httpx.Response(503, json={"error": {"message": "busy"}})
            return _ok_handler(request)

        rr = HttpReranker(RR_URL, KEY, "m", retries=1, retry_backoff_s=0,
                          transport=httpx.MockTransport(handler))
        out = await rr.rerank("q", _chunks(3))
        assert len(calls) == 2, "one retry after a 503"
        assert [c.chunk_id for c in out] == [102, 101, 100]

    asyncio.run(main())


def test_read_timeout_is_not_retried():
    """Retrying a slow cross-encoder just doubles the user's wait."""

    async def main():
        calls: list[int] = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("too slow")

        rr = HttpReranker(RR_URL, KEY, "m", retries=3, retry_backoff_s=0,
                          transport=httpx.MockTransport(handler))
        try:
            await rr.rerank("q", _chunks(1))
            raise AssertionError("expected RerankError")
        except RerankError:
            pass
        assert len(calls) == 1

    asyncio.run(main())


def test_bad_key_is_not_retried():
    async def main():
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(401, json={"error": {"message": "Invalid API-key"}})

        rr = HttpReranker(RR_URL, KEY, "m", retries=3, retry_backoff_s=0,
                          transport=httpx.MockTransport(handler))
        try:
            await rr.rerank("q", _chunks(1))
            raise AssertionError("expected RerankError")
        except RerankError as exc:
            assert "401" in str(exc) and "Invalid API-key" in str(exc)
        assert len(calls) == 1

    asyncio.run(main())
