"""pi.rag - enterprise RAG kernel (parser/chunker/retriever/ingest/eval).

Portability contract: modules under pi.rag MUST NOT import pi.tools /
pi.server / pi.agent / fastapi. The only allowed coupling points are:
  - pi.rag.adapters  (pi integration: EmbeddingClient, Database, metering hooks)
  - pi.tools.rag     (the RagTool shell)

Everything else talks through the five Protocols in pi.rag.protocols and the
defaults in pi.rag.defaults, so the whole directory can be lifted into
another project unchanged.
"""

from pi.rag.config import RagConfig
from pi.rag.retriever import HybridRetriever, rrf_fuse
from pi.rag.types import (
    Chunk,
    DocMeta,
    EmbedResult,
    IngestStatus,
    IngestOutcome,
    RetrievedChunk,
    RetrievalMode,
    RetrievalResult,
)

__all__ = [
    "Chunk",
    "DocMeta",
    "EmbedResult",
    "HybridRetriever",
    "IngestOutcome",
    "IngestStatus",
    "RagConfig",
    "RetrievalMode",
    "RetrievalResult",
    "RetrievedChunk",
    "rrf_fuse",
]
