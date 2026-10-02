"""Parsing layer: file -> structured blocks, with a quality gate.

Architecture (RAG_DESIGN §2.1): a parser ROUTER, not one hardcoded tool.
Each backend declares the extensions it owns; ``parse_file`` sniffs the file
(extension + magic bytes) and dispatches. Heavy backends (MinerU for complex
PDFs, PaddleOCR for scans) are deliberately OUT of the app process - they are
external services (v1.5); v1 detects such files and returns
``needs_heavy_parser=True`` so ingest marks them instead of poisoning the
index with OCR-less garbage.

The quality gate is the production-critical part: text density per page and
garbled-char ratio decide whether a PDF is text-bearing. A scanned book run
through pdfplumber yields ~0 chars/page -> flagged, not ingested.

All heavy deps (pdfplumber, python-docx, openpyxl, bs4) are lazy-imported
inside their backend - kernel import stays cheap and dependency-light.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import re
from pathlib import Path

from pi.rag.types import (
    BLOCK_CODE,
    BLOCK_HEADING,
    BLOCK_LIST,
    BLOCK_PARAGRAPH,
    BLOCK_TABLE,
    ParseResult,
    ParsedBlock,
)

log = logging.getLogger("pi.rag.parser")

# Text-density floor: a text-bearing PDF page yields hundreds of chars; a
# scanned page yields ~0. Below this average -> needs_heavy_parser.
DEFAULT_MIN_DENSITY = 50.0
# Garbled-char ceiling: pdfplumber on some CJK PDFs emits private-use-area
# glyphs (U+E000-F8FF) or replacement chars. Above this ratio -> not usable.
MAX_GARBLED_RATIO = 0.30

_GARBLED_RE = re.compile(r"[\ue000-\uf8ff\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_RE = re.compile(r"[A-Za-z]")

# PDF magic: %PDF-
_PDF_MAGIC = b"%PDF-"
# OOXML (docx/xlsx) magic: PK zip
_ZIP_MAGIC = b"PK\x03\x04"


class ParseError(Exception):
    """Unparseable file (corrupt, encrypted, unsupported). Ingest marks the
    doc FAILED with this message - one bad file never kills a batch."""


def sniff_kind(path: Path) -> str:
    """Extension + magic-byte sniffing -> a canonical kind string.

    Extension lies sometimes (renamed files); magic bytes don't. We trust
    bytes when they disagree with the extension.
    """
    ext = path.suffix.lower()
    try:
        head = path.open("rb").read(8)
    except OSError:
        head = b""
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        # docx and xlsx are both zip; peek at content types
        try:
            import zipfile

            names = zipfile.ZipFile(path).namelist()
            if any(n.startswith("word/") for n in names):
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
        except Exception:  # noqa: BLE001 - fall through to extension
            pass
        return ext.lstrip(".") or "zip"
    return {
        ".txt": "txt", ".md": "md", ".markdown": "md", ".html": "html",
        ".htm": "html", ".csv": "csv", ".tsv": "tsv",
    }.get(ext, ext.lstrip(".") or "bin")


def _quality_verdict(
    text: str,
    page_count: int | None,
    min_density: float,
    *,
    scannable: bool = False,
) -> tuple[float, bool, str]:
    """(quality 0-1, needs_heavy_parser, reason).

    ``scannable`` marks backends where empty/low-density output plausibly means
    a SCANNED document that an OCR heavy-parser could recover (PDF, images).
    For text-native formats (txt/md/html/csv/docx/xlsx) an empty result is just
    an empty file - OCR cannot help, so we must NOT claim needs_heavy_parser
    (that would misroute the doc and mislead ops). Those fall through with
    quality 0 and let ingest terminate them as FAILED via the empty-chunk gate.
    """
    if not text.strip():
        if scannable:
            return 0.0, True, "no extractable text (scanned or image-only?)"
        return 0.0, False, "no extractable text (empty document)"
    garbled = len(_GARBLED_RE.findall(text))
    ratio = garbled / max(1, len(text))
    if ratio > MAX_GARBLED_RATIO:
        if scannable:
            return round(1.0 - ratio, 3), True, f"garbled char ratio {ratio:.0%} (scanned/bad extraction?)"
        # text-native garbling is an encoding defect, not an OCR candidate:
        # low quality, but heavy-parser won't fix it.
        return round(1.0 - ratio, 3), False, f"garbled char ratio {ratio:.0%} (encoding defect?)"
    if page_count:
        density = len(text) / page_count
        if density < min_density:
            return round(min(1.0, density / min_density), 3), True, (
                f"text density {density:.0f} chars/page < {min_density:.0f} (likely scanned)"
            )
    # crude quality: penalize garbled ratio continuously
    return round(1.0 - ratio, 3), False, ""


def _detect_language(text: str) -> str:
    cjk = len(_CJK_RE.findall(text[:5000]))
    ascii_n = len(_ASCII_RE.findall(text[:5000]))
    if cjk and ascii_n:
        return "mixed" if min(cjk, ascii_n) / max(cjk, ascii_n) > 0.2 else ("zh" if cjk > ascii_n else "en")
    return "zh" if cjk else "en"


# ---- 页眉/页脚检测 + 排版质量信号 -----------------------------------------
# 通用性设计：不匹配任何具体期刊名/文档名，只用两个**可量化、与文档无关**的信号：
#   1) 跨页重复行频率（数字归一化后）—— 页眉页脚在几乎每页重复，正文不会；
#   2) 指纹是否只含"数字占位符+标点"（页码/装饰/分隔线这类短行）。
# 由这两个信号算出"页眉页脚污染率"，既是过滤依据，也是判断"该 PDF 是否该
# 交给 layout-aware 的 heavy parser"的通用排版质量分。
_HEADER_FREQ = 0.4      # 含文字的行（期刊名/卷期）出现在 ≥40% 页才算页眉
_HEADER_NUM_FREQ = 0.6  # 纯数字/符号行（页码/装饰）阈值更高：正文独立数字行罕见，宁可不杀
_HEADER_MIN_LEN = 4     # 含文字指纹的最短长度；再短且非纯数字符号的，不判
# 双栏布局检测：几何行内相邻字符的最大 x 间隙（中缝）的中位数。
# 关键事实（实测）：pdfplumber 对双栏页面把左右栏拼成一条**全宽行**，左右栏交界
# 处有一条无字符的竖缝——这条拼接行内会出现一个远大于词间距（~16pt）的间隙
# （双栏期刊实测 33~98pt）。单栏文本的行内最大间隙就是词间距（~16pt），且页与页
# 之间极其稳定。因此"行内最大间隙中位数"能可靠区分两者，且与文档内容/语言/期刊
# 名无关（只依赖字符坐标）。
#
# 早先的"30%~70% 分位跨度"算法是错的：它对任何填满页宽的文本（无论单双栏）都
# 给出 ~0.4，把单行长文本（如 tests 里的 mini PDF）误判成双栏。已废弃。
_COLUMN_SEAM_THRESHOLD = 20.0  # 行内最大字符间隙中位数 > 此值（pt）= 双栏/表格
_MULTI_COLUMN_PAGE_RATIO = 0.5  # 超过一半页是双栏/表格 → 路由 layout-aware heavy parser


def _line_fingerprint(line: str) -> str:
    """数字归一化 + 去空白 + 大小写归一化：让只差页码/大小写的行指纹相同。"""
    return re.sub(r"\s+", "", re.sub(r"\d+", "#", line.strip())).lower()


def _is_numeric_symbol_fp(fp: str) -> bool:
    """指纹是否只由数字占位符和标点组成（页码 "#"、装饰 "··"、分隔线 "--"）。"""
    return bool(fp) and not re.search(r"[\u4e00-\u9fffA-Za-z]", fp)


def _intra_line_gap_median(page) -> float:
    """单页所有几何行内相邻字符的最大 x 间隙的中位数（pt）。

    双栏/表格页的拼接行里，左右栏交界（或表格列间）有一条无字符的宽缝，行内
    最大间隙会远大于词间距；单栏文本的行内最大间隙就是词间空格。返回中位数
    （而非最大值）以免疫个别含装饰分隔线的行。
    """
    line_max_gaps: list[float] = []
    for line in page.extract_text_lines():
        xs = sorted(
            c.get("x0") for c in line.get("chars", [])
            if isinstance(c.get("x0"), (int, float))
        )
        if len(xs) >= 3:
            gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
            line_max_gaps.append(max(gaps))
    if not line_max_gaps:
        return 0.0
    line_max_gaps.sort()
    return line_max_gaps[len(line_max_gaps) // 2]


def _filter_repeated_lines(pages: list[str]) -> tuple[list[str], float]:
    """剔除跨页重复的页眉/页脚行，返回 (过滤后的每页文本, 污染率)。

    污染率 = 被识别为页眉页脚的字符数 / 总字符数，是给调用方的**通用排版
    质量信号**（见 parse_pdf）：污染率高通常意味着双栏期刊式排版，pdfplumber
    的抽取（页眉混入 + 双栏交错）质量差，应路由 heavy parser。
    """
    n = len(pages)
    total_chars = sum(len(t) for t in pages)
    if n < 3:
        return pages, 0.0  # 太短的文件无从判断"跨页重复"
    paged = [[ln.strip() for ln in t.splitlines() if ln.strip()] for t in pages]
    freq: dict[str, int] = {}
    for lines in paged:
        seen: set[str] = set()  # 同一页内重复只计一次，避免高估
        for ln in lines:
            fp = _line_fingerprint(ln)
            if fp and fp not in seen:
                seen.add(fp)
                freq[fp] = freq.get(fp, 0) + 1
    text_threshold = max(2, int(n * _HEADER_FREQ))
    num_threshold = max(2, int(n * _HEADER_NUM_FREQ))
    header = {
        fp for fp, hits in freq.items()
        if (len(fp) >= _HEADER_MIN_LEN and hits >= text_threshold)
        or (_is_numeric_symbol_fp(fp) and hits >= num_threshold)
    }
    if not header:
        return pages, 0.0
    removed = 0
    filtered: list[str] = []
    for lines in paged:
        kept = [ln for ln in lines if _line_fingerprint(ln) not in header]
        removed += sum(len(ln) for ln in lines) - sum(len(ln) for ln in kept)
        filtered.append("\n".join(kept))
    contamination = removed / max(1, total_chars)
    return filtered, contamination


# ---------------------------------------------------------------------------
# Backends: each returns ParseResult. Heavy deps lazy-imported inside.
# ---------------------------------------------------------------------------


def parse_txt(path: Path) -> ParseResult:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError(f"cannot decode {path.name}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [
        ParsedBlock(kind=BLOCK_PARAGRAPH, text=para.strip())
        for para in text.split("\n\n")
        if para.strip()
    ]
    q, heavy, reason = _quality_verdict(text, None, DEFAULT_MIN_DENSITY)
    return ParseResult(
        blocks=blocks, title=path.stem, language=_detect_language(text),
        char_count=len(text), quality=q, needs_heavy_parser=heavy,
        reason=reason, backend="txt",
    )


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_MD_FENCE = re.compile(r"^(```|~~~)")

# -- inline HTML inside markdown -------------------------------------------
# Real-world exports (语雀/Notion/Confluence -> .md) are markdown in name only:
# headings arrive as <h1 id="...">, every run of text is wrapped in
# <font style="color:...">, and tables are pipe grids of styled spans. Feeding
# that raw to an embedder wastes the vector budget on markup and - worse -
# loses the heading hierarchy that title_path/contextual retrieval depends on.
_HTML_BLOCK_HEADING = re.compile(r"<(h[1-6])\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
_HTML_BR = re.compile(r"<br\s*/?>", re.I)
_HTML_ANY_TAG = re.compile(r"</?[a-zA-Z][^<>\n]{0,300}>")
# paired emphasis only - a lone `*`/`_` is far more often literal (a * b,
# snake_case) than markup, so single-underscore/asterisk italics are left alone.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_UNDER = re.compile(r"__(.+?)__", re.S)
_MD_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_WS_RUN = re.compile(r"[ \t\u00a0]{2,}")


def _strip_inline_markup(text: str) -> str:
    """Reduce an inline-HTML/markdown-decorated run to clean readable text.

    Order matters: <br> becomes a newline BEFORE generic tags are dropped (or
    the line break is lost), entities are unescaped after tag removal (so
    &lt;div&gt; survives as literal text instead of being eaten as a tag).
    """
    if not text:
        return ""
    out = _HTML_BR.sub("\n", text)
    out = _HTML_ANY_TAG.sub("", out)
    out = html.unescape(out)
    out = _MD_BOLD.sub(r"\1", out)
    out = _MD_UNDER.sub(r"\1", out)
    out = _MD_CODE_SPAN.sub(r"\1", out)
    out = _WS_RUN.sub(" ", out)
    return out.strip()


# -- layout-model markdown cleaning (PaddleOCR / MinerU output) ------------
# Layout models (RAGFlow-style DeepDoc, PaddleOCR-VL, MinerU) return "markdown"
# that is really HTML+LaTeX: tables as <table><tr><td>...</td></tr></table>,
# centred captions as <div style=...>, images as <img src="imgs/...">, and math
# as $ \omega $ / $ ^{[16]} $. Feeding that raw to the chunker wastes the vector
# budget on markup and - worse - loses table structure (cells glued together)
# and lets citation superscripts ($ ^{[16]} $) leak in as noise.
#
# This cleaner is VENDOR-NEUTRAL and CONTENT-NEUTRAL: it targets the OUTPUT
# SHAPE (HTML table, inline math, self-closing img), never a specific document,
# journal, or language. MinerU can reuse it unchanged when a service exists.

# Inline math $...$ (never display $$...$$, which layout models rarely emit in
# doc parsing; if it appears the same regex still degrades gracefully).
_LATEX_INLINE = re.compile(r"\$([^$]+)\$")

# Common LaTeX symbol commands -> Unicode. A generic map, not document-specific:
# these glyphs are what an embedder understands better than raw TeX control words.
_LATEX_SYMBOLS = {
    # Greek (lower + upper)
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ",
    r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π", r"\rho": "ρ",
    r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ", r"\Omega": "Ω",
    # operators / relations
    r"\geq": "≥", r"\ge": "≥", r"\leq": "≤", r"\le": "≤",
    r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡",
    r"\pm": "±", r"\mp": "∓", r"\times": "×", r"\div": "÷",
    r"\cdot": "·", r"\cdots": "…", r"\ldots": "…",
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←",
    r"\leftrightarrow": "↔", r"\Rightarrow": "⇒", r"\Leftarrow": "⇐",
    r"\infty": "∞", r"\propto": "∝", r"\sim": "~", r"\in": "∈",
    r"\notin": "∉", r"\subset": "⊂", r"\supset": "⊃", r"\cup": "∪",
    r"\cap": "∩", r"\forall": "∀", r"\exists": "∃", r"\partial": "∂",
    r"\nabla": "∇", r"\degree": "°", r"\deg": "°",
}
# Formatting-prefix commands: the semantic content lives in the {braces}, so
# drop the control word (e.g. \text{foo} -> foo, \mathrm{foo} -> foo).
_LATEX_FORMAT_PREFIX = re.compile(r"\\[a-zA-Z]+\s*\{")


def _latex_inline_to_text(text: str) -> str:
    """Inline LaTeX ``$...$`` -> readable plain text.

    Rules (all content-neutral):
    - known symbol commands -> Unicode (``\\omega`` -> ω, ``\\geq`` -> ≥);
    - superscript/subscript markers (``^``/``_``) and braces are flattened, so a
      citation superscript ``$ ^{[16]} $`` becomes ``[16]`` and a table footnote
      ``$ ^{{a}} $`` becomes ``a``;
    - formatting prefixes (``\\text{...}``) drop the control word, keep the body;
    - any leftover control word (e.g. a unit ``\\mmol``) keeps its NAME with the
      backslash stripped (-> ``mmol``), so ``$ \\geq 2.3\\ mmol/L $`` -> ``≥ 2.3 mmol/L``.
    """
    def repl(m: re.Match) -> str:
        body = m.group(1)
        body = _LATEX_FORMAT_PREFIX.sub("{", body)
        for cmd, ch in _LATEX_SYMBOLS.items():
            body = body.replace(cmd, ch)
        body = re.sub(r"[\^_]", "", body)  # flatten sup/sub
        body = body.replace("{", "").replace("}", "")
        body = body.replace("\\", "")  # leftover control words keep their name
        return body.strip()

    return _LATEX_INLINE.sub(repl, text)


_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.I | re.S)
_HTML_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_HTML_CELL = re.compile(r"<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>", re.I | re.S)


def _html_table_to_markdown(text: str) -> str:
    """``<table>...</table>`` -> markdown pipe grid (``| cell | cell |``).

    Each ``<tr>`` becomes one row, each ``<td>/<th>`` one cell. Row/col spans
    are FLATTENED (a merged cell is emitted once, at its first position): the
    merge *shape* is a rendering concern, retrieval needs the cell TEXT and the
    row grouping, both of which survive. The pipe grid is then recognised by
    parse_markdown as BLOCK_TABLE (atomic in the chunker).
    """
    def table_repl(m: re.Match) -> str:
        rows: list[str] = []
        for tr in _HTML_TR.finditer(m.group(0)):
            cells: list[str] = []
            for cm in _HTML_CELL.finditer(tr.group(1)):
                cell = _HTML_ANY_TAG.sub("", cm.group(1))
                cell = html.unescape(cell)
                # Layout models double-escape newlines INSIDE a cell (a literal
                # backslash-n, not a real line break): flatten to a space so the
                # pipe grid stays one line per row.
                cell = cell.replace("\\n", " ").replace("\\r", " ")
                cell = re.sub(r"\s+", " ", cell).strip()
                if cell:
                    cells.append(cell)
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows)

    return _HTML_TABLE.sub(table_repl, text)


def clean_ocr_markdown(text: str) -> str:
    """Layout-model output -> clean Markdown (used by heavy-parser ingest).

    Pipeline (order matters): inline LaTeX first (so table cells and captions
    both get plain text), then HTML tables -> pipe grids, then strip the
    remaining HTML tags (div/span/img/p) keeping their text, and finally
    collapse the blank-line noise the tag removal leaves behind.
    """
    if not text:
        return ""
    out = _latex_inline_to_text(text)
    out = _html_table_to_markdown(out)
    out = _HTML_ANY_TAG.sub("", out)
    out = html.unescape(out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _split_html_headings(line: str) -> list[tuple[int | None, str]]:
    """Split one line into (heading_level | None, text) runs.

    Handles the export quirk of several block headings on a single line
    ("<h1>二.方法</h1> <h2>1.人工评估</h2>") - each must become its own block
    or the heading stack collapses two levels into one.
    """
    out: list[tuple[int | None, str]] = []
    pos = 0
    for m in _HTML_BLOCK_HEADING.finditer(line):
        pre = line[pos : m.start()]
        if pre.strip():
            out.append((None, pre))
        out.append((int(m.group(1)[1]), m.group(2)))
        pos = m.end()
    tail = line[pos:]
    if tail.strip():
        out.append((None, tail))
    return out or [(None, line)]


def _is_pipe_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


_PIPE_SEP = re.compile(r"^\|[\s:\-|]+\|$")


def _pipe_table_to_text(lines: list[str]) -> str:
    """Markdown pipe grid -> newline-joined "cell | cell" rows (one BLOCK_TABLE).

    Separator rows (| --- | --- |) are dropped, cells are de-marked-up. Keeping
    the grid as ONE block lets the chunker treat it atomically.
    """
    rows: list[str] = []
    for ln in lines:
        s = ln.strip()
        if _PIPE_SEP.match(s):
            continue
        cells = [_strip_inline_markup(c) for c in s.strip("|").split("|")]
        if any(cells):
            rows.append(" | ".join(c for c in cells))
    return "\n".join(rows)


def parse_markdown(path: Path) -> ParseResult:
    """Markdown -> blocks with heading levels preserved (drives title_path)."""
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError(f"cannot decode {path.name}")
    text = text.replace("\r\n", "\n")

    blocks: list[ParsedBlock] = []
    in_fence = False
    fence_lines: list[str] = []
    para: list[str] = []
    list_buf: list[str] = []
    table_buf: list[str] = []
    title = path.stem
    title_locked = False

    def flush_para() -> None:
        if para:
            # join wrapped lines, then de-markup the whole run once
            joined = _strip_inline_markup(" ".join(l.strip() for l in para))
            if joined:
                blocks.append(ParsedBlock(kind=BLOCK_PARAGRAPH, text=joined))
            para.clear()

    def flush_list() -> None:
        if list_buf:
            cleaned = "\n".join(_strip_inline_markup(x) for x in list_buf).strip()
            if cleaned:
                blocks.append(ParsedBlock(kind=BLOCK_LIST, text=cleaned))
            list_buf.clear()

    def flush_table() -> None:
        if table_buf:
            grid = _pipe_table_to_text(table_buf)
            if grid:
                blocks.append(ParsedBlock(kind=BLOCK_TABLE, text=grid))
            table_buf.clear()

    def flush_all() -> None:
        flush_para()
        flush_list()
        flush_table()

    for line in text.split("\n"):
        # fenced code wins over everything (a pipe row or <h1> inside a fence
        # is literal source, not structure)
        if _MD_FENCE.match(line.strip()):
            if in_fence:
                blocks.append(ParsedBlock(kind=BLOCK_CODE, text="\n".join(fence_lines)))
                fence_lines.clear()
            else:
                flush_all()
            in_fence = not in_fence
            continue
        if in_fence:
            fence_lines.append(line)
            continue

        if not line.strip():
            flush_all()
            continue

        # ATX heading (#, ##, ...) - level from the hashes
        m = _MD_HEADING.match(line)
        if m:
            flush_all()
            level = len(m.group(1))
            heading = _strip_inline_markup(m.group(2))
            if heading:
                if level == 1 and not title_locked:
                    title, title_locked = heading, True
                blocks.append(ParsedBlock(kind=BLOCK_HEADING, text=heading, level=level))
            continue

        # pipe table row: buffer until a blank/non-table line ends the grid
        if _is_pipe_row(line):
            flush_para()
            flush_list()
            table_buf.append(line)
            continue
        flush_table()

        # HTML block heading(s) possibly sharing a line (语雀/Notion export):
        # "<h1>二.方法</h1> <h2>1.人工评估</h2>" -> one block per heading.
        runs = _split_html_headings(line)
        if any(lvl is not None for lvl, _ in runs):
            flush_para()
            flush_list()
            for lvl, seg in runs:
                clean = _strip_inline_markup(seg)
                if not clean:
                    continue
                if lvl is None:
                    para.append(clean)
                else:
                    if lvl == 1 and not title_locked:
                        title, title_locked = clean, True
                    blocks.append(ParsedBlock(kind=BLOCK_HEADING, text=clean, level=lvl))
            continue

        # bullet / ordered list item
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            flush_para()
            list_buf.append(line.rstrip())
            continue

        flush_list()
        para.append(line)

    if in_fence and fence_lines:  # unclosed fence at EOF
        blocks.append(ParsedBlock(kind=BLOCK_CODE, text="\n".join(fence_lines)))
    flush_all()

    plain = "\n\n".join(b.text for b in blocks)
    q, heavy, reason = _quality_verdict(plain, None, DEFAULT_MIN_DENSITY)
    return ParseResult(
        blocks=blocks, title=title, language=_detect_language(plain),
        char_count=len(plain), quality=q, needs_heavy_parser=heavy,
        reason=reason, backend="markdown",
    )


def parse_html(path: Path) -> ParseResult:
    """HTML -> blocks. bs4 lazy-imported; script/style/nav stripped."""
    from bs4 import BeautifulSoup  # noqa: PLC0415

    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError(f"cannot decode {path.name}")
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else path.stem

    blocks: list[ParsedBlock] = []
    body = soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]):
        if el.name == "table":
            rows = []
            for tr in el.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(ParsedBlock(kind=BLOCK_TABLE, text="\n".join(rows)))
        elif el.name.startswith("h"):
            txt = el.get_text(" ", strip=True)
            if txt:
                blocks.append(ParsedBlock(kind=BLOCK_HEADING, text=txt, level=int(el.name[1])))
        elif el.name == "pre":
            txt = el.get_text()
            if txt.strip():
                blocks.append(ParsedBlock(kind=BLOCK_CODE, text=txt.strip()))
        elif el.name == "li":
            txt = el.get_text(" ", strip=True)
            if txt:
                blocks.append(ParsedBlock(kind=BLOCK_LIST, text=f"- {txt}"))
        else:
            txt = el.get_text(" ", strip=True)
            if txt:
                blocks.append(ParsedBlock(kind=BLOCK_PARAGRAPH, text=txt))

    plain = "\n\n".join(b.text for b in blocks)
    q, heavy, reason = _quality_verdict(plain, None, DEFAULT_MIN_DENSITY)
    return ParseResult(
        blocks=blocks, title=title, language=_detect_language(plain),
        char_count=len(plain), quality=q, needs_heavy_parser=heavy,
        reason=reason, backend="bs4",
    )


def parse_pdf(path: Path, max_pages: int = 500, min_density: float = DEFAULT_MIN_DENSITY) -> ParseResult:
    """Text-bearing PDF via pdfplumber (lazy import, blocking -> to_thread is
    the CALLER's job; this function is sync and ingest wraps it).

    Scanned/image-only PDFs are DETECTED here (density gate) and flagged
    needs_heavy_parser - v1 does not OCR. Tables are extracted per page and
    emitted as BLOCK_TABLE so the chunker keeps rows together.
    """
    import pdfplumber  # noqa: PLC0415

    blocks: list[ParsedBlock] = []
    page_count = 0
    total_chars = 0
    truncated = False
    multi_column_pages = 0
    try:
        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            if page_count > max_pages:
                truncated = True
            pages = list(pdf.pages[:max_pages])
            # 双栏/表格检测（行内最大字符间隙中位数，通用）：pdfplumber 对双栏
            # 的抽取不可靠（左右栏交错成一条全宽行），这类文档应路由 layout-aware
            # 的 heavy parser。表格页的列间空白同样会产生大间隙，而 layout 模型
            # 对表格的结构化本就更好，所以一并路由——保守、安全。
            for page in pages:
                if _intra_line_gap_median(page) >= _COLUMN_SEAM_THRESHOLD:
                    multi_column_pages += 1
            # 先抽每页文本，剔除跨页重复的页眉/页脚，再切段落——否则期刊的
            # 页眉会混进正文、污染 embedding、挤占检索 top1。
            texts, _ = _filter_repeated_lines(
                [page.extract_text() or "" for page in pages]
            )
            for i, (page, text) in enumerate(zip(pages, texts)):
                total_chars += len(text.strip())
                # tables first (as structured rows), then non-table text
                try:
                    tables = page.extract_tables() or []
                except Exception:  # noqa: BLE001 - one bad page never kills the file
                    tables = []
                for tbl in tables:
                    rows = [" | ".join((c or "").replace("\n", " ").strip() for c in row)
                            for row in tbl if row and any((c or "").strip() for c in row)]
                    if rows:
                        blocks.append(ParsedBlock(kind=BLOCK_TABLE, text="\n".join(rows), page=i + 1))
                for para in re.split(r"\n\s*\n", text):
                    para = para.strip()
                    if para:
                        blocks.append(ParsedBlock(kind=BLOCK_PARAGRAPH, text=para, page=i + 1))
    except Exception as exc:  # noqa: BLE001 - encrypted/corrupt PDF
        raise ParseError(f"pdfplumber failed on {path.name}: {exc}") from exc

    plain = "\n\n".join(b.text for b in blocks)
    # PDF is the scannable backend: empty/low-density output plausibly means a
    # scanned page that OCR could recover, so needs_heavy_parser is legitimate
    # here (unlike text-native formats).
    q, heavy, reason = _quality_verdict(plain, page_count or None, min_density, scannable=True)
    # 通用排版信号：双栏布局 → pdfplumber 抽取不可靠，路由 heavy parser。
    # layout-aware OCR（PaddleOCR/MinerU）能还原双栏顺序、剔除页眉页脚。
    if not heavy and page_count and multi_column_pages / page_count >= _MULTI_COLUMN_PAGE_RATIO:
        heavy = True
        reason = (reason + "; " if reason else "") + (
            f"multi-column layout ({multi_column_pages}/{page_count} pages)"
        )
    if truncated and not heavy:
        reason = (reason + "; " if reason else "") + f"truncated at {max_pages}/{page_count} pages"
    return ParseResult(
        blocks=blocks, title=path.stem, language=_detect_language(plain),
        page_count=page_count, char_count=len(plain), quality=q,
        needs_heavy_parser=heavy, reason=reason, backend="pdfplumber",
    )


def parse_docx(path: Path) -> ParseResult:
    """Word via python-docx (lazy). Heading styles -> BLOCK_HEADING w/ level;
    tables -> BLOCK_TABLE rows. Image-only docx -> density gate flags it."""
    import docx  # noqa: PLC0415

    try:
        d = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"python-docx failed on {path.name}: {exc}") from exc

    blocks: list[ParsedBlock] = []
    title = path.stem
    # Document body order matters (interleaved paragraphs/tables); python-docx
    # exposes them via the XML body children.
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = d.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            p = Paragraph(child, d)
            txt = p.text.strip()
            if not txt:
                continue
            style = (p.style.name or "").lower() if p.style else ""
            m = re.match(r"heading (\d)", style)
            if m:
                level = int(m.group(1))
                if level == 1 and title == path.stem:
                    title = txt
                blocks.append(ParsedBlock(kind=BLOCK_HEADING, text=txt, level=level))
            elif style.startswith("title") and title == path.stem:
                title = txt
                blocks.append(ParsedBlock(kind=BLOCK_HEADING, text=txt, level=1))
            else:
                blocks.append(ParsedBlock(kind=BLOCK_PARAGRAPH, text=txt))
        elif child.tag.endswith("}tbl"):
            t = Table(child, d)
            rows = []
            for row in t.rows:
                cells = [c.text.replace("\n", " ").strip() for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(ParsedBlock(kind=BLOCK_TABLE, text="\n".join(rows)))

    plain = "\n\n".join(b.text for b in blocks)
    q, heavy, reason = _quality_verdict(plain, None, DEFAULT_MIN_DENSITY)
    return ParseResult(
        blocks=blocks, title=title, language=_detect_language(plain),
        char_count=len(plain), quality=q, needs_heavy_parser=heavy,
        reason=reason, backend="python-docx",
    )


def _rows_to_blocks(header: list[str], rows: list[list[str]], sheet: str = "") -> list[ParsedBlock]:
    """Tabular rows -> blocks of N rows each, every row 'col: val' serialized.

    Why serialize instead of raw CSV lines: retrieval matches natural-language
    questions against column NAMES ('保险理赔金额' hits 'total_claim_amount: 57700'
    only if the name is in the text). Raw positional CSV can't be matched by
    column semantics. Batching rows keeps chunks information-dense.
    """
    blocks: list[ParsedBlock] = []
    ROWS_PER_BLOCK = 20
    prefix = f"[{sheet}] " if sheet else ""
    for i in range(0, len(rows), ROWS_PER_BLOCK):
        batch = rows[i : i + ROWS_PER_BLOCK]
        lines = []
        for row in batch:
            pairs = [f"{h}: {v}" for h, v in zip(header, row) if str(v).strip() not in ("", "?", "None", "nan")]
            if pairs:
                lines.append(prefix + "; ".join(pairs))
        if lines:
            blocks.append(ParsedBlock(kind=BLOCK_TABLE, text="\n".join(lines)))
    return blocks


def parse_csv(path: Path, delimiter: str = ",") -> ParseResult:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError(f"cannot decode {path.name}")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError(f"{path.name} is empty") from None
    header = [h.strip() for h in header]
    rows = [r for r in reader if any(c.strip() for c in r)]
    blocks = _rows_to_blocks(header, rows)
    plain = "\n\n".join(b.text for b in blocks)
    summary = f"columns({len(header)}): " + ", ".join(header[:20])
    blocks.insert(0, ParsedBlock(kind=BLOCK_PARAGRAPH, text=f"{summary}; rows: {len(rows)}"))
    q, heavy, reason = _quality_verdict(plain, None, DEFAULT_MIN_DENSITY)
    return ParseResult(
        blocks=blocks, title=path.stem, language=_detect_language(plain),
        char_count=len(plain), quality=q, needs_heavy_parser=heavy,
        reason=reason, backend="csv",
    )


def parse_xlsx(path: Path) -> ParseResult:
    import openpyxl  # noqa: PLC0415

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"openpyxl failed on {path.name}: {exc}") from exc
    blocks: list[ParsedBlock] = []
    try:
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header = [str(c).strip() if c is not None else "" for c in next(rows_iter)]
            except StopIteration:
                continue
            rows = [
                [str(c).strip() if c is not None else "" for c in row]
                for row in rows_iter
                if any(c is not None and str(c).strip() for c in row)
            ]
            if rows:
                blocks.extend(_rows_to_blocks(header, rows, sheet=sheet))
    finally:
        wb.close()
    plain = "\n\n".join(b.text for b in blocks)
    q, heavy, reason = _quality_verdict(plain, None, DEFAULT_MIN_DENSITY)
    return ParseResult(
        blocks=blocks, title=path.stem, language=_detect_language(plain),
        char_count=len(plain), quality=q, needs_heavy_parser=heavy,
        reason=reason, backend="openpyxl",
    )


def parse_image(path: Path) -> ParseResult:
    """v1 does NOT OCR. Images are flagged for the heavy-parser service."""
    return ParseResult(
        blocks=[], title=path.stem, quality=0.0, needs_heavy_parser=True,
        reason="image file: OCR required (heavy parser service, v1.5)", backend="none",
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_BACKENDS = {
    "txt": parse_txt,
    "md": parse_markdown,
    "html": parse_html,
    "csv": parse_csv,
    "tsv": lambda p: parse_csv(p, delimiter="\t"),
    "xlsx": parse_xlsx,
    "docx": parse_docx,
}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"}
_UNSUPPORTED = {"zip", "rar", "7z", "exe", "dll", "so", "bin", "ppt", "pptx"}  # ppt*: v1.5 heavy


def can_parse(kind: str) -> bool:
    return kind in _BACKENDS or kind == "pdf" or kind in _IMAGE_EXTS


def parse_file(
    path: str | Path,
    *,
    max_pdf_pages: int = 500,
    min_density: float = DEFAULT_MIN_DENSITY,
) -> ParseResult:
    """Route one file to its backend. Raises ParseError for unsupported/
    corrupt input (ingest isolates it per-file); returns needs_heavy_parser
    results for scanned/image/complex files (ingest marks, never crashes)."""
    p = Path(path)
    if not p.is_file():
        raise ParseError(f"not a file: {p}")
    kind = sniff_kind(p)
    log.info("parsing %s (kind=%s)", p.name, kind)
    if kind == "pdf":
        return parse_pdf(p, max_pages=max_pdf_pages, min_density=min_density)
    if kind in _IMAGE_EXTS:
        return parse_image(p)
    if kind in _BACKENDS:
        return _BACKENDS[kind](p)
    raise ParseError(f"unsupported file kind {kind!r} for {p.name} (v1: text formats only)")
