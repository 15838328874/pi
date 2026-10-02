"""Default Protocol implementations shipped with the kernel.

Standalone mode = these defaults + RagConfig.from_env(); pi mode swaps in
adapters for whatever pi already has (embedding client, SQLAlchemy store,
metering hooks).
"""

from pi.rag.defaults.bm25 import MemoryBM25Index, tokenize
from pi.rag.defaults.fake_embedder import FakeEmbedder
from pi.rag.defaults.http_embedder import EmbeddingError, HttpEmbedder
from pi.rag.defaults.http_reranker import HttpReranker, NoopHooks, RerankError
from pi.rag.defaults.memory_vector import InMemoryVectorStore
from pi.rag.defaults.sqlite_store import SqliteChunkStore

__all__ = [
    "EmbeddingError",
    "FakeEmbedder",
    "HttpEmbedder",
    "HttpReranker",
    "InMemoryVectorStore",
    "MemoryBM25Index",
    "NoopHooks",
    "RerankError",
    "SqliteChunkStore",
    "tokenize",
]
