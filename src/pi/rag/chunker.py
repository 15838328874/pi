"""Semantic chunking + contextual retrieval (Anthropic-style, cheap tier).

Structure-aware, NOT fixed-count slicing (notebook Part3 结论 + RAG_DESIGN
§2.2): walk the parsed blocks maintaining a heading stack; accumulate
paragraph text up to max_chars; split oversized blocks at sentence
boundaries with overlap; tables and code stay ATOMIC (never split a table
row set mid-way - retrieval needs whole rows to make sense).

Contextual retrieval v1 = title_path prefix on the EMBED text only:
  embed_text = "文档标题 > 章节 > 小节\n\n" + text
The displayed/cited text stays clean. Zero LLM cost. The LLM-generated
context tier (Anthropic full version) is reserved behind
ChunkingConfig.contextual_llm (v1.5) - the seam is embed_text vs text.

The chunker is PURE: ParseResult -> list[ChunkDraft]. No store, no user,
no ids - ingest binds those. That keeps it unit-testable and reusable.
"""

from __future__ import annotations

import re

from pi.rag.config import ChunkingConfig
from pi.rag.types import (
    BLOCK_CODE,
    BLOCK_HEADING,
    BLOCK_LIST,
    BLOCK_PARAGRAPH,
    BLOCK_TABLE,
    ChunkDraft,
    ParseResult,
    ParsedBlock,
)

# Sentence boundaries: CJK full stops + ascii terminators followed by space/EOL.
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])|(?<=[.])\s+")


def _split_sentences(text: str) -> list[str]:
    parts = [p for p in _SENT_SPLIT.split(text) if p and p.strip()]
    return parts or [text]


#: Blocks that must never absorb foreign prose (nor be absorbed into it).
#: A table row set / code body only makes sense whole and unmixed - merging a
#: runt paragraph into them corrupts the very thing retrieval needs.
_ATOMIC_KINDS = frozenset({BLOCK_TABLE, BLOCK_CODE})

#: Sentence-ish terminators used to snap an overlap tail back to a boundary.
_BOUNDARY = re.compile(r"[。！？；!?;.\n]")


class Chunker:
    def __init__(self, cfg: ChunkingConfig | None = None) -> None:
        self.cfg = cfg or ChunkingConfig()

    # -- public API ---------------------------------------------------------

    def chunk(self, parsed: ParseResult) -> list[ChunkDraft]:
        """ParseResult -> ordered ChunkDrafts (seq assigned 0..n-1)."""
        blocks = self._clean_blocks(parsed.blocks)
        if not blocks:
            return []
        acc: list[ParsedBlock] = []  # paragraph/list accumulation buffer
        acc_len = 0
        heading_stack: list[str] = []
        drafts: list[ChunkDraft] = []

        def flush() -> None:
            nonlocal acc, acc_len
            if not acc:
                return
            text = "\n\n".join(b.text for b in acc).strip()
            if text:
                pages = [b.page for b in acc if b.page is not None]
                drafts.append(self._make_draft(text, heading_stack, pages[0] if pages else None, BLOCK_PARAGRAPH))
            acc = []
            acc_len = 0

        for b in blocks:
            if b.kind == BLOCK_HEADING:
                flush()
                self._push_heading(heading_stack, b)
                continue
            if b.kind in (BLOCK_TABLE, BLOCK_CODE):
                # atomic: tables/code never merge into paragraph flow nor split
                flush()
                for piece in self._split_oversized(b.text, atomic=True):
                    drafts.append(self._make_draft(piece, heading_stack, b.page, b.kind))
                continue
            # paragraph / list: accumulate to target size
            if acc_len + len(b.text) > self.cfg.max_chars and acc:
                flush()
            if len(b.text) > self.cfg.hard_max_chars:
                flush()
                for piece in self._split_oversized(b.text, atomic=False):
                    drafts.append(self._make_draft(piece, heading_stack, b.page, b.kind))
                continue
            acc.append(b)
            acc_len += len(b.text)
        flush()

        # Merge runt chunks (< min_chars) into a same-kind neighbour. Atomic
        # blocks (tables/code) are excluded on BOTH sides: they must stay
        # whole and unmixed. Backward pass first, then forward for runts
        # stranded right after an atomic block (code/table followed by a
        # one-line paragraph) - otherwise they would survive as junk chunks.
        merged = self._merge_runts(drafts)
        # re-seq after merges
        for i, d in enumerate(merged):
            d.seq = i
        return merged

    # -- runt merging -------------------------------------------------------

    def _can_absorb(self, host: ChunkDraft, runt: ChunkDraft) -> bool:
        """Whether `host` may swallow `runt` without breaking a contract."""
        if host.kind != runt.kind:
            return False  # never mix prose into a table/code body
        if runt.kind in _ATOMIC_KINDS:
            return False  # atomic pieces are already correctly sized by design
        if host.title_path != runt.title_path:
            return False  # never merge across a heading boundary - it would
            # mislabel the merged chunk's citation path (contextual retrieval)
        return len(host.text) + len(runt.text) + 2 <= self.cfg.hard_max_chars

    def _absorb(self, host: ChunkDraft, runt: ChunkDraft) -> None:
        host.text = (host.text + "\n\n" + runt.text).strip()
        host.embed_text = self._contextual(prefix=host.title_path, text=host.text)

    def _merge_runts(self, drafts: list[ChunkDraft]) -> list[ChunkDraft]:
        if len(drafts) < 2:
            return list(drafts)

        # pass 1: runt joins the PREVIOUS chunk
        out: list[ChunkDraft] = []
        for d in drafts:
            if out and len(d.text) < self.cfg.min_chars and self._can_absorb(out[-1], d):
                self._absorb(out[-1], d)
                continue
            out.append(d)

        # pass 2: still-runt chunks try the NEXT chunk, then fall back to the
        # nearest preceding NON-atomic chunk in the same section. A short code
        # snippet / table is never a runt (it is atomic by design, its size is
        # not a defect); and a runt is never dropped - losing text would be an
        # invisible recall loss, worse than one small chunk.
        result: list[ChunkDraft] = []
        i = 0
        while i < len(out):
            d = out[i]
            if d.kind in _ATOMIC_KINDS or len(d.text) >= self.cfg.min_chars:
                result.append(d)
                i += 1
                continue

            host = self._find_forward_host(out, i + 1, d)
            if host is not None:
                host.text = (d.text + "\n\n" + host.text).strip()
                host.embed_text = self._contextual(prefix=host.title_path, text=host.text)
                i += 1
                continue

            host = self._find_backward_host(result, d)
            if host is not None:
                self._absorb(host, d)
                i += 1
                continue

            result.append(d)  # nowhere compatible: keep it, never lose text
            i += 1
        return result

    def _find_forward_host(self, out: list[ChunkDraft], start: int, runt: ChunkDraft) -> ChunkDraft | None:
        for j in range(start, len(out)):
            if self._can_absorb(out[j], runt):
                return out[j]
        return None

    def _find_backward_host(self, result: list[ChunkDraft], runt: ChunkDraft) -> ChunkDraft | None:
        """Nearest preceding non-atomic chunk in the SAME section (title_path).

        Skipping over atomic blocks is deliberate: prose stranded right after a
        code/table block semantically continues the section's prose, and the
        section guard stops a runt from being glued onto unrelated content far
        above it.
        """
        for host in reversed(result):
            if host.kind in _ATOMIC_KINDS:
                continue
            if host.title_path != runt.title_path:
                return None  # left the section - stop searching
            if self._can_absorb(host, runt):
                return host
        return None

    # -- internals ----------------------------------------------------------

    def _push_heading(self, stack: list[str], b: ParsedBlock) -> None:
        level = max(1, min(6, b.level or 1))
        # pop to parent level, then push
        del stack[level - 1 :]
        while len(stack) < level - 1:
            stack.append("")
        stack.append(b.text.strip())

    def _title_path(self, stack: list[str], doc_title: str = "") -> str:
        parts = ([doc_title] if doc_title else []) + [h for h in stack if h]
        return " > ".join(parts)

    def _contextual(self, prefix: str, text: str) -> str:
        if not self.cfg.contextual_prefix or not prefix:
            return text
        return f"{prefix}\n\n{text}"

    def _make_draft(self, text: str, stack: list[str], page: int | None, kind: str) -> ChunkDraft:
        path = self._title_path(stack)
        return ChunkDraft(
            seq=0,  # assigned by caller after merges
            text=text,
            embed_text=self._contextual(path, text),
            title_path=path,
            page=page,
            kind=kind,
        )

    def _split_oversized(self, text: str, *, atomic: bool) -> list[str]:
        """Split >hard_max_chars blocks. atomic=True (tables/code): split by
        LINES only (never mid-row). atomic=False: sentence boundaries with
        overlap."""
        if len(text) <= self.cfg.hard_max_chars:
            return [text]
        pieces: list[str]
        if atomic:
            pieces = text.split("\n")
        else:
            pieces = _split_sentences(text)
            # a single "sentence" can still exceed hard_max (dense CJK w/o
            # punctuation): fall back to line, then char-window split.
            grown: list[str] = []
            for p in pieces:
                if len(p) <= self.cfg.hard_max_chars:
                    grown.append(p)
                else:
                    grown.extend(self._char_windows(p))
            pieces = grown
        # greedy pack with overlap carried into the next chunk
        out: list[str] = []
        cur = ""
        sep = "\n" if atomic else ""
        for p in pieces:
            if not p.strip():
                continue
            if cur and len(cur) + len(sep) + len(p) > self.cfg.hard_max_chars:
                out.append(cur.strip())
                tail = cur[-self.cfg.overlap_chars :] if (self.cfg.overlap_chars and not atomic) else ""
                m = re.search(r"[。！？；!?;.\n]", tail[1:]) if tail else None
                tail = tail[m.start() + 1 :].strip() if (tail and m) else ""
                cur = (tail + sep + p) if tail else p
            else:
                cur = cur + sep + p if cur else p
        if cur.strip():
            out.append(cur.strip())
        return out or [text[: self.cfg.hard_max_chars]]

    def _char_windows(self, text: str) -> list[str]:
        size = self.cfg.hard_max_chars
        step = max(1, size - self.cfg.overlap_chars)
        return [text[i : i + size] for i in range(0, len(text), step)]

    @staticmethod
    def _clean_blocks(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
        """Drop empty/whitespace blocks; normalize newlines. Cleaning at this
        seam (not in parsers) keeps parser output faithful for debugging."""
        out = []
        for b in blocks:
            t = b.text.replace("\r\n", "\n").replace("\r", "\n")
            t = re.sub(r"[ \t]+\n", "\n", t)
            if t.strip():
                out.append(ParsedBlock(kind=b.kind, text=t, level=b.level, page=b.page, meta=b.meta))
        return out
