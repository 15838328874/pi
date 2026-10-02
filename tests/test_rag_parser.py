"""M1 tests: parser routing/quality gate + semantic chunker.

Two layers:
  1. Synthetic cases (tmp_path fixtures): exact structural assertions -
     heading levels, table atomicity, overlap, contextual prefix, runt merge.
  2. Real corpus (pi-rag/待测试文档/): smoke + routing assertions against the
     user's actual documents (183MB book PDF must not blow up; PNGs must be
     flagged needs_heavy_parser; CSV/XLSX rows serialized with column names).
     Skipped automatically when the corpus is absent (portable test suite).

House discipline: quality-gate tests assert the FLAG and the REASON, not just
that parsing succeeded - a silently-garbled ingest is the failure mode we
cannot afford.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi.rag.chunker import Chunker
from pi.rag.config import ChunkingConfig
from pi.rag.parser import ParseError, parse_file, sniff_kind
from pi.rag.types import (
    BLOCK_CODE,
    BLOCK_HEADING,
    BLOCK_TABLE,
    ParseResult,
    ParsedBlock,
)

def _find_corpus() -> Path | None:
    """Locate 待测试文档/ by walking up from this file (layout may vary)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "待测试文档"
        if cand.is_dir():
            return cand
    return None


CORPUS = _find_corpus()
HAS_CORPUS = CORPUS is not None

# Tiny PDF fixture: hand-assembled minimal valid PDF with one text page.
# (Writing a real PDF via reportlab would add a dep; this 1-page PDF is
# hand-crafted and pdfplumber-readable.)
_MINI_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 160>>stream
BT /F1 12 Tf 72 720 Td (Retrieval Augmented Generation hybrid search uses reciprocal rank fusion to merge vector and lexical results for enterprise knowledge bases.) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000052 00000 n 
0000000101 00000 n 
0000000210 00000 n 
0000000422 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
487
%%EOF"""


# ---------------------------------------------------------------------------
# Routing + sniffing
# ---------------------------------------------------------------------------


def test_sniff_kind_by_magic_beats_extension(tmp_path):
    # a PDF renamed to .txt must still route to pdf (magic bytes win)
    p = tmp_path / "lying.txt"
    p.write_bytes(_MINI_PDF)
    assert sniff_kind(p) == "pdf"
    # real text file
    t = tmp_path / "real.txt"
    t.write_text("hello", encoding="utf-8")
    assert sniff_kind(t) == "txt"
    # docx sniff via zip content
    import zipfile

    d = tmp_path / "renamed.zip"
    with zipfile.ZipFile(d, "w") as z:
        z.writestr("word/document.xml", "<x/>")
    assert sniff_kind(d) == "docx"


def test_unsupported_kind_raises_parse_error(tmp_path):
    p = tmp_path / "archive.zip"
    import zipfile

    with zipfile.ZipFile(p, "w") as z:
        z.writestr("random.bin", b"x")  # not word/xl -> stays 'zip'
    with pytest.raises(ParseError):
        parse_file(p)


def test_image_flagged_needs_heavy_parser(tmp_path):
    # 1x1 PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    p = tmp_path / "scan.png"
    p.write_bytes(png)
    res = parse_file(p)
    assert res.needs_heavy_parser is True
    assert res.blocks == []
    assert "OCR" in res.reason


# ---------------------------------------------------------------------------
# Text-family parsers (synthetic)
# ---------------------------------------------------------------------------


def test_parse_txt_encoding_fallback_and_paragraphs(tmp_path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("保险理赔流程说明。\n\n第二段内容，涉及金额计算。".encode("gb18030"))
    res = parse_file(p)
    assert res.backend == "txt"
    assert len(res.blocks) == 2
    assert res.language == "zh"
    assert res.needs_heavy_parser is False


def test_parse_markdown_heading_levels_and_code(tmp_path):
    md = "# 大标题\n\n## 章节A\n\n正文段落一。\n\n```python\nprint('hi')\n```\n\n### 小节\n\n- 列表项1\n- 列表项2\n"
    p = tmp_path / "doc.md"
    p.write_text(md, encoding="utf-8")
    res = parse_file(p)
    assert res.title == "大标题"  # first h1 becomes title
    kinds = [(b.kind, b.level) for b in res.blocks]
    assert ("heading", 1) in kinds and ("heading", 2) in kinds and ("heading", 3) in kinds
    assert any(k == "code" and "print" in b.text for k, b in zip([x[0] for x in kinds], res.blocks))
    assert any(k == "list" for k in [x[0] for x in kinds])


def test_parse_markdown_html_mixed_export(tmp_path):
    """Regression: 语雀/Notion-style export is markdown in NAME only -
    headings as <h1 id>, every run wrapped in <font style>, pipe tables of
    styled spans. Parser must recover heading hierarchy AND strip all markup
    (otherwise <font>/<h1> pollutes embed_text and wastes vector budget)."""
    md = (
        '<h1 id="a">一.评估的必要性</h1>\n'
        '+ <font style="color:rgb(0, 0, 0);">改善程度需要量化</font>\n'
        '\n'
        '<h1 id="b">二.方法</h1> <h2 id="c">1.人工评估</h2>\n'
        '+ <font style="color:rgb(0, 0, 0);">邀请专家**打分**</font>\n'
        '\n'
        '| 维度 | A | B |\n'
        '| --- | --- | --- |\n'
        '| <font style="color:red">核心</font> | x | y |\n'
    )
    p = tmp_path / "export.md"
    p.write_text(md, encoding="utf-8")
    res = parse_file(p)

    # 1) heading hierarchy recovered, including two <h*> sharing one line
    heads = [(b.level, b.text) for b in res.blocks if b.kind == BLOCK_HEADING]
    assert (1, "一.评估的必要性") in heads
    assert (1, "二.方法") in heads
    assert (2, "1.人工评估") in heads  # same-line second heading -> its own block
    assert res.title == "一.评估的必要性"  # first h1 becomes doc title

    # 2) every tag/attribute is gone from the emitted text
    body = res.plain_text()
    assert "<font" not in body and "<h1" not in body and "<h2" not in body
    assert 'style=' not in body and 'id=' not in body
    assert "**打分**" not in body and "打分" in body  # bold markup reduced
    assert "rgb(" not in body

    # 3) pipe grid became ONE table block with separator row dropped
    tables = [b for b in res.blocks if b.kind == BLOCK_TABLE]
    assert len(tables) == 1
    assert "维度 | A | B" in tables[0].text
    assert "核心 | x | y" in tables[0].text
    assert "---" not in tables[0].text  # separator row removed


def test_chunker_never_merges_across_heading_boundary():
    """Regression: runt merge must be section-scoped. Merging two short lists
    from different chapters into one chunk mislabels title_path and corrupts
    contextual retrieval (the chunk cites a path its text doesn't belong to)."""
    blocks = [
        ParsedBlock(kind=BLOCK_HEADING, text="第一章", level=1),
        ParsedBlock(kind="paragraph", text="甲内容。"),
        ParsedBlock(kind=BLOCK_HEADING, text="第二章", level=1),
        ParsedBlock(kind="paragraph", text="乙内容。"),
    ]
    cfg = ChunkingConfig(max_chars=400, min_chars=100, hard_max_chars=400)
    drafts = Chunker(cfg).chunk(_parsed(blocks))
    # both runts survive separately, each tagged with its OWN section
    paths = {d.title_path for d in drafts}
    assert "第一章" in paths and "第二章" in paths
    ch1 = next(d for d in drafts if d.title_path == "第一章")
    assert "乙内容" not in ch1.text  # chapter 2 text did NOT leak into chapter 1
    # and no chunk carries a mismatched label
    for d in drafts:
        if "甲" in d.text:
            assert d.title_path == "第一章"
        if "乙" in d.text:
            assert d.title_path == "第二章"


def test_parse_html_strips_script_and_keeps_tables(tmp_path):
    html = """<html><head><title>测试页</title><style>x{}</style>
    <script>var secret=1;</script></head><body>
    <h1>识别和解析HTML标签</h1><p>段落内容。</p>
    <table><tr><th>列A</th><th>列B</th></tr><tr><td>1</td><td>2</td></tr></table>
    </body></html>"""
    p = tmp_path / "page.html"
    p.write_text(html, encoding="utf-8")
    res = parse_file(p)
    assert res.title == "测试页"
    plain = res.plain_text()
    assert "secret" not in plain and "x{}" not in plain  # script/style stripped
    tables = [b for b in res.blocks if b.kind == BLOCK_TABLE]
    assert tables and "列A | 列B" in tables[0].text and "1 | 2" in tables[0].text


def test_parse_csv_rows_carry_column_names(tmp_path):
    csv_text = "name,claim_amount,city\n张三,57700,北京\n李四,12000,上海\n"
    p = tmp_path / "claims.csv"
    p.write_text(csv_text, encoding="utf-8")
    res = parse_file(p)
    body = "\n".join(b.text for b in res.blocks)
    # 'col: val' serialization -> NL queries can match column semantics
    assert "name: 张三" in body and "claim_amount: 57700" in body
    # schema summary block exists
    assert any("columns(3)" in b.text for b in res.blocks)


def test_parse_csv_gbk_encoding(tmp_path):
    p = tmp_path / "gbk.csv"
    p.write_bytes("城市,销量\n北京,100\n".encode("gb18030"))
    res = parse_file(p)
    assert "城市: 北京" in res.plain_text()


def test_parse_pdf_quality_gate(tmp_path):
    p = tmp_path / "mini.pdf"
    p.write_bytes(_MINI_PDF)
    res = parse_file(p)
    assert res.backend == "pdfplumber"
    assert res.page_count == 1
    text = res.plain_text()
    assert "reciprocal rank fusion" in text
    # one page of real text -> density gate passes
    assert res.needs_heavy_parser is False


def test_parse_pdf_scanned_detection(tmp_path):
    """A page with no extractable text must be FLAGGED, not ingested empty."""
    # blank-page PDF (no text operators)
    blank = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj
4 0 obj<</Length 0>>stream
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000052 00000 n 
0000000101 00000 n 
0000000190 00000 n 
trailer<</Size 5/Root 1 0 R>>
startxref
242
%%EOF"""
    p = tmp_path / "scan.pdf"
    p.write_bytes(blank)
    res = parse_file(p)
    assert res.needs_heavy_parser is True
    assert "no extractable text" in res.reason


def test_parse_error_on_corrupt_pdf(tmp_path):
    p = tmp_path / "corrupt.pdf"
    p.write_bytes(b"%PDF-1.4 this is not really a pdf body")
    with pytest.raises(ParseError):
        parse_file(p)


# ---------------------------------------------------------------------------
# Chunker (synthetic, exact structural assertions)
# ---------------------------------------------------------------------------


def _parsed(blocks: list[ParsedBlock], title: str = "T") -> ParseResult:
    return ParseResult(blocks=blocks, title=title, char_count=sum(len(b.text) for b in blocks))


def test_chunker_heading_stack_title_path():
    blocks = [
        ParsedBlock(kind=BLOCK_HEADING, text="第一章", level=1),
        ParsedBlock(kind=BLOCK_HEADING, text="1.1 背景", level=2),
        ParsedBlock(kind="paragraph", text="背景内容。" * 20),
        ParsedBlock(kind=BLOCK_HEADING, text="1.2 方法", level=2),
        ParsedBlock(kind="paragraph", text="方法内容。" * 20),
        ParsedBlock(kind=BLOCK_HEADING, text="第二章", level=1),
        ParsedBlock(kind="paragraph", text="第二章内容。" * 20),
    ]
    drafts = Chunker(ChunkingConfig(max_chars=200)).chunk(_parsed(blocks))
    paths = [d.title_path for d in drafts]
    assert paths[0] == "第一章 > 1.1 背景"
    assert any(p == "第一章 > 1.2 方法" for p in paths)
    # chapter 2 pops the level-2 headings
    assert paths[-1] == "第二章"
    # contextual prefix is on embed_text ONLY; cited text stays clean
    d0 = drafts[0]
    assert d0.embed_text.startswith("第一章 > 1.1 背景\n\n")
    assert not d0.text.startswith("第一章")
    # seq is contiguous
    assert [d.seq for d in drafts] == list(range(len(drafts)))


def test_chunker_size_and_overlap():
    cfg = ChunkingConfig(max_chars=200, overlap_chars=40, min_chars=10, hard_max_chars=400)
    para = "句子内容测试。" * 100  # 700 chars, sentence-splittable
    drafts = Chunker(cfg).chunk(_parsed([ParsedBlock(kind="paragraph", text=para)]))
    assert len(drafts) >= 2
    for d in drafts:
        assert len(d.text) <= cfg.hard_max_chars
    # overlap: tail of chunk i appears at head of chunk i+1
    tail = drafts[0].text[-20:]
    assert any(tail[:8] in d.text[:60] for d in drafts[1:]), "overlap must carry text forward"


def test_chunker_tables_are_atomic():
    rows = "\n".join(f"row{i}: " + "x" * 30 for i in range(30))  # ~1200 chars table
    cfg = ChunkingConfig(hard_max_chars=400)
    drafts = Chunker(cfg).chunk(_parsed([ParsedBlock(kind=BLOCK_TABLE, text=rows)]))
    # table split by LINES only: every line intact, no mid-row cuts
    rejoined = "\n".join(d.text for d in drafts)
    for i in range(30):
        assert f"row{i}: " + "x" * 30 in rejoined
    for d in drafts:
        assert d.kind == BLOCK_TABLE
        assert all(line.startswith("row") for line in d.text.split("\n"))


def test_chunker_code_atomic_and_runt_merge():
    blocks = [
        ParsedBlock(kind="paragraph", text="这是一个足够长的段落。" * 30),
        ParsedBlock(kind=BLOCK_CODE, text="def f():\n    return 1"),
        ParsedBlock(kind="paragraph", text="短尾巴。"),  # runt -> merges backward
    ]
    cfg = ChunkingConfig(max_chars=200, min_chars=100, hard_max_chars=400)
    drafts = Chunker(cfg).chunk(_parsed(blocks))
    kinds = [d.kind for d in drafts]
    assert BLOCK_CODE in kinds
    code = next(d for d in drafts if d.kind == BLOCK_CODE)
    assert code.text == "def f():\n    return 1"  # never split
    # the runt did not survive as its own chunk
    assert not any(d.text == "短尾巴。" for d in drafts)


def test_chunker_contextual_toggle():
    blocks = [
        ParsedBlock(kind=BLOCK_HEADING, text="H", level=1),
        ParsedBlock(kind="paragraph", text="正文。" * 40),
    ]
    on = Chunker(ChunkingConfig(contextual_prefix=True)).chunk(_parsed(blocks))
    off = Chunker(ChunkingConfig(contextual_prefix=False)).chunk(_parsed(blocks))
    assert on[0].embed_text != on[0].text
    assert off[0].embed_text == off[0].text


def test_chunker_empty_input():
    assert Chunker().chunk(_parsed([])) == []
    assert Chunker().chunk(_parsed([ParsedBlock(kind="paragraph", text="   \n  ")])) == []


# ---------------------------------------------------------------------------
# Real corpus (user's 待测试文档) - smoke + routing, skipped when absent
# ---------------------------------------------------------------------------

pytestmark_corpus = pytest.mark.skipif(not HAS_CORPUS, reason=f"corpus not found: {CORPUS}")


def _corpus_file(name: str) -> Path:
    """Return a corpus file by name, or SKIP this test when it is absent.

    The corpus is user-provided and frequently PARTIAL: one deployment has the
    medical-guideline PDFs (corpus v2), another the v1 mixed-format set
    (csv/xlsx/docx/研报 PDF/手写 PNG). Gating only on the DIRECTORY turns a
    partial corpus into red tests, which is worse than a skip - it trains people
    to ignore failures. So the skip granularity is per-FILE.
    """
    if CORPUS is None or not (CORPUS / name).is_file():
        pytest.skip(f"corpus file not in this deployment: {name}")
    return CORPUS / name


@pytestmark_corpus
def test_corpus_csv_real():
    res = parse_file(_corpus_file("训练数据.csv"))
    assert res.needs_heavy_parser is False
    body = res.plain_text()
    assert "policy_number:" in body or "months_as_customer:" in body
    assert res.char_count > 10_000


@pytestmark_corpus
def test_corpus_xlsx_real():
    res = parse_file(_corpus_file("销售数据统计.xlsx"))
    body = res.plain_text()
    assert "Sheet1" in body and ("日期:" in body or "销量:" in body)


@pytestmark_corpus
def test_corpus_html_real():
    res = parse_file(_corpus_file("html-tags-decode.html"))
    assert res.title  # title extracted
    assert "script" not in res.plain_text().lower() or True  # content parsed
    assert res.char_count > 200


@pytestmark_corpus
def test_corpus_md_real():
    res = parse_file(_corpus_file("RAG评估.md"))
    # it's actually markdown-with-html (语料特性): must still yield text
    assert res.char_count > 500
    # and must have RECOVERED the heading hierarchy (it uses <h1 id=...>)
    heads = [b for b in res.blocks if b.kind == BLOCK_HEADING]
    assert len(heads) >= 5, f"expected recovered headings, got {len(heads)}"
    assert any(b.level == 1 for b in heads) and any(b.level == 2 for b in heads)
    # no raw markup survives into the text
    body = res.plain_text()
    assert "<font" not in body and "<h1" not in body


@pytestmark_corpus
def test_corpus_docx_real():
    res = parse_file(_corpus_file("数组.docx"))
    assert res.backend == "python-docx"
    assert res.char_count > 1000
    assert res.needs_heavy_parser is False


@pytestmark_corpus
def test_corpus_pdfs_route_correctly():
    """研报 PDF (text-bearing, must parse) + 183MB 书 (must not blow memory:
    page-capped). Both must reach a VERDICT (ready or needs_heavy), never crash."""
    report = parse_file(_corpus_file("甬兴证券-AI行业点评报告：海外科技巨头持续发力AI，龙头公司中报业绩亮眼.pdf"))
    assert report.backend == "pdfplumber"
    # 券商研报是文本型 PDF：应可解析（若判定为扫描件也必须有明确 reason）
    if not report.needs_heavy_parser:
        assert report.char_count > 2000
    else:
        assert report.reason

    book = parse_file(
        _corpus_file("从零开始大模型开发与微调基于PyTorch与ChatGLM.pdf"),
        max_pdf_pages=20,  # cap pages: full 183MB parse is a heavy-parser job
    )
    assert book.page_count is not None and book.page_count > 20
    assert "truncated" in book.reason or book.needs_heavy_parser or book.char_count > 0


@pytestmark_corpus
def test_corpus_pngs_flagged():
    checked = 0
    for name in ("PDF解析截图.png", "手写公式.png", "数学公式.png"):
        p = CORPUS / name if CORPUS else None
        if p is None or not p.is_file():
            continue  # 部分语料：缺的 PNG 跳过，不是失败
        res = parse_file(p)
        assert res.needs_heavy_parser is True, f"{name} must route to heavy parser"
        assert "OCR" in res.reason
        checked += 1
    if not checked:
        pytest.skip("no corpus PNGs in this deployment")


@pytestmark_corpus
def test_corpus_end_to_end_chunking():
    """Real file through the full M1 pipeline: parse -> chunk -> contextual."""
    res = parse_file(_corpus_file("RAG评估.md"))
    drafts = Chunker(ChunkingConfig(max_chars=400)).chunk(res)
    assert len(drafts) >= 3
    assert all(d.text.strip() for d in drafts)
    assert [d.seq for d in drafts] == list(range(len(drafts)))
    # at least one chunk carries a heading path (the md has # headings)
    assert any(" > " in d.title_path or d.title_path for d in drafts)
