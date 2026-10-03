"""Vector backend for semantic memory: Milvus via pymilvus (optional).

Set PI_MILVUS_URI (e.g. http://127.0.0.1:19531) to enable vector retrieval;
without it MemoryRepo stays lexical-only. pymilvus is imported lazily inside
MilvusStore so the package is only needed when configured - the same pattern
as redis.asyncio in RedisBackend.

Degradation contract: every MilvusStore method may raise; MemoryRepo catches
and falls back to lexical search, so vector failures never fail a run. The
Postgres memories table is the source of truth - this collection is a
rebuildable index keyed by the same memory_id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger("pi.server.vectorstore")

_COLLECTION = "pi_memories"
_TEXT_MAX = 4096  # Milvus VARCHAR ceiling; Postgres keeps the full text
# gRPC honors http(s)_proxy env vars; a dead proxy on the host must not break
# local Milvus connections. This mirrors httpx trust_env=False in EmbeddingClient.
_PROXY_KILL = [("grpc.enable_http_proxy", 0)]


class VectorStore(Protocol):
    async def add(self, memory_id: int, user_id: int, text: str, vector: list[float]) -> None:
        """Upsert one vector, keyed by the Postgres memory row id (idempotent)."""

    async def search(self, user_id: int, vector: list[float], k: int) -> list[tuple[int, float]]:
        """Top-k (memory_id, cosine_similarity) for one user, best match first.

        The score is exposed (not just the ids) so callers can do semantic
        dedup with a similarity threshold - Milvus already computes it, and
        dropping it here forced the memory layer to fall back to lexical dedup.
        """

    async def ping(self) -> bool:
        """Health probe."""

    async def close(self) -> None:
        pass


class MilvusStore:
    """Milvus backend. All pymilvus calls are synchronous/blocking, so they run
    via asyncio.to_thread (house precedent: PBKDF2 in app.py)."""

    def __init__(self, uri: str, collection: str = _COLLECTION, timeout: float = 5.0) -> None:
        self.uri = uri
        self.collection = collection
        self.timeout = timeout
        # Lazy: MilvusClient.__init__ performs the Connect RPC eagerly, so the
        # client must be built at first use inside a try/except, never at app
        # boot - otherwise a Milvus outage would take down create_app.
        self._client = None
        self._ensure_lock = asyncio.Lock()

    def _get_client(self):
        """Lazy import + lazy construct. May raise (Milvus down) - callers catch."""
        if self._client is None:
            import pymilvus  # noqa: PLC0415 - lazy, like redis.asyncio

            self._client = pymilvus.MilvusClient(
                uri=self.uri,
                timeout=self.timeout,
                grpc_options=_PROXY_KILL,
            )
        return self._client

    async def _ensure(self, dim: int) -> None:
        """Create the collection on first use, serialized in-process and
        tolerant of a cross-process race (two workers creating at once)."""
        async with self._ensure_lock:
            c = await asyncio.to_thread(self._get_client)
            try:
                if not await asyncio.to_thread(c.has_collection, self.collection):
                    await asyncio.to_thread(self._create_collection, c, dim)
            except Exception:  # noqa: BLE001 - re-check before giving up
                if not await asyncio.to_thread(c.has_collection, self.collection):
                    raise

    def _create_collection(self, client, dim: int) -> None:
        import pymilvus

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("memory_id", pymilvus.DataType.INT64, is_primary=True)
        schema.add_field("user_id", pymilvus.DataType.INT64)
        schema.add_field("text", pymilvus.DataType.VARCHAR, max_length=_TEXT_MAX)
        schema.add_field("vector", pymilvus.DataType.FLOAT_VECTOR, dim=dim)
        index = client.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        index.add_index(field_name="user_id", index_type="INVERTED")
        client.create_collection(self.collection, schema=schema, index_params=index)
        log.info("milvus collection %r created (dim=%d, COSINE)", self.collection, dim)

    async def add(self, memory_id: int, user_id: int, text: str, vector: list[float]) -> None:
        await self._ensure(len(vector))
        row = {
            "memory_id": int(memory_id),
            "user_id": int(user_id),
            "text": text[:_TEXT_MAX],
            "vector": list(vector),
        }
        await asyncio.to_thread(self._get_client().upsert, self.collection, data=[row])

    async def search(self, user_id: int, vector: list[float], k: int) -> list[tuple[int, float]]:
        await self._ensure(len(vector))
        res = await asyncio.to_thread(
            self._get_client().search,
            self.collection,
            data=[list(vector)],
            filter=f"user_id == {int(user_id)}",  # int filter: bare literal, no quotes
            limit=max(1, int(k)),
            output_fields=["memory_id"],
            search_params={"metric_type": "COSINE"},
        )
        hits = res[0] if res else []
        # COSINE: distance is similarity (higher = closer) and hits arrive
        # best-first - do not re-sort.
        # pymilvus 3.x: 主键挂在 Hit 的属性 `id` 上，`Hit.entity` 只包含请求的
        # output_fields。旧写法 h["id"] 在**有命中时**必抛 KeyError（空结果不会
        # 进入推导式，所以这个 bug 能长期潜伏），向量召回反而在最该生效时失败。
        # 返回 (memory_id, cosine_similarity)：分数留给上层做语义去重。
        return [(int(h.id), float(h.distance)) for h in hits]  # pk == Postgres memory id

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
