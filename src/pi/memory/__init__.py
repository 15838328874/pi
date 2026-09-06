"""Long-term memory: LLM-extracted facts, embedded and retrieved from a vector store.

Layer position: below pi.agent (the agent core receives a bare callable, never this
package) and beside pi.tools. server/runner.py and server/app.py are the only
importers, keeping the documented one-way dependency rule intact.

Off by default. Memory needs both PI_MILVUS_URI and PI_EMBEDDING_MODEL; with either
missing the service runs with memory disabled rather than failing to start, so the
test suite and a keyless dev box keep working unchanged.
"""

from __future__ import annotations

from pi.memory.embed import Embedder, HashEmbedder, OpenAIEmbedder, get_embedder
from pi.memory.extract import FactCandidate, extract_facts, parse_facts, transcript_chars
from pi.memory.repo import DictMemoryRepo, MemoryRepo, MemoryRowIn, pack_embedding, unpack_embedding
from pi.memory.rerank import DashScopeReranker, Reranker, get_reranker
from pi.memory.service import MemoryService, UsageSink
from pi.memory.store import (
    Fact,
    InMemoryStore,
    MilvusStore,
    NoOpStore,
    VectorStore,
    get_store,
)

__all__ = [
    "DashScopeReranker",
    "DictMemoryRepo",
    "Embedder",
    "Fact",
    "FactCandidate",
    "HashEmbedder",
    "InMemoryStore",
    "MemoryRepo",
    "MemoryRowIn",
    "MemoryService",
    "MilvusStore",
    "NoOpStore",
    "OpenAIEmbedder",
    "Reranker",
    "UsageSink",
    "VectorStore",
    "extract_facts",
    "get_embedder",
    "get_reranker",
    "get_store",
    "pack_embedding",
    "parse_facts",
    "transcript_chars",
    "unpack_embedding",
]
