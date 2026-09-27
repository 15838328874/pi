"""Tests for vector semantic memory: Milvus/embedding path with graceful fallback.

All external services are faked in-process; no Milvus, no embedding API, no
network. The unconfigured path (MemoryRepo(db) with no args) must behave
exactly like the lexical-only repo these tests replaced.
"""

from __future__ import annotations

import asyncio

from pi.llm.embedding import EmbeddingError
from pi.server.db import Database, MemoryRepo


class FakeEmbedder:
    """Deterministic pseudo-vectors from a hash of the text; can be set to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise EmbeddingError("fake embedder failure")
        vecs = []
        for t in texts:
            h = hash(t)
            vecs.append([float(((h >> (8 * i)) & 0xFF) % 16) for i in range(16)])
        return vecs


class FakeVectorStore:
    """Records adds; search results are scripted per test."""

    def __init__(self, scripted: list[int] | Exception | None = None) -> None:
        self.adds: list[tuple[int, int, str, list[float]]] = []
        self.scripted = scripted
        self.last_search: tuple[int, list[float], int] | None = None

    async def add(self, memory_id: int, user_id: int, text: str, vector: list[float]) -> None:
        self.adds.append((memory_id, user_id, text, vector))

    async def search(self, user_id: int, vector: list[float], k: int) -> list[int]:
        self.last_search = (user_id, vector, k)
        if isinstance(self.scripted, Exception):
            raise self.scripted
        return list(self.scripted or [])

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def _repo(db_path, vector_store=None, embedder=None):
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    return db, MemoryRepo(db, vector_store=vector_store, embedder=embedder)


def test_add_writes_db_and_vector(tmp_path):
    db, repo = _repo(str(tmp_path / "v.db"), FakeVectorStore(), FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")
        row = (await repo.list_for_user(1))[0]
        (memory_id, user_id, text, vec), *_ = repo.vector_store.adds
        assert memory_id == row.id  # id captured from flush, reused as vector key
        assert user_id == 1
        assert text == "the API uses snake_case naming"
        assert len(vec) == 16
        await db.dispose()

    asyncio.run(main())


def test_search_uses_vector_order_not_lexical(tmp_path):
    store = FakeVectorStore()
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        for text in ("the API uses snake_case naming", "we deploy on ECS", "prefer async"):
            await repo.add(1, text)
        rows = await repo.list_for_user(1)
        by_text = {r.text: r.id for r in rows}
        # lexically only "snake_case" should match; script the vector order as
        # [async-row, api-row] and prove the result follows the vector order.
        store.scripted = [by_text["prefer async"], by_text["the API uses snake_case naming"]]
        hits = await repo.search(1, "api naming convention", k=2)
        assert [h.id for h in hits] == store.scripted
        await db.dispose()

    asyncio.run(main())


def test_search_passes_user_id_to_store(tmp_path):
    store = FakeVectorStore(scripted=[])
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(7, "note")
        await repo.search(7, "anything", k=3)
        assert store.last_search is not None
        assert store.last_search[0] == 7
        await db.dispose()

    asyncio.run(main())


def test_search_falls_back_lexical_on_embedder_error(tmp_path):
    db, repo = _repo(str(tmp_path / "v.db"), FakeVectorStore(), FakeEmbedder(fail=True))

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")
        hits = await repo.search(1, "api naming", k=2)
        assert hits and "snake_case" in hits[0].text
        await db.dispose()

    asyncio.run(main())


def test_search_falls_back_lexical_on_store_error(tmp_path):
    store = FakeVectorStore(scripted=RuntimeError("milvus down"))
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")
        hits = await repo.search(1, "api naming", k=2)
        assert hits and "snake_case" in hits[0].text
        await db.dispose()

    asyncio.run(main())


def test_search_falls_back_lexical_on_empty_hits(tmp_path):
    db, repo = _repo(str(tmp_path / "v.db"), FakeVectorStore(scripted=[]), FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")
        hits = await repo.search(1, "api naming", k=2)
        assert hits and "snake_case" in hits[0].text
        await db.dispose()

    asyncio.run(main())


def test_add_swallows_embedder_error_text_still_searchable(tmp_path):
    db, repo = _repo(str(tmp_path / "v.db"), FakeVectorStore(), FakeEmbedder(fail=True))

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")  # must not raise
        hits = await repo.search(1, "api naming", k=2)
        assert hits and "snake_case" in hits[0].text
        await db.dispose()

    asyncio.run(main())


def test_add_swallows_store_error(tmp_path):
    store = FakeVectorStore(scripted=None)

    async def fail_add(*args, **kwargs):
        raise RuntimeError("milvus down")

    store.add = fail_add
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")  # must not raise
        rows = await repo.list_for_user(1)
        assert len(rows) == 1
        await db.dispose()

    asyncio.run(main())


def test_vector_hits_missing_in_db_are_skipped(tmp_path):
    store = FakeVectorStore()
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "the API uses snake_case naming")
        row = (await repo.list_for_user(1))[0]
        store.scripted = [9999, row.id]  # 9999 not in DB -> skipped
        hits = await repo.search(1, "api naming", k=2)
        assert [h.id for h in hits] == [row.id]
        await db.dispose()

    asyncio.run(main())


def test_vector_hits_from_other_user_are_skipped(tmp_path):
    store = FakeVectorStore()
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        await repo.add(1, "user one note")
        await repo.add(2, "user two note")
        other = (await repo.list_for_user(2))[0]
        store.scripted = [other.id]  # belongs to user 2, not user 1
        hits = await repo.search(1, "note", k=2)
        # foreign hit dropped, then lexical fallback returns the owned row
        assert [h.text for h in hits] == ["user one note"]
        await db.dispose()

    asyncio.run(main())


def test_unconfigured_repo_is_lexical_only(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'plain.db').as_posix()}")

    async def main():
        await db.init()
        repo = MemoryRepo(db)  # no vector_store / embedder, as before
        await repo.add(1, "the API uses snake_case naming")
        await repo.add(1, "we deploy on Volcano Engine ECS")
        hits = await repo.search(1, "api naming convention", k=2)
        assert hits and "snake_case" in hits[0].text
        assert await repo.search(2, "api naming", k=2) == []
        await db.dispose()

    asyncio.run(main())


def test_create_app_wires_vector_memory(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pi.server.app import create_app
    from pi.server.config import ServerSettings

    monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'w.db').as_posix()}")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("PI_EMBEDDING_URL", "http://127.0.0.1:19531")
    monkeypatch.setenv("PI_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("PI_EMBEDDING_MODEL", "test-model")
    monkeypatch.setenv("PI_MILVUS_URI", "http://127.0.0.1:19531")
    settings = ServerSettings.from_env()
    assert settings.vector_memory_enabled

    with TestClient(create_app(settings)) as client:
        assert client.app.state.vector_store is not None
        r = client.get("/readyz")
        assert r.status_code == 200
        assert "milvus" in r.json()["checks"]  # value is "degraded" without a live Milvus
