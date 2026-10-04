"""Tests for vector semantic memory: Milvus/embedding path with graceful fallback.

All external services are faked in-process; no Milvus, no embedding API, no
network. The unconfigured path (MemoryRepo(db) with no args) must behave
exactly like the lexical-only repo these tests replaced.
"""

from __future__ import annotations

import asyncio

from conftest import TEST_DB_URL

from pi.llm.embedding import EmbeddingError, EmbeddingResult
from pi.server.db import Database, MemoryRepo


class FakeEmbedder:
    """Deterministic pseudo-vectors from a hash of the text; can be set to fail."""

    def __init__(self, fail: bool = False, usage_tokens: int = 7) -> None:
        self.fail = fail
        self.usage_tokens = usage_tokens
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(texts)
        if self.fail:
            raise EmbeddingError("fake embedder failure")
        vecs = []
        for t in texts:
            h = hash(t)
            vecs.append([float(((h >> (8 * i)) & 0xFF) % 16) for i in range(16)])
        return EmbeddingResult(vectors=vecs, usage_tokens=self.usage_tokens)


class FakeVectorStore:
    """Records adds; search results are scripted per test as (id, cosine)."""

    def __init__(self, scripted: list[tuple[int, float]] | Exception | None = None) -> None:
        self.adds: list[tuple[int, int, str, list[float]]] = []
        self.scripted = scripted
        self.last_search: tuple[int, list[float], int] | None = None

    async def add(self, memory_id: int, user_id: int, text: str, vector: list[float]) -> None:
        self.adds.append((memory_id, user_id, text, vector))

    async def search(self, user_id: int, vector: list[float], k: int) -> list[tuple[int, float]]:
        self.last_search = (user_id, vector, k)
        if isinstance(self.scripted, Exception):
            raise self.scripted
        return list(self.scripted or [])

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def _repo(db_path, vector_store=None, embedder=None):
    db = Database(TEST_DB_URL)
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
        # [(async-row, .9), (api-row, .8)] and prove the result follows it.
        store.scripted = [
            (by_text["prefer async"], 0.9),
            (by_text["the API uses snake_case naming"], 0.8),
        ]
        hits = await repo.search(1, "api naming convention", k=2)
        assert [h.id for h in hits] == [mid for mid, _ in store.scripted]
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
        store.scripted = [(9999, 0.9), (row.id, 0.8)]  # 9999 not in DB -> skipped
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
        store.scripted = [(other.id, 0.9)]  # belongs to user 2, not user 1
        hits = await repo.search(1, "note", k=2)
        # foreign hit dropped, then lexical fallback returns the owned row
        assert [h.text for h in hits] == ["user one note"]
        await db.dispose()

    asyncio.run(main())


def test_semantic_dedup_requires_judge(tmp_path):
    """措辞不同但语义相同，无 judge 时无法去重（词法 Jaccard 漏判 → 写重）。

    语义去重（"回答请用中文" vs "用户偏好中文回答"）需要 judge；无 judge 时
    只剩词法（只抓逐字重复），措辞差异大的同义改写会写重——宁可重复不丢。
    """
    store = FakeVectorStore()
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        assert await repo.add(1, "用户偏好中文回答") is True
        # 无 judge：措辞不同语义相同 → 词法漏判 → 写重（语义去重需 judge）
        assert await repo.add(1, "回答请用中文") is True
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_add_keeps_distinct_below_similarity_threshold(tmp_path):
    """共享词但语义不同 → 相似度 0.5 < 0.92 → 不去重（词法会误杀）。"""
    store = FakeVectorStore()
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        assert await repo.add(1, "项目 A 的代号是 Orion") is True
        row = (await repo.list_for_user(1))[0]
        store.scripted = [(row.id, 0.5)]  # 相似但不够 → 视为新事实
        assert await repo.add(1, "项目 B 的代号是 Atlas") is True
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_add_semantic_dedup_falls_back_to_lexical_on_store_error(tmp_path):
    """语义去重时向量库挂了 → 降级词法去重，写入仍安全。"""
    store = FakeVectorStore(scripted=RuntimeError("milvus down"))
    db, repo = _repo(str(tmp_path / "v.db"), store, FakeEmbedder())

    async def main():
        await db.init()
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False  # 词法兜底
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_add_after_eviction_not_blocked_by_orphan_vector(tmp_path):
    """驱逐留下的孤儿向量不应让重新 add 相同记忆被误判重。

    `_enforce_limit` 只删 MySQL 行、Milvus 向量留着（注释说是无害孤儿）。
    但语义去重若只看向量分数、不验证命中的 memory_id 是否还在 MySQL，就会
    命中已驱逐的孤儿向量 → 高分 → 误判重 → 这条记忆再也写不回来。
    """
    store = FakeVectorStore()
    embedder = FakeEmbedder()
    db = Database(TEST_DB_URL)
    repo = MemoryRepo(db, vector_store=store, embedder=embedder, memory_limit=2)

    async def main():
        await db.init()
        assert await repo.add(1, "fact A") is True
        assert await repo.add(1, "fact B") is True
        assert await repo.add(1, "fact C") is True  # 触发驱逐：fact A 出局
        assert "fact A" not in {r.text for r in await repo.list_for_user(1)}

        # fact A 的孤儿向量仍在 Milvus（FakeVectorStore 记录了它的 memory_id）
        a_mid = next(mid for mid, _uid, text, _vec in store.adds if text == "fact A")
        store.scripted = [(a_mid, 0.96)]  # 语义去重命中孤儿，高分
        assert await repo.add(1, "fact A") is True  # 不应被孤儿挡路
        assert "fact A" in {r.text for r in await repo.list_for_user(1)}
        await db.dispose()

    asyncio.run(main())


def test_conflict_supersedes_with_new_milvus_vector(tmp_path):
    """judge 判 conflict → 版本化：新行 id 入 Milvus，旧行退役（不再二次 upsert）。"""
    store = FakeVectorStore()
    embedder = FakeEmbedder()
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "conflict", 0

    repo = MemoryRepo(db, vector_store=store, embedder=embedder, judge=judge)

    async def main():
        await db.init()
        assert await repo.add(1, "用户偏好中文回答") is True
        old = (await repo.list_for_user(1))[0]
        # 让向量召回命中旧行（cosine 0.8，低于 0.88 判重阈值），触发 judge→conflict
        store.scripted = [(old.id, 0.8)]
        assert await repo.add(1, "用户偏好英文回答") is True
        rows = await repo.list_for_user(1)
        assert len(rows) == 1
        assert rows[0].id != old.id  # 新行，不是原地覆盖
        assert rows[0].text == "用户偏好英文回答"
        # Milvus：旧行 id 一次（初次 add），新行 id 一次（版本化写入），各一次
        mids = [mid for mid, _u, _t, _v in store.adds]
        assert mids.count(old.id) == 1
        assert mids.count(rows[0].id) == 1
        assert store.adds[-1][2] == "用户偏好英文回答"
        await db.dispose()

    asyncio.run(main())


def test_unconfigured_repo_is_lexical_only(tmp_path):
    db = Database(TEST_DB_URL)

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

    monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
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


class TestEmbedUsageMetering:
    """Embedding spend is metered like LLM tokens (L-fix parity with main)."""

    def _repo(self, db_path, embedder, usage_list):
        db = Database(TEST_DB_URL)

        async def on_usage(user_id: int, tokens: int) -> None:
            usage_list.append((user_id, tokens))

        return db, MemoryRepo(db, vector_store=FakeVectorStore(), embedder=embedder, on_embed_usage=on_usage)

    def test_usage_recorded_after_successful_embed(self, tmp_path):
        usage: list[tuple[int, int]] = []
        db, repo = self._repo(str(tmp_path / "m.db"), FakeEmbedder(usage_tokens=11), usage)

        async def main():
            await db.init()
            await repo.add(3, "note")
            assert usage == [(3, 11)]  # add path meters the embed spend
            await repo.search(3, "note", k=2)
            assert usage == [(3, 11), (3, 11)]  # search path too
            await db.dispose()

        asyncio.run(main())

    def test_embed_failure_records_nothing(self, tmp_path):
        usage: list[tuple[int, int]] = []
        db, repo = self._repo(str(tmp_path / "m.db"), FakeEmbedder(fail=True), usage)

        async def main():
            await db.init()
            await repo.add(3, "note")  # embed raises -> nothing spent -> nothing metered
            assert usage == []
            await db.dispose()

        asyncio.run(main())

    def test_store_failure_still_meters_the_embed(self, tmp_path):
        usage: list[tuple[int, int]] = []
        store = FakeVectorStore()

        async def fail_add(*args, **kwargs):
            raise RuntimeError("milvus down")

        store.add = fail_add
        db = Database(TEST_DB_URL)

        async def on_usage(user_id: int, tokens: int) -> None:
            usage.append((user_id, tokens))

        repo = MemoryRepo(db, vector_store=store, embedder=FakeEmbedder(usage_tokens=5), on_embed_usage=on_usage)

        async def main():
            await db.init()
            await repo.add(3, "note")  # embed succeeded and was billed; index died after
            assert usage == [(3, 5)]
            await db.dispose()

        asyncio.run(main())

    def test_search_failure_reuses_embedding_no_double_bill(self, tmp_path):
        """embed 成功 + 语义判重的 search 挂了 → vec 复用，只计一次费。"""
        usage: list[tuple[int, int]] = []
        store = FakeVectorStore(scripted=RuntimeError("milvus down"))
        embedder = FakeEmbedder(usage_tokens=5)
        db = Database(TEST_DB_URL)

        async def on_usage(user_id: int, tokens: int) -> None:
            usage.append((user_id, tokens))

        repo = MemoryRepo(db, vector_store=store, embedder=embedder, on_embed_usage=on_usage)

        async def main():
            await db.init()
            assert await repo.add(3, "note") is True  # dedup search 挂了，词法兜底仍写入
            # add 里 embed 一次；_vector_add 复用 vec，不再 embed → 只计一次
            assert usage == [(3, 5)]
            assert len(embedder.calls) == 1
            await db.dispose()

        asyncio.run(main())
