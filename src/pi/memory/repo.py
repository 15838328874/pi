"""Relational source of truth for long-term memory.

MySQL owns every fact; the vector store (Milvus) is an accelerator over it. The
service writes here first and mirrors to the index best-effort, so a lost or
rebuilt Milvus never loses a user's memory. This module defines the persistence
contract MemoryService codes against, plus an in-process implementation for
tests and keyless dev. The SQL implementation lives in pi/server/db.py because
it shares the app's declarative Base and alembic metadata - the dependency
stays one-way (server imports memory, never the reverse).

Isolation: every method takes user_id and every SQL predicate includes it. A
fact that belongs to another user reads as absent, the same contract
VectorStore.delete has.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from pi.memory.store import Fact


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MemoryRowIn:
    """One new fact to insert. `id` is assigned by the repo."""

    text: str
    kind: str
    source_session: str
    created_at: str = ""
    embedding: Sequence[float] = field(default_factory=list)


def pack_embedding(vec: Sequence[float]) -> bytes:
    """float32 little-endian. 1024-dim = 4KB per row - BLOB territory, not TEXT."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_embedding(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob[: n * 4]))


class MemoryRepo(Protocol):
    """Every fact's home. The vector store only ever mirrors what is here."""

    async def insert_many(self, user_id: int, rows: Sequence[MemoryRowIn], *, synced: bool = False) -> list[int]:
        """Insert rows; returns their ids in input order.

        `synced=True` marks the rows as already reflected in the vector index -
        used when the index is disabled so pending_sync() has nothing to chase.
        """

    async def touch(self, user_id: int, fact_id: int, embedding: Sequence[float]) -> bool:
        """Re-confirm a fact: bump last_seen_at, replace the vector, mark unsynced.

        False when the row is gone or inactive - the no-resurrection rule, now
        a single UPDATE's rowcount instead of a delete-race.
        """

    async def get_active(self, user_id: int, limit: int = 500) -> list[tuple[Fact, list[float]]]:
        """Active facts with their vectors, oldest first."""

    async def get_by_ids(self, user_id: int, ids: Sequence[int]) -> list[Fact]:
        """Active facts for these ids. The retrieval join.

        Index hits carry only ids, so this is where a hit becomes a fact again -
        and where a zombie vector (the row decayed or was deleted after the index
        saw it) reads as absent instead of being injected as a memory.
        """

    async def update_text(
        self,
        user_id: int,
        fact_id: int,
        text: str,
        kind: str,
        embedding: Sequence[float],
        source_session: str | None = None,
    ) -> bool:
        """Overwrite one fact's text/kind/vector in place; False when it is gone.

        Arbitration's merged fact lands here rather than as delete+insert, so the
        row keeps its id: a crash mid-merge leaves the duplicates in place for the
        next sweep to re-merge, never a lost group. Marks the row unsynced like
        every other write. `source_session` overrides the origin when given
        (arbitration marks its merged rows "arbiter"), None keeps it.
        """

    async def count_active(self, user_id: int) -> int:
        """Active fact count, for cap enforcement."""

    async def lru_ids(self, user_id: int, count: int) -> list[int]:
        """Ids of the longest-unconfirmed active facts, eviction order."""

    async def delete(self, user_id: int, fact_id: int) -> bool:
        """Hard-delete one fact. Another user's id reads as absent."""

    async def delete_user(self, user_id: int) -> int:
        """Hard-delete every fact of one user (deregistration / erasure)."""

    async def deactivate_older_than(self, cutoff: str) -> list[tuple[int, int]]:
        """Decay: facts unconfirmed since before `cutoff` become inactive.

        Returns (user_id, fact_id) pairs so the caller can drop them from the
        vector index with the tenant filter intact.
        """

    async def pending_sync(self, limit: int) -> list[tuple[Fact, list[float]]]:
        """Active facts not yet reflected in the vector index, oldest first."""

    async def mark_synced(self, ids: Sequence[int]) -> int:
        """Flag rows as reflected in the index."""


class DictMemoryRepo:
    """In-process MemoryRepo: tests, and dev boxes that want persistence-free memory."""

    def __init__(self) -> None:
        self._rows: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    def _fact(self, row: dict[str, Any]) -> tuple[Fact, list[float]]:
        return (
            Fact(
                id=row["id"],
                user_id=row["user_id"],
                text=row["text"],
                kind=row["kind"],
                source_session=row["source_session"],
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
            ),
            row["embedding"],
        )

    async def insert_many(
        self, user_id: int, rows: Sequence[MemoryRowIn], *, synced: bool = False
    ) -> list[int]:
        now = now_iso()
        ids: list[int] = []
        for r in rows:
            fid = self._next_id
            self._next_id += 1
            self._rows[fid] = {
                "id": fid,
                "user_id": int(user_id),
                "text": r.text,
                "kind": r.kind,
                "source_session": r.source_session,
                "created_at": r.created_at or now,
                "last_seen_at": now,
                "is_active": True,
                "synced": synced,
                "embedding": list(r.embedding),
            }
            ids.append(fid)
        return ids

    async def touch(self, user_id: int, fact_id: int, embedding: Sequence[float]) -> bool:
        row = self._rows.get(int(fact_id))
        if row is None or row["user_id"] != int(user_id) or not row["is_active"]:
            return False
        row["last_seen_at"] = now_iso()
        row["embedding"] = list(embedding)
        row["synced"] = False
        return True

    async def get_active(self, user_id: int, limit: int = 500) -> list[tuple[Fact, list[float]]]:
        rows = [
            self._fact(r)
            for r in self._rows.values()
            if r["user_id"] == int(user_id) and r["is_active"]
        ]
        rows.sort(key=lambda pair: (pair[0].created_at, pair[0].id))
        return rows[:limit]

    async def get_by_ids(self, user_id: int, ids: Sequence[int]) -> list[Fact]:
        wanted = {int(i) for i in ids}
        return [
            self._fact(r)[0]
            for r in self._rows.values()
            if r["id"] in wanted and r["user_id"] == int(user_id) and r["is_active"]
        ]

    async def update_text(
        self,
        user_id: int,
        fact_id: int,
        text: str,
        kind: str,
        embedding: Sequence[float],
        source_session: str | None = None,
    ) -> bool:
        row = self._rows.get(int(fact_id))
        if row is None or row["user_id"] != int(user_id) or not row["is_active"]:
            return False
        row["text"] = text
        row["kind"] = kind
        if source_session is not None:
            row["source_session"] = source_session
        row["last_seen_at"] = now_iso()
        row["embedding"] = list(embedding)
        row["synced"] = False
        return True

    async def count_active(self, user_id: int) -> int:
        return sum(
            1
            for r in self._rows.values()
            if r["user_id"] == int(user_id) and r["is_active"]
        )

    async def lru_ids(self, user_id: int, count: int) -> list[int]:
        rows = [
            r
            for r in self._rows.values()
            if r["user_id"] == int(user_id) and r["is_active"]
        ]
        rows.sort(key=lambda r: (r["last_seen_at"], r["created_at"], r["id"]))
        return [r["id"] for r in rows[:count]]

    async def delete(self, user_id: int, fact_id: int) -> bool:
        row = self._rows.get(int(fact_id))
        if row is None or row["user_id"] != int(user_id):
            return False
        del self._rows[row["id"]]
        return True

    async def delete_user(self, user_id: int) -> int:
        doomed = [fid for fid, r in self._rows.items() if r["user_id"] == int(user_id)]
        for fid in doomed:
            del self._rows[fid]
        return len(doomed)

    async def deactivate_older_than(self, cutoff: str) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for r in self._rows.values():
            if r["is_active"] and r["last_seen_at"] < cutoff:
                r["is_active"] = False
                out.append((r["user_id"], r["id"]))
        return out

    async def pending_sync(self, limit: int) -> list[tuple[Fact, list[float]]]:
        rows = [r for r in self._rows.values() if r["is_active"] and not r["synced"]]
        rows.sort(key=lambda r: r["id"])
        return [self._fact(r) for r in rows[:limit]]

    async def mark_synced(self, ids: Sequence[int]) -> int:
        n = 0
        for fid in ids:
            row = self._rows.get(int(fid))
            if row is not None:
                row["synced"] = True
                n += 1
        return n
