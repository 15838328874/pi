"""Core data types for the RAG kernel.

Plain dataclasses/enums only - no framework imports. The kernel (``pi.rag``)
is designed to be lifted out of pi wholesale: the ONLY file allowed to
import ``pi.*`` outside the kernel is ``adapters.py`` (pi integration) and
``pi.tools.rag`` (the RagTool shell).

Naming follows the repo's semantic-memory precedent: the SQL row id is the
vector-store PK (``memory_id`` there, ``chunk_id`` here), so the vector index
stays a rebuildable projection of the SQL source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RetrievalMode(str, Enum):
    """Which pipeline actually served a query.

    Surfaced to callers and to ``on_retrieval`` so degradation is never
    silent (house rule:附属系统失败要发噪音，不静默).
    """

    HYBRID = "hybrid"  # vector + BM25 -> RRF (+ optional rerank)
    VECTOR_ONLY = "vector_only"  # lexical index unavailable, vector path ok
    BM25_FALLBACK = "bm25_fallback"  # embedding/vector store failed, lexical served
    SQL_FALLBACK = "sql_fallback"  # lexical index also unavailable -> SQL LIKE
    EMPTY = "empty"  # nothing configured / no data for this user


class IngestStatus(str, Enum):
    """Lifecycle of one document in ``rag_docs``."""

    PENDING = "pending"
    READY = "ready"  # parsed + chunked + embedded + indexed
    INDEX_PENDING = "index_pending"  # SQL written, vector upsert failed; rebuild-index fixes
    NEEDS_HEAVY_PARSER = "needs_heavy_parser"  # scanned/complex layout -> v1.5 external parser
    FAILED = "failed"  # parse/embed failed after retries; reason in rag_docs.error


@dataclass
class DocMeta:
    """Document metadata (rag_docs row, minus housekeeping columns)."""

    doc_key: str  # caller-chosen stable id, unique per user
    user_id: int
    title: str = ""
    source_path: str = ""
    visibility: str = "private"  # reserved for doc-level ACL; v1 enforces user-level only


@dataclass
class Chunk:
    """One chunk as stored in the SQL source of truth (rag_chunks row)."""

    chunk_id: int  # SQL PK; doubles as the vector-store PK
    doc_key: str
    user_id: int
    seq: int  # position within the doc (0-based)
    text: str  # cleaned chunk text - what gets returned to the model
    embed_text: str = ""  # text actually embedded (chunk + contextual prefix); "" -> use text
    title_path: str = ""  # "文档标题 > 章节 > 小节" for citation display
    page: int | None = None  # PDF page number when known
    extra: dict[str, Any] = field(default_factory=dict)

    def text_to_embed(self) -> str:
        return self.embed_text or self.text

    def text_to_index(self) -> str:
        """What the LEXICAL channel should index: title path + body.

        Symmetric with ``text_to_embed`` and for the same reason. Contextual
        retrieval puts the heading path into ``embed_text``, so the vector
        channel can find a chunk by its section name; if BM25 indexed the bare
        body, a query for a term that appears ONLY in a heading ("权限",
        "contextual", a table caption, a clause number) would return zero
        lexical hits while the vector channel found it - and hybrid retrieval
        would be strictly worse than vector-only for exactly the exact-term
        lookups BM25 exists to win.

        So: lexical indexing text = title_path + "\\n" + body. The body is NOT
        duplicated into a boosted field here on purpose - field weighting is a
        TUNING decision and the house rule is 先建评测再调检索, so it belongs in
        an A/B sweep (M4), not in a hardcoded constant.
        """
        if self.title_path and self.title_path not in self.text:
            return f"{self.title_path}\n{self.text}"
        return self.text


@dataclass
class RetrievedChunk:
    """One hit returned to the caller - ALWAYS carries citation fields."""

    chunk_id: int
    doc_key: str
    text: str
    score: float  # fused/reranked score; comparable only within one result
    title: str = ""
    title_path: str = ""
    source: str = ""  # source_path of the doc, for citation
    page: int | None = None

    def text_to_score(self) -> str:
        """What a CROSS-ENCODER must be shown: title path + body.

        Third instance of the channel-symmetry house rule, and the one that
        was missed. The vector channel embeds ``Chunk.text_to_embed()``
        (contextual prefix = title_path), the lexical channel indexes
        ``Chunk.text_to_index()`` (title_path + body). Both retrieval channels
        therefore CAN find a chunk by its section name.

        If the reranker is handed the bare ``text`` instead, it is asked to
        re-score from scratch with strictly LESS information than the two
        channels that produced its candidate list had. Consequence, measured
        on the M4 sweep (5 LOST cases, all tagged ``edge_case``, 3 of them
        falling from rr=1.000 to 0.20-0.50): queries whose answer LIVES IN THE
        HEADING ("LangSmith 被放在哪个章节路径下", "3.2 小节提到哪种异常",
        "归入的具体点评类别") become unanswerable by the cross-encoder, so it
        falls back to body-level lexical similarity and promotes the
        same-document NEIGHBOURING chunks - whose bodies look alike - over the
        gold. That is exactly the observed top-3 shape.

        So: rerank scoring text = title_path + "\\n" + body, symmetric with
        ``Chunk.text_to_index()``. The guard mirrors it too: do not duplicate
        the path when the body already opens with it.
        """
        if self.title_path and self.title_path not in self.text:
            return f"{self.title_path}\n{self.text}"
        return self.text


@dataclass
class RetrievalResult:
    """Full retrieval response: hits + how they were produced."""

    chunks: list[RetrievedChunk] = field(default_factory=list)
    mode: RetrievalMode = RetrievalMode.EMPTY
    degraded: bool = False  # True whenever mode != HYBRID (or rerank was skipped due to error)
    duration_ms: int = 0
    outcome: str = ""  # what was reported to on_retrieval (may differ from mode on partial failures)


@dataclass
class EmbedResult:
    """Embedder output. Vectors MUST be in input order."""

    vectors: list[list[float]] = field(default_factory=list)
    usage_tokens: int = 0


@dataclass
class IngestOutcome:
    """What one ingest_file call did - surfaced to callers + CLI, never silent.

    ``status`` is the terminal IngestStatus written to rag_docs:
      READY             - parsed + chunked + embedded + vector-projected
      INDEX_PENDING     - SQL written; embedding/vector step failed (text is
                          still BM25-searchable; rebuild-index fixes the vector)
      NEEDS_HEAVY_PARSER- scanned/complex layout, v1 declines to index garbage
      FAILED            - parse itself failed (reason carries why)

    ``chunks_indexed`` counts vectors actually upserted; ``chunks_stored``
    counts SQL rows written. They differ only on the INDEX_PENDING path.
    """

    doc_key: str
    status: str = IngestStatus.READY.value
    chunks_stored: int = 0
    chunks_indexed: int = 0
    usage_tokens: int = 0
    title: str = ""
    reason: str = ""  # populated on FAILED / NEEDS_HEAVY / INDEX_PENDING
    degraded: bool = False  # True when status != READY (something fell short)


# ---------------------------------------------------------------------------
# Parsing layer (M1)
# ---------------------------------------------------------------------------

# Block kinds produced by parsers. The chunker uses these to find semantic
# boundaries (heading/paragraph) instead of slicing at arbitrary char counts.
BLOCK_HEADING = "heading"
BLOCK_PARAGRAPH = "paragraph"
BLOCK_TABLE = "table"
BLOCK_CODE = "code"
BLOCK_LIST = "list"

# Parser capability tags -> ingest status routing.
# v1 handles text-bearing formats in-process; scanned/complex layouts go to
# an EXTERNAL heavy parser service (v1.5: MinerU / PaddleOCR) and are marked
# needs_heavy_parser so they never pollute the index with garbage.
NEEDS_HEAVY = "needs_heavy_parser"


@dataclass
class ParsedBlock:
    """One structural unit extracted from a document."""

    kind: str  # BLOCK_* above
    text: str
    level: int = 0  # heading level 1-6; 0 for non-headings
    page: int | None = None  # source page (PDF) when known
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    """Parser output: structure + a quality verdict.

    ``quality`` and ``needs_heavy_parser`` are the gate that keeps garbage out
    of the index: a scanned PDF parsed by a text extractor yields near-empty
    pages, and ingesting that would poison retrieval silently. Ingest marks
    such docs NEEDS_HEAVY instead of READY.
    """

    blocks: list[ParsedBlock] = field(default_factory=list)
    title: str = ""
    language: str = ""  # best-effort: "zh" / "en" / "mixed"
    page_count: int | None = None
    char_count: int = 0
    quality: float = 1.0  # 0.0-1.0; low = likely scanned/garbled
    needs_heavy_parser: bool = False
    reason: str = ""  # why heavy parser is needed / why quality is low
    backend: str = ""  # which parser backend produced this (for audit)

    def plain_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())


@dataclass
class ChunkDraft:
    """Chunker output before persistence: no ids, no user/doc binding.

    The ingest pipeline turns these into ``Chunk`` rows (assigning chunk_id /
    doc_key / user_id). Keeping them separate means the chunker is pure and
    testable without any store.
    """

    seq: int
    text: str  # clean chunk text - what gets returned to the model
    embed_text: str = ""  # text actually embedded (chunk + contextual prefix)
    title_path: str = ""  # "文档标题 > 章节 > 小节" for citation display
    page: int | None = None
    kind: str = BLOCK_PARAGRAPH
    meta: dict[str, Any] = field(default_factory=dict)
