"""Milvus vector backend for the RAG kernel (real-stack projection).

This is the production ``RagVectorStore``: a rebuildable projection of the SQL
source of truth (rag_chunks), keyed by the same chunk_id so vector hits hydrate
1:1 through ``ChunkStore.get_chunks_by_ids``. Mirrors the house pattern proven
in ``pi/server/vectorstore.py`` (semantic memory):

  - lazy import + lazy construct: MilvusClient.__init__ does the Connect RPC
    eagerly, so it is built at first use inside try/except, never at boot - a
    Milvus outage must not take down create_app.
  - every pymilvus call is blocking -> wrapped in asyncio.to_thread.
  - gRPC proxy killed (grpc.enable_http_proxy=0): a dead http(s)_proxy on the
    host must not break a local Milvus connection (same reason httpx uses
    trust_env=False in the embedders).
  - ACL: user_id filter is a BARE int literal (no quotes); doc_key is a string
    literal and MUST be escaped (Milvus filter injection guard).

Unlike pi_memories, this collection stores NO text - SQL owns the bytes and
this stays a thin (chunk_id, user_id, doc_key, vector) index. That keeps the
projection cheap to rebuild and impossible to drift from the truth on content.

Degradation contract: every method may raise; the RETRIEVER (M3) catches and
falls back to BM25 -> SQL LIKE, reporting each degradation via UsageHooks.
"""

from __future__ import annotations

import asyncio
import logging

from pi.rag.types import Chunk

log = logging.getLogger("pi.rag.milvus")

_DEFAULT_COLLECTION = "pi_rag_chunks"
# gRPC honors http(s)_proxy env; kill it so a dead host proxy can't break local
# Milvus. Mirrors httpx trust_env=False in HttpEmbedder/HttpReranker.
_PROXY_KILL = [("grpc.enable_http_proxy", 0)]


def _escape_str(value: str) -> str:
    """Escape a string for a Milvus boolean-expression literal.

    doc_key is caller-influenced (defaults to a file path), so it must not be
    interpolated raw into a filter: a backslash or quote could break the
    expression or (worst case) widen the ACL scope. Milvus string literals use
    single quotes with backslash escapes.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class MilvusRagVectorStore:
    """RagVectorStore backed by a Milvus collection (default pi_rag_chunks).

    Constructed with just the URI; the client and collection are created lazily
    on first use. ``dim`` is discovered from the first upsert (the embedder's
    output width) so the collection schema matches the configured model without
    a separate config knob.
    """

    def __init__(
        self,
        uri: str,
        collection: str = _DEFAULT_COLLECTION,
        timeout: float = 10.0,
        consistency: str = "Strong",
    ) -> None:
        self.uri = uri
        self.collection = collection
        self.timeout = timeout
        # "Strong" (default) = read-after-write: a chunk is searchable the
        # instant it is written, at the cost of a query-coordinator sync on
        # EVERY search. Measured on 527 vectors: Strong 399ms vs Bounded/Session
        # 4ms per search, identical hits (tools/probe_local_stages.py) - so the
        # barrier, not the ANN scan, dominates. Deployments that value latency
        # over instant post-ingest visibility set PI_RAG_MILVUS_CONSISTENCY.
        self.consistency = consistency or "Strong"
        self._client = None
        self._ensure_lock = asyncio.Lock()
        self._dim: int | None = None

    # -- lazy client / collection ------------------------------------------

    def _get_client(self):
        """Lazy import + lazy construct. May raise (Milvus down) - callers catch."""
        if self._client is None:
            import pymilvus  # noqa: PLC0415 - lazy, only needed when configured

            self._client = pymilvus.MilvusClient(
                uri=self.uri,
                timeout=self.timeout,
                grpc_options=_PROXY_KILL,
            )
        return self._client

    async def _ensure(self, dim: int) -> None:
        """Create the collection on first use (serialized in-process, tolerant
        of a cross-process race between workers).

        Fast path first: this sits in front of EVERY search, so re-validating
        existence remotely would put a metadata round trip on the read path for
        the whole process lifetime. ``_dim`` doubles as the ready flag (it is
        only set after the collection is known to exist), and ``drop()`` clears
        it. Mirrors the ``_schema_ready`` short-circuit in MysqlChunkStore.
        """
        if self._dim == dim:
            return
        async with self._ensure_lock:
            if self._dim == dim:
                return
            c = await asyncio.to_thread(self._get_client)
            try:
                if not await asyncio.to_thread(c.has_collection, self.collection):
                    await asyncio.to_thread(self._create_collection, c, dim)
                self._dim = dim
            except Exception:  # noqa: BLE001 - re-check before giving up
                if not await asyncio.to_thread(c.has_collection, self.collection):
                    raise
                # Another process created it between our check and our create.
                self._dim = dim

    def _create_collection(self, client, dim: int) -> None:
        import pymilvus  # noqa: PLC0415

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        # PK == rag_chunks.id (the SQL row id) so vector hits hydrate 1:1.
        schema.add_field("chunk_id", pymilvus.DataType.INT64, is_primary=True)
        schema.add_field("user_id", pymilvus.DataType.INT64)
        # doc_key enables delete_by_doc (re-ingest purge) without a SQL round-trip.
        schema.add_field("doc_key", pymilvus.DataType.VARCHAR, max_length=1024)
        schema.add_field("vector", pymilvus.DataType.FLOAT_VECTOR, dim=dim)
        index = client.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        index.add_index(field_name="user_id", index_type="INVERTED")
        client.create_collection(self.collection, schema=schema, index_params=index)
        log.info("milvus collection %r created (dim=%d, COSINE)", self.collection, dim)

    # -- RagVectorStore protocol -------------------------------------------

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Idempotent upsert keyed by chunk_id. len(vectors) == len(chunks)."""
        if len(chunks) != len(vectors):
            raise ValueError(
                f"upsert mismatch: {len(chunks)} chunks vs {len(vectors)} vectors"
            )
        if not chunks:
            return
        dim = len(vectors[0])
        await self._ensure(dim)
        rows = [
            {
                "chunk_id": int(ch.chunk_id),
                "user_id": int(ch.user_id),
                "doc_key": str(ch.doc_key)[:1024],
                "vector": list(vec),
            }
            for ch, vec in zip(chunks, vectors)
        ]
        await asyncio.to_thread(self._get_client().upsert, self.collection, rows)

    async def delete_by_doc(self, user_id: int, doc_key: str) -> None:
        """Drop all vectors of one doc (re-ingest purge + INDEX_PENDING cleanup).

        Scoped by user_id AND doc_key so a collision of doc_keys across users
        can never delete another tenant's vectors.
        """
        # _ensure needs a dim; if the collection was never created there is
        # nothing to delete, so probe lazily via the client (may raise -> caller
        # treats as best-effort). Use dim=0 sentinel: _ensure only creates when
        # has_collection is False, and a delete on a fresh empty collection is a
        # harmless no-op. We avoid creating an empty collection here by checking
        # existence first.
        c = await asyncio.to_thread(self._get_client)
        if not await asyncio.to_thread(c.has_collection, self.collection):
            return
        filt = f"user_id == {int(user_id)} and doc_key == '{_escape_str(doc_key)}'"
        await asyncio.to_thread(c.delete, self.collection, filter=filt)

    async def search(
        self,
        user_id: int,
        vector: list[float],
        k: int,
        doc_keys: list[str] | None = None,
    ) -> list[tuple[int, float]]:
        """Top-k (chunk_id, score) for ONE user, best first.

        ACL: user_id is a bare int literal in the filter. doc_keys (optional)
        narrows to specific docs, each escaped. COSINE: distance IS similarity
        (higher = closer) and hits arrive best-first - do not re-sort.
        """
        await self._ensure(len(vector))
        filt = f"user_id == {int(user_id)}"
        if doc_keys:
            quoted = ", ".join(f"'{_escape_str(dk)}'" for dk in doc_keys)
            filt += f" and doc_key in [{quoted}]"
        res = await asyncio.to_thread(
            self._get_client().search,
            self.collection,
            data=[list(vector)],
            filter=filt,
            limit=max(1, int(k)),
            output_fields=["chunk_id"],
            search_params={"metric_type": "COSINE"},
            # Configurable read-after-write. Strong (default) guarantees a
            # just-ingested chunk is searchable immediately; Bounded/Session
            # give up a bounded staleness window for ~100x lower search latency
            # (399ms -> 4ms measured, same hits). See PI_RAG_MILVUS_CONSISTENCY.
            consistency_level=self.consistency,
        )
        hits = res[0] if res else []
        # pymilvus 3.x exposes the PK under its FIELD NAME (chunk_id) at the top
        # level plus a 'distance' score; 2.x used a generic 'id'. Read both.
        out: list[tuple[int, float]] = []
        for h in hits:
            cid = h.get("chunk_id", h.get("id"))
            if cid is None:
                continue
            out.append((int(cid), float(h.get("distance", 0.0))))
        # COSINE: distance IS similarity (higher = closer), hits arrive
        # best-first - do not re-sort.
        return out

    async def drop(self) -> None:
        """Drop the WHOLE collection (ops/test primitive, not a user-facing path).

        Legal because of the house truth-source rule: this collection is a
        disposable projection of rag_chunks (SQL). Dropping it loses nothing
        that ``IngestPipeline.rebuild_index`` cannot re-derive. Used by the
        integration test for a clean slate + zero residue, and by ops when a
        schema change (e.g. embedding dim) requires a rebuild.

        Never raises: a missing collection or unreachable Milvus is already
        the desired end state / a best-effort cleanup.
        """
        try:
            c = await asyncio.to_thread(self._get_client)
            if await asyncio.to_thread(c.has_collection, self.collection):
                await asyncio.to_thread(c.drop_collection, self.collection)
                log.info("milvus collection %r dropped", self.collection)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            log.debug("milvus drop failed for %r", self.collection, exc_info=True)
        finally:
            self._dim = None

    async def ping(self) -> bool:
        try:
            return await asyncio.to_thread(self._get_client().get_server_version) is not None
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:  # noqa: BLE001
                log.debug("milvus close failed", exc_info=True)
            self._client = None
