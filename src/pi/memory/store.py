"""Vector index for long-term memory: disabled (default), in-process, or Milvus.

Set PI_MILVUS_URI to enable Milvus. Without it memory is off (NoOpStore) so the
service and its test suite keep working with no external dependency - the same
degradation contract PI_REDIS_URL has in server/cache.py.

The store is a pure accelerator, never a source of truth: it holds only
(fact id, user_id, vector) triples, where the id is the owning MySQL row's
primary key. Fact text, provenance and lifecycle live in the MemoryRepo, so a
dropped or corrupted index is rebuilt from MySQL with zero API calls
(tools/rebuild_milvus.py) and a stale entry can never fabricate a fact - the
service joins search results back against the repo before using them.

Fail-open on availability, fail-closed on isolation: an unreachable Milvus
degrades to "no memories this turn" with a loud error, but the user_id filter
is structurally mandatory (see _user_filter) so a cross-tenant read cannot be
expressed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("pi.memory.store")


@dataclass
class Fact:
    """One remembered fact.

    `score` is search-time similarity, 0 when listed. `created_at` is when the fact
    was first stored; `last_seen_at` is the last time a later extraction re-confirmed
    it (equal to created_at until then). Eviction orders by last_seen_at, so a fact
    that keeps being re-stated survives and one that never recurs does not.
    """

    id: int
    user_id: int
    text: str
    kind: str
    source_session: str
    created_at: str
    last_seen_at: str = ""
    score: float = 0.0


def _user_filter(user_id: int) -> str:
    """Build the tenant filter. The int() is the isolation guarantee.

    Milvus filter expressions are strings, so a user-controlled value interpolated
    here would be an expression-injection hole (and a cross-tenant read). Coercing
    to int first turns anything else into a ValueError or TypeError instead. Every
    query in this module goes through this function - never build the filter inline.
    """
    return f"user_id == {int(user_id)}"


class VectorStore(Protocol):
    """Mirror of the MemoryRepo's facts, addressed by the repo's row ids."""

    #: False when memory is switched off, so callers can skip work entirely.
    enabled: bool

    async def setup(self, dim: int | None = None) -> None:
        """Create backing storage if missing. Idempotent; safe across replicas.

        `dim` overrides the configured vector width. It exists because
        PI_EMBEDDING_DIM=0 means "use the model's native width", which is only known
        after probing the embedder - and the store is constructed before that.
        """

    async def upsert(self, user_id: int, rows: Sequence[tuple[int, Sequence[float]]]) -> bool:
        """Index rows of (repo row id, vector); True once the index accepted them.

        The id is the primary key, so re-sending a row replaces its vector instead
        of duplicating it - that idempotence is what makes the write path
        retry-safe (a failed sync just re-upserts) and what lets touch be a
        one-statement repo UPDATE plus an upsert with the same id, rather than the
        delete+reinsert id rotation it used to be.
        """

    async def search(self, user_id: int, vector: Sequence[float], limit: int) -> list[tuple[int, float]]:
        """Nearest (repo row id, cosine similarity) pairs, best first.

        No payload comes back on purpose: the caller joins ids against the repo,
        which is where fact text lives and where is_active decides whether a
        hit is still a fact at all. That join is the zombie-vector filter - the
        index may lag behind a decay or delete, MySQL may not.
        """

    async def delete(self, user_id: int, fact_ids: Sequence[int]) -> int:
        """Drop vectors by repo row id; returns how many went away (best-effort).

        Both keys required per id: another user's id reads as absent. The count is
        advisory only - deletion is not read back, so 0 does not mean failure.
        """

    async def delete_user(self, user_id: int) -> int:
        """Drop every vector of one user (deregistration); returns how many went away."""

    async def ping(self) -> bool:
        """Health probe. True when disabled - 'off' is not 'broken'."""

    async def close(self) -> None:
        pass


class NoOpStore:
    """Memory disabled. Every call is a cheap no-op."""

    enabled = False

    async def setup(self, dim: int | None = None) -> None:
        return None

    async def upsert(self, user_id: int, rows: Sequence[tuple[int, Sequence[float]]]) -> bool:
        # True, not False: a caller that ignored `enabled` must not conclude the
        # rows failed to sync and retry them forever.
        return True

    async def search(self, user_id: int, vector: Sequence[float], limit: int) -> list[tuple[int, float]]:
        return []

    async def delete(self, user_id: int, fact_ids: Sequence[int]) -> int:
        return 0

    async def delete_user(self, user_id: int) -> int:
        return 0

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class InMemoryStore:
    """Process-local brute-force index: tests, and single-instance dev without Milvus.

    Vectors stay whatever length the embedder produced - cosine is scale-free, so a
    test can use 8-dim hashes while production uses 1024.
    """

    enabled = True

    def __init__(self) -> None:
        # fact_id -> (owner user_id, vector). The fact itself lives in the repo.
        self._index: dict[int, tuple[int, list[float]]] = {}

    async def setup(self, dim: int | None = None) -> None:
        return None

    async def upsert(self, user_id: int, rows: Sequence[tuple[int, Sequence[float]]]) -> bool:
        for fid, vec in rows:
            self._index[int(fid)] = (int(user_id), list(vec))
        return True

    async def search(self, user_id: int, vector: Sequence[float], limit: int) -> list[tuple[int, float]]:
        uid = int(user_id)
        scored = [
            (_cosine(vector, vec), fid)
            for fid, (owner, vec) in self._index.items()
            if owner == uid
        ]
        # score descending; ties keep insertion order, which is stable enough for
        # an in-process index and matches what tests assert against
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [(fid, score) for score, fid in scored[:limit]]

    async def delete(self, user_id: int, fact_ids: Sequence[int]) -> int:
        uid = int(user_id)
        doomed = []
        for i in fact_ids:
            entry = self._index.get(int(i))
            if entry is not None and entry[0] == uid:
                doomed.append(int(i))
        for fid in dict.fromkeys(doomed):  # tolerate duplicate ids in the input
            del self._index[fid]
        return len(doomed)

    async def delete_user(self, user_id: int) -> int:
        uid = int(user_id)
        doomed = [fid for fid, (owner, _) in self._index.items() if owner == uid]
        for fid in doomed:
            del self._index[fid]
        return len(doomed)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class MilvusStore:
    """Milvus backend: sync MilvusClient driven from a dedicated thread pool.

    Why sync-in-a-thread rather than AsyncMilvusClient: every route in server/app.py
    is `async def` running directly on the event loop with no thread-pool fallback,
    so one un-awaited gRPC call freezes the whole service - health checks and every
    in-flight SSE stream included. That is the §17.15 PBKDF2 failure mode (453ms
    site-wide stall) and it would happen on every request here. gRPC releases the GIL
    in C, so a pool gets real parallelism. The pool is dedicated rather than the
    default `asyncio.to_thread` executor so memory traffic cannot starve other
    to_thread users (password hashing).

    Collection layout for a million-user scale: one collection, `user_id` as the
    partition key. Milvus hashes it into `num_partitions` buckets and prunes to one
    bucket per query, so a search touches 1/1024 of the data instead of filtering the
    whole collection. A partition per user is impossible (far over the partition
    limit); no partition key at all means every search scans everything.
    """

    enabled = True

    def __init__(
        self,
        uri: str,
        token: str,
        namespace: str = "pi",
        dim: int = 512,
        num_partitions: int = 1024,
        max_workers: int = 8,
        timeout: float = 30.0,
    ):
        # Lazy import: pymilvus is an optional extra (grpcio + pandas + protobuf).
        from pymilvus import DataType, MilvusClient

        self._DataType = DataType
        self._client = MilvusClient(uri=uri, token=token, timeout=timeout)
        self._collection = f"{namespace}_memories"
        self._dim = int(dim)
        self._num_partitions = int(num_partitions)
        self._uri = uri
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="milvus")

    @property
    def collection(self) -> str:
        return self._collection

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(fn, *args, **kwargs))

    async def setup(self, dim: int | None = None) -> None:
        if dim:
            self._dim = int(dim)
        await self._call(self._setup_sync)

    def _setup_sync(self) -> None:
        DataType = self._DataType
        if self._client.has_collection(self._collection):
            self._check_existing_collection()
            return

        # auto_id=False: the primary key is the MySQL row id supplied by the
        # caller, which is what makes upsert idempotent and touch a one-shot
        # replace. With auto_id=True Milvus mints its own ids, upsert is
        # unusable, and every touch had to be a delete+insert that rotated the id.
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("user_id", DataType.INT64, is_partition_key=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)

        index_params = self._client.prepare_index_params()
        # AUTOINDEX rather than HNSW: at ~1e8 vectors HNSW's resident memory is
        # unaffordable, and the managed service picks a disk-friendly index itself.
        # COSINE rather than the demo's L2: text embeddings are compared by angle,
        # and only a cosine score gives PI_MEMORY_MIN_SIMILARITY a stable meaning.
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )

        self._client.create_collection(
            self._collection,
            schema=schema,
            index_params=index_params,
            num_partitions=self._num_partitions,
        )
        log.info(
            "milvus collection %s created (dim=%d, partitions=%d)",
            self._collection,
            self._dim,
            self._num_partitions,
        )

    def _check_existing_collection(self) -> None:
        """Refuse to run against a collection our config cannot serve.

        Two refusals, both because MySQL owns the facts now and this index is
        disposable: a dim mismatch would make every insert fail one at a time, and
        an auto_id collection predates the deterministic-PK schema - upserts
        against it cannot carry the MySQL row id. Either way the operator drops
        the collection and rebuilds it from MySQL (tools/rebuild_milvus.py);
        silently continuing or silently rebuilding are both worse than a loud
        error that degrades memory to off (get_store catches this).
        """
        desc = self._client.describe_collection(self._collection)
        for field in desc.get("fields", []):
            name = field.get("name")
            if name == "id" and field.get("auto_id"):
                raise RuntimeError(
                    f"collection {self._collection} was created with auto_id ids, "
                    f"which predates the MySQL source-of-truth design. Drop it and "
                    f"rebuild from MySQL with tools/rebuild_milvus.py."
                )
            if name != "vector":
                continue
            params = field.get("params", {}) or {}
            actual = params.get("dim", field.get("dim"))
            if actual is not None and int(actual) != self._dim:
                raise RuntimeError(
                    f"collection {self._collection} has dim={actual} but "
                    f"PI_EMBEDDING_DIM={self._dim}; refusing to rebuild it. Either set "
                    f"PI_EMBEDDING_DIM={actual} or drop the collection deliberately."
                )

    async def upsert(self, user_id: int, rows: Sequence[tuple[int, Sequence[float]]]) -> bool:
        if not rows:
            return True
        data = [
            # No flush(): Milvus flushes on its own schedule. Flushing per write
            # serialises everything behind a segment seal and destroys throughput
            # - the vendor demo does it to make a script's output deterministic,
            # which is not what a service wants.
            {"id": int(fid), "user_id": int(user_id), "vector": list(vec)}
            for fid, vec in rows
        ]
        await self._call(self._client.upsert, self._collection, data)
        return True

    async def search(self, user_id: int, vector: Sequence[float], limit: int) -> list[tuple[int, float]]:
        rows = await self._call(
            self._client.search,
            self._collection,
            data=[list(vector)],
            filter=_user_filter(user_id),
            limit=int(limit),
            anns_field="vector",
            # COSINE: higher is closer, so `distance` is already a similarity and
            # PI_MEMORY_MIN_SIMILARITY means the same thing across models.
            search_params={"metric_type": "COSINE"},
            # Bounded, always: retrieval is the only caller, it reads through the
            # repo join anyway (which is where freshness comes from), and Session
            # would cost ~370ms whenever this client has unsynced writes. The old
            # read-your-writes need is gone with the index - dedup reads MySQL.
            consistency_level="Bounded",
        )
        group = (rows or [[]])[0] or []
        return [(int(h.get("id", 0)), float(h.get("distance", 0.0))) for h in group]

    async def delete(self, user_id: int, fact_ids: Sequence[int]) -> int:
        ids = [int(i) for i in fact_ids]
        if not ids:
            return 0
        # Both predicates: deleting by ids alone would let a crafted id remove
        # another tenant's vector. Every id is int()-coerced before it reaches
        # the expression, same guarantee _user_filter makes for user_id.
        filt = f"{_user_filter(user_id)} and id in [{', '.join(str(i) for i in ids)}]"
        res = await self._call(self._client.delete, self._collection, filter=filt)
        return _delete_count(res)

    async def delete_user(self, user_id: int) -> int:
        res = await self._call(
            self._client.delete, self._collection, filter=_user_filter(user_id)
        )
        return _delete_count(res)

    async def drop(self) -> bool:
        """Drop the whole collection. Rebuild-tool only: the server never drops.

        Returns False when there was nothing to drop, so the tool can tell a
        no-op from a rebuild in its report.
        """
        if not await self._call(self._client.has_collection, self._collection):
            return False
        await self._call(self._client.drop_collection, self._collection)
        return True

    async def ping(self) -> bool:
        try:
            await self._call(self._client.has_collection, self._collection)
            return True
        except Exception:  # noqa: BLE001 - a probe reports, it does not raise
            return False

    async def close(self) -> None:
        try:
            await self._call(self._client.close)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("milvus close failed", exc_info=True)
        self._pool.shutdown(wait=False, cancel_futures=True)


def _delete_count(res: Any) -> int:
    if isinstance(res, dict):
        for key in ("delete_count", "deleted_count", "count"):
            if key in res:
                try:
                    return int(res[key])
                except (TypeError, ValueError):
                    return 0
    return 0


def get_store(
    uri: str,
    token: str = "",
    namespace: str = "pi",
    dim: int = 512,
    num_partitions: int = 1024,
) -> VectorStore:
    """Pick Milvus when configured; otherwise disabled.

    A misconfigured or unreachable Milvus degrades to NoOp with a loud error rather
    than taking the service down - memory is an enhancement, not an isolation
    boundary, so fail-open on availability is correct here (contrast PI_SANDBOX,
    whose wrong value removes isolation and therefore fails startup).
    """
    if not uri:
        return NoOpStore()
    try:
        store = MilvusStore(uri, token, namespace=namespace, dim=dim, num_partitions=num_partitions)
    except Exception:  # noqa: BLE001 - includes ImportError when the extra is absent
        log.exception("milvus unavailable, memory disabled")
        return NoOpStore()
    log.info("milvus memory store configured: %s (ns=%s, dim=%d)", uri, namespace, dim)
    return store
