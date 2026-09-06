"""Offline tests for tools/rebuild_milvus.py: guards and the MySQL walk.

The real Milvus round trip belongs to the integration tier; what these tests
pin is everything around it - the two refusals that keep a typo from wiping a
production index, and the walk logic itself (keyset paging, per-user grouping,
exact-id mark_synced) against a fake store on a real SQLite database.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from pi.memory.repo import MemoryRowIn
from pi.server.db import Database, UserMemory, UserMemoryRepo

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "rebuild_milvus.py"
_spec = importlib.util.spec_from_file_location("rebuild_milvus_tool", TOOL_PATH)
rebuild = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rebuild)


class FakeStore:
    """Records the rebuild's every move against the Milvus API."""

    def __init__(self, uri, token, namespace="pi", dim=512, num_partitions=1024):
        self.collection = f"{namespace}_memories"
        self.calls: list[str] = []
        self.upserts: list[tuple[int, list[tuple[int, list[float]]]]] = []

    async def drop(self) -> bool:
        self.calls.append("drop")
        return True

    async def setup(self, dim=None) -> None:
        self.calls.append(f"setup:{dim}")

    async def upsert(self, user_id, rows):
        self.upserts.append((int(user_id), list(rows)))
        return True

    async def close(self) -> None:
        self.calls.append("close")


@pytest.fixture()
def fake_milvus(monkeypatch):
    made: list[FakeStore] = []
    cls = FakeStore

    def factory(*a, **k):
        store = cls(*a, **k)
        made.append(store)
        return store

    monkeypatch.setattr(rebuild, "MilvusStore", factory)
    return made


def _env(monkeypatch, tmp_path: Path, ns: str) -> Path:
    monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'rb.db').as_posix()}")
    monkeypatch.setenv("PI_MILVUS_URI", "https://milvus.example")
    monkeypatch.setenv("PI_MILVUS_TOKEN", "t")
    monkeypatch.setenv("PI_MILVUS_NS", ns)
    monkeypatch.setenv("PI_EMBEDDING_DIM", "8")
    return tmp_path / "rb.db"


def _seed(db: Database) -> dict[str, list[int]]:
    """User 7: three facts (one decayed later). User 8: one fact. Ids by user."""
    repo = UserMemoryRepo(db)
    ids: dict[str, list[int]] = {}

    async def seed():
        ids["u7"] = await repo.insert_many(
            7,
            [
                MemoryRowIn(text=f"事实 {i}", kind="fact", source_session="s",
                            embedding=[float(i)] * 8)
                for i in range(3)
            ],
        )
        ids["u8"] = await repo.insert_many(
            8,
            [MemoryRowIn(text="另一用户", kind="fact", source_session="s",
                         embedding=[9.0] * 8)],
        )
        # Decay one row the way the service does: soft, is_active=0.
        async with AsyncSession(db.engine) as s:
            await s.execute(
                update(UserMemory)
                .where(UserMemory.id == ids["u7"][1])
                .values(is_active=False)
            )
            await s.commit()

    import asyncio

    asyncio.run(seed())
    return ids


class TestGuards:
    def test_the_production_namespace_is_refused_without_yes(self, monkeypatch, tmp_path, fake_milvus):
        _env(monkeypatch, tmp_path, ns="pi")
        monkeypatch.setattr(sys, "argv", ["rebuild_milvus.py"])

        with pytest.raises(SystemExit) as exc:
            rebuild.main()

        assert "refusing to touch namespace 'pi'" in str(exc.value)
        assert fake_milvus == [], "the store must never be constructed on refusal"

    def test_the_production_namespace_is_allowed_with_yes(self, monkeypatch, tmp_path, fake_milvus):
        _env(monkeypatch, tmp_path, ns="pi")
        monkeypatch.setattr(sys, "argv", ["rebuild_milvus.py", "--yes"])
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rb.db').as_posix()}")
        import asyncio

        asyncio.run(db.init())
        ids = _seed(db)
        asyncio.run(db.dispose())

        rebuild.main()

        assert len(fake_milvus) == 1
        store = fake_milvus[0]
        assert store.collection == "pi_memories"
        upserted = {fid for _, rows in store.upserts for fid, _ in rows}
        assert upserted == set(ids["u7"][:1] + ids["u7"][2:]) | set(ids["u8"])

    def test_an_empty_source_is_refused(self, monkeypatch, tmp_path, fake_milvus):
        _env(monkeypatch, tmp_path, ns="it")
        monkeypatch.setattr(sys, "argv", ["rebuild_milvus.py", "--yes"])
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rb.db').as_posix()}")
        import asyncio

        asyncio.run(db.init())
        asyncio.run(db.dispose())

        with pytest.raises(SystemExit) as exc:
            rebuild.main()

        assert "zero active memories" in str(exc.value)
        assert fake_milvus == [], "an empty MySQL must not cost the index anything"

    def test_a_missing_milvus_uri_is_refused(self, monkeypatch, tmp_path, fake_milvus):
        _env(monkeypatch, tmp_path, ns="it")
        monkeypatch.delenv("PI_MILVUS_URI")
        monkeypatch.setattr(sys, "argv", ["rebuild_milvus.py", "--yes"])

        with pytest.raises(SystemExit) as exc:
            rebuild.main()

        assert "PI_MILVUS_URI is empty" in str(exc.value)


class TestWalk:
    def test_pages_are_walked_grouped_per_user_and_marked_synced(
        self, monkeypatch, tmp_path, fake_milvus, capsys
    ):
        _env(monkeypatch, tmp_path, ns="it")
        monkeypatch.setattr(sys, "argv", ["rebuild_milvus.py", "--yes", "--batch", "2"])
        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rb.db').as_posix()}")
        import asyncio

        asyncio.run(db.init())
        ids = _seed(db)
        asyncio.run(db.dispose())

        rebuild.main()

        store = fake_milvus[0]
        # drop before setup, and the configured dim reaches the new collection
        assert store.calls[0] == "drop"
        assert store.calls[1] == "setup:8"
        assert store.calls[-1] == "close"
        # Every upsert is single-tenant: the walk groups pages per user before
        # calling the store, which is what keeps _user_filter meaningful.
        assert all(0 < len(rows) for _, rows in store.upserts)
        assert {uid for uid, _ in store.upserts} == {7, 8}
        upserted = [(uid, fid) for uid, rows in store.upserts for fid, _ in rows]
        assert sorted(upserted) == sorted(
            [(7, fid) for fid in ids["u7"] if fid != ids["u7"][1]] + [(8, ids["u8"][0])]
        )
        # The vectors are MySQL's blobs, verbatim - no embedder in the loop.
        vecs = {fid: vec for _, rows in store.upserts for fid, vec in rows}
        assert vecs[ids["u8"][0]] == [9.0] * 8
        # batch=2 over 3 active facts forces two pages; user 7's two facts share
        # one upsert because the walk groups each page per tenant before calling.
        assert len(store.upserts) == 2
        assert [len(rows) for _, rows in store.upserts] == [2, 1]
        out = capsys.readouterr().out
        assert "2/3 fact(s) mirrored" in out

        # Exactly the upserted ids flipped to synced; the decayed row keeps 0.
        async def check():
            repo = UserMemoryRepo(db)
            active = await repo.pending_sync(100)
            return [f.id for f, _ in active]

        db2 = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rb.db').as_posix()}")
        asyncio.run(db2.init())
        pending = asyncio.run(check())
        asyncio.run(db2.dispose())
        assert pending == [], "nothing the walk mirrored may stay pending"

        assert "done: 3 fact(s)" in out
