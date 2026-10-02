"""Tests for the external heavy-parser (OCR) integration.

Two layers:

1. ``PaddleOcrHeavyParser`` protocol steps are exercised against a mock
   ``httpx.MockTransport`` — submit → poll → download JSONL → joined Markdown,
   plus the failure paths. No network, no real token.

2. Ingest's ``needs_heavy_parser`` branch: a fake HeavyParser returns Markdown
   and the scanned file must flow through parse → chunk → embed → READY (not
   NEEDS_HEAVY_PARSER).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pi.rag.config import ChunkingConfig, RagConfig
from pi.rag.defaults.fake_embedder import FakeEmbedder
from pi.rag.defaults.memory_vector import InMemoryVectorStore
from pi.rag.defaults.sqlite_store import SqliteChunkStore
from pi.rag.heavy import HeavyParserError, PaddleOcrHeavyParser
from pi.rag.ingest import IngestPipeline
from pi.rag.types import IngestStatus

JOB_URL = "https://paddleocr.example/api/v2/ocr/jobs"


def _handler_done(request: httpx.Request) -> httpx.Response:
    """submit -> jobId；poll -> done + jsonUrl；download -> JSONL with markdown."""
    if request.method == "POST":
        return httpx.Response(200, json={"code": 0, "data": {"jobId": "job-123"}})
    if request.method == "GET" and request.url.path.endswith("/job-123"):
        return httpx.Response(200, json={
            "data": {"state": "done",
                     "resultUrl": {"jsonUrl": "https://store.example/result.json"}}})
    if request.method == "GET" and request.url.host == "store.example":
        lines = [
            json.dumps({"result": {"layoutParsingResults": [
                {"markdown": {"text": "## 标题\n\n正文内容。"}},
                {"markdown": {"text": "第二页。"}},
            ]}}),
        ]
        return httpx.Response(200, text="\n".join(lines) + "\n")
    return httpx.Response(404)


def _handler_failed(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(200, json={"code": 0, "data": {"jobId": "job-x"}})
    return httpx.Response(200, json={"data": {"state": "failed", "errorMsg": "bad image"}})


def _handler_submit_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, text="unauthorized")


def _mk(path_text: str = "dummy") -> Path:
    p = Path("/tmp/paddleocr-test.bin")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return p


# ---------------------------------------------------------------------------
# PaddleOcrHeavyParser protocol
# ---------------------------------------------------------------------------


def test_paddleocr_joins_markdown_across_pages():
    parser = PaddleOcrHeavyParser(
        JOB_URL, "token", "PaddleOCR-VL-1.6",
        transport=httpx.MockTransport(_handler_done),
    )
    out = parser.parse(_mk())
    assert "## 标题" in out
    assert "正文内容。" in out
    assert "第二页。" in out


def test_paddleocr_failed_job_raises():
    parser = PaddleOcrHeavyParser(
        JOB_URL, "token", "m", transport=httpx.MockTransport(_handler_failed))
    with pytest.raises(HeavyParserError, match="bad image"):
        parser.parse(_mk())


def test_paddleocr_submit_error_raises():
    parser = PaddleOcrHeavyParser(
        JOB_URL, "token", "m", transport=httpx.MockTransport(_handler_submit_error))
    with pytest.raises(HeavyParserError, match="HTTP 401"):
        parser.parse(_mk())


# ---------------------------------------------------------------------------
# ingest needs_heavy_parser -> OCR branch
# ---------------------------------------------------------------------------


class _FakeHeavyParser:
    def __init__(self, md: str) -> None:
        self.md = md
        self.calls = 0

    def parse(self, path: Path) -> str:
        self.calls += 1
        return self.md


def _pipeline(tmp_path: Path, heavy):
    store = SqliteChunkStore(tmp_path / "rag.sqlite3")
    vec = InMemoryVectorStore()
    emb = FakeEmbedder()
    cfg = RagConfig(chunking=ChunkingConfig(max_chars=300, min_chars=20))
    pipe = IngestPipeline(store, emb, vec, config=cfg, heavy_parser=heavy)
    return pipe, store


def test_scanned_file_flows_through_ocr_to_ready(tmp_path):
    md = "# 扫描件识别结果\n\n这是一段 OCR 出来的正文，足够长以通过最小切块门槛，"
    md += "这里再补一些内容确保有稳定的文本块。\n\n## 第二小节\n\n又一段内容。"
    fake = _FakeHeavyParser(md)
    pipe, store = _pipeline(tmp_path, fake)

    # 一个无文本的"扫描"图片：parser 会判 needs_heavy_parser
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    async def main():
        out = await pipe.ingest_file(scan, user_id=1, doc_key="scan.png")
        assert fake.calls == 1
        assert out.status == IngestStatus.READY.value, out.reason
        doc = await store.get_doc(1, "scan.png")
        assert doc["status"] == IngestStatus.READY.value
        chunks = await store.list_chunks_for_user(1)
        assert any("OCR" in (c.text or "") or "扫描件" in (c.text or "") for c in chunks)

    asyncio.run(main())


def test_scanned_file_stays_needs_heavy_when_ocr_fails(tmp_path):
    class _Boom:
        def parse(self, path: Path) -> str:
            raise HeavyParserError("boom")

    pipe, store = _pipeline(tmp_path, _Boom())
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    async def main():
        out = await pipe.ingest_file(scan, user_id=1, doc_key="scan.png")
        assert out.status == IngestStatus.NEEDS_HEAVY_PARSER.value
        doc = await store.get_doc(1, "scan.png")
        assert doc["status"] == IngestStatus.NEEDS_HEAVY_PARSER.value

    asyncio.run(main())


def test_no_heavy_parser_keeps_v1_behaviour(tmp_path):
    """未配 OCR 服务时，图片维持 v1 行为：标记 needs_heavy_parser，不入库。"""
    pipe, store = _pipeline(tmp_path, None)
    scan = tmp_path / "scan.png"
    scan.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    async def main():
        out = await pipe.ingest_file(scan, user_id=1, doc_key="scan.png")
        assert out.status == IngestStatus.NEEDS_HEAVY_PARSER.value
        assert await store.list_chunks_for_user(1) == []

    asyncio.run(main())
