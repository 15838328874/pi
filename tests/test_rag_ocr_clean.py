"""Tests for layout-model markdown cleaning (RAGFlow-style DeepDoc output).

PaddleOCR-VL / MinerU return "markdown" that is really HTML+LaTeX: tables as
``<table><tr><td>``, centred captions as ``<div style=...>``, images as
``<img src="imgs/...">``, and math as ``$ \\omega $`` / ``$ ^{[16]} $``. These
tests pin ``clean_ocr_markdown`` and the ingest wiring that uses it, so table
structure, citation superscripts, and image/tag noise never leak into chunks.

``paddleocr_vl_sample.jsonl`` is a REAL PaddleOCR-VL response (an 8-page
medical guideline), captured verbatim - not a hand-written toy.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pi.rag.chunker import Chunker
from pi.rag.config import ChunkingConfig, RagConfig
from pi.rag.defaults.fake_embedder import FakeEmbedder
from pi.rag.defaults.memory_vector import InMemoryVectorStore
from pi.rag.defaults.sqlite_store import SqliteChunkStore
from pi.rag.ingest import IngestPipeline
from pi.rag.parser import clean_ocr_markdown, parse_markdown
from pi.rag.types import IngestStatus

FIXTURE = Path(__file__).parent / "fixtures" / "paddleocr_vl_sample.jsonl"


def _fixture_pages() -> list[str]:
    pages: list[str] = []
    for line in FIXTURE.read_text(encoding="utf-8").strip().splitlines():
        for res in json.loads(line)["result"]["layoutParsingResults"]:
            pages.append(res["markdown"]["text"])
    return pages


# ---------------------------------------------------------------------------
# LaTeX inline math
# ---------------------------------------------------------------------------


def test_latex_symbols_to_unicode():
    assert clean_ocr_markdown(r"$ \omega $-3") == "ω-3"
    assert clean_ocr_markdown(r"$ \geq 2.3\ mmol/L $") == "≥ 2.3 mmol/L"
    assert clean_ocr_markdown(r"$ \times $") == "×"
    assert clean_ocr_markdown(r"$ \pm $") == "±"


def test_latex_citation_superscript_flattened():
    assert clean_ocr_markdown(r"$ ^{[16]} $") == "[16]"
    assert clean_ocr_markdown(r"$ ^{[17-18]} $") == "[17-18]"
    assert clean_ocr_markdown(r"$ ^{{a}} $") == "a"
    assert clean_ocr_markdown(r"$ ^{{[5]}} $") == "[5]"
    assert clean_ocr_markdown(r"$ ^{14} $") == "14"


def test_latex_formatting_prefix_keeps_body():
    assert clean_ocr_markdown(r"$ \text{foo} $") == "foo"
    assert clean_ocr_markdown(r"$ \mathrm{bar} $") == "bar"


# ---------------------------------------------------------------------------
# HTML table -> pipe grid
# ---------------------------------------------------------------------------


def test_html_table_to_pipe_grid_flattens_spans():
    md = (
        '<table border=1><tr><td rowspan="2" colspan="2">危险因素 (个)</td>'
        "<td colspan=\"3\">血清胆固醇水平分层</td></tr>"
        "<tr><td>低危</td><td>中危</td><td>高危</td></tr></table>"
    )
    out = clean_ocr_markdown(md)
    assert "| 危险因素 (个) | 血清胆固醇水平分层 |" in out
    assert "| 低危 | 中危 | 高危 |" in out


def test_html_table_cell_literal_newline_flattened():
    # PaddleOCR double-escapes newlines INSIDE a cell (a literal backslash-n).
    md = (
        "<table border=1><tr><td>高强度\\n（降 LDL-C ≥ 50%）</td>"
        "<td>阿托伐他汀 40~80 mg\\n瑞舒伐他汀 20 mg</td></tr></table>"
    )
    out = clean_ocr_markdown(md)
    assert "\\" not in out
    assert "高强度 （降 LDL-C ≥ 50%）" in out
    assert "阿托伐他汀 40~80 mg 瑞舒伐他汀 20 mg" in out


# ---------------------------------------------------------------------------
# block tags / images
# ---------------------------------------------------------------------------


def test_div_kept_as_text_img_dropped():
    assert clean_ocr_markdown('<div style="text-align: center;">表5 降脂药物</div>') == "表5 降脂药物"
    # a self-closing img is pure noise (its alt text is not content)
    assert clean_ocr_markdown(
        '<div style="text-align: center;"><img src="imgs/x.jpg" alt="Image" width="75%" /></div>'
    ) == ""


# ---------------------------------------------------------------------------
# real fixture
# ---------------------------------------------------------------------------


def test_real_fixture_cleans_without_residue():
    out = clean_ocr_markdown("\n\n".join(_fixture_pages()))
    # zero markup / math / backslash noise survives
    for token in ("<table", "<td", "<img", "<div", "<span", "$", "\\"):
        assert token not in out, f"residual {token!r} leaked into cleaned markdown"
    # the six tables all survived as pipe grids
    assert out.count("|") > 0


def test_real_fixture_reparses_to_structured_blocks(tmp_path):
    out = clean_ocr_markdown("\n\n".join(_fixture_pages()))
    p = tmp_path / "ocr.md"
    p.write_text(out, encoding="utf-8")
    parsed = parse_markdown(p)
    kinds = {b.kind for b in parsed.blocks}
    assert "table" in kinds
    assert "heading" in kinds
    tables = [b for b in parsed.blocks if b.kind == "table"]
    assert len(tables) == 6
    # the lipid stratification table is intact: header + threshold rows
    joined = "\n".join(t.text for t in tables)
    assert "LDL-C" in joined
    assert "≥ 5.2" in joined


# ---------------------------------------------------------------------------
# ingest wiring: heavy-parser output is cleaned before chunking
# ---------------------------------------------------------------------------


class _FakeHeavyParser:
    def __init__(self, md: str) -> None:
        self.md = md

    def parse(self, path: Path) -> str:
        return self.md


def test_ingest_ocr_branch_cleans_before_chunk(tmp_path):
    md = (
        "# 血脂管理\n\n"
        "他汀类药物可使 LDL-C 水平降低 $ ^{[16]} $，且 $ \\geq 2.3\\ mmol/L $ 时获益更明显。\n\n"
        '<div style="text-align: center;">表1 降脂药物联合应用策略</div>\n\n'
        "<table border=1><tr><td>联合应用策略</td><td>适用情况</td></tr>"
        "<tr><td>他汀 + 胆固醇吸收抑制剂</td><td>单药治疗后 LDL-C 不达标</td></tr></table>\n\n"
        "以上内容足够长，确保 chunker 产生稳定的文本块以便断言清洗后的内容确实入库。"
    )
    pipe, store = _pipeline(tmp_path, _FakeHeavyParser(md))

    scan = tmp_path / "scan.png"
    scan.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    async def main():
        out = await pipe.ingest_file(scan, user_id=1, doc_key="scan.png")
        assert out.status == IngestStatus.READY.value, out.reason
        chunks = await store.list_chunks_for_user(1)
        assert chunks
        all_text = "\n".join(c.text or "" for c in chunks)
        # markup / LaTeX never reached the store
        for token in ("<table", "<td", "<div", "$", "\\geq"):
            assert token not in all_text, f"residual {token!r} in chunks"
        # but the semantic content did
        assert "他汀 + 胆固醇吸收抑制剂" in all_text
        assert "≥ 2.3 mmol/L" in all_text

    asyncio.run(main())


def _pipeline(tmp_path: Path, heavy):
    store = SqliteChunkStore(tmp_path / "rag.sqlite3")
    vec = InMemoryVectorStore()
    emb = FakeEmbedder()
    cfg = RagConfig(chunking=ChunkingConfig(max_chars=300, min_chars=20))
    pipe = IngestPipeline(store, emb, vec, config=cfg, heavy_parser=heavy)
    return pipe, store
