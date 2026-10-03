"""记忆精判阶段测试：reranker 判重 + LLM judge 冲突覆盖 + 降级链。

这些测试只注入假的 reranker/judge 回调，不依赖 embedding/Milvus——目的是把
`_resolve_verdict` 的分支（重复/冲突/新事实/降级）逐个钉死。
"""

from __future__ import annotations

import asyncio

from conftest import TEST_DB_URL

from pi.server.db import Database, MemoryRepo


def _repo(db, reranker=None, judge=None):
    return MemoryRepo(db, reranker=reranker, judge=judge)


def test_reranker_high_score_no_judge_dedups(tmp_path):
    """reranker 高分且无 judge → 判重（保守默认）。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.9] * len(docs)

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_reranker_low_score_inserts_new(tmp_path):
    """reranker 低分 → 新事实，写入。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.3] * len(docs)

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is True  # reranker 说低分 → 仍写
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_judge_conflict_overwrites_in_place(tmp_path):
    """judge 判 conflict → 原地覆盖旧行（id/created_at 保留，text 更新）。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.9] * len(docs)

    async def judge(user_id, new, existing):
        return "conflict"

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank, judge=judge)
        assert await repo.add(1, "用户偏好中文回答") is True
        old = (await repo.list_for_user(1))[0]
        assert await repo.add(1, "用户偏好英文回答") is True  # 冲突 → 覆盖
        rows = await repo.list_for_user(1)
        assert len(rows) == 1
        assert rows[0].id == old.id  # 原地更新，不新增行
        assert rows[0].text == "用户偏好英文回答"
        await db.dispose()

    asyncio.run(main())


def test_judge_duplicate_skips(tmp_path):
    """judge 判 duplicate → 不写。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.9] * len(docs)

    async def judge(user_id, new, existing):
        return "duplicate"

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank, judge=judge)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "这个项目的代号叫 Orion") is False  # judge 说重复
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_judge_new_inserts(tmp_path):
    """judge 判 new → 写新（两条都保留）。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.9] * len(docs)

    async def judge(user_id, new, existing):
        return "new"

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank, judge=judge)
        assert await repo.add(1, "项目 A 用 MySQL") is True
        assert await repo.add(1, "项目 B 用 MySQL") is True  # judge 说新事实
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_reranker_failure_degrades_to_lexical(tmp_path):
    """reranker 抛异常 → 降级到词法判重（写入仍安全）。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        raise RuntimeError("rerank down")

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False  # 词法兜底判重
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_judge_failure_treats_as_new(tmp_path):
    """judge 抛异常 → fail-open 判 new（写新）。丢写比偶发重复更糟。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.9] * len(docs)

    async def judge(user_id, new, existing):
        raise RuntimeError("llm down")

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank, judge=judge)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "这个项目的代号叫 Orion") is True  # judge 挂了 → 写新（不丢）
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_no_reranker_falls_back_to_cosine_path(tmp_path):
    """无 reranker → 完全走原有 cosine + 词法路径（行为不变）。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)  # 无 reranker/judge/embedder
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())
