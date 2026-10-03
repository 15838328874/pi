"""Tests for semantic memory (P3): cross-session long-term memory."""

from __future__ import annotations

import asyncio

from conftest import TEST_DB_URL

from pi.server.db import Database, MemoryRepo
from pi.tools.base import ToolContext
from pi.tools.memory import RecallTool, RememberTool


def test_memory_repo_search(tmp_path):
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        await repo.add(1, "the API uses snake_case naming")
        await repo.add(1, "we deploy on Volcano Engine ECS")
        await repo.add(1, "prefer async over threads")

        hits = await repo.search(1, "api naming convention", k=2)
        assert hits and "snake_case" in hits[0].text

        assert await repo.search(1, "zzz unrelated", k=2) == []
        # scoped per user
        assert await repo.search(2, "api naming", k=2) == []
        await db.dispose()

    asyncio.run(main())


def test_remember_and_recall_tools(tmp_path):
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        ctx = ToolContext(cwd=tmp_path)
        ctx.memory = repo
        ctx.user_db_id = 7

        remember = RememberTool()
        r = await remember.execute({"text": "project uses redis for locks"}, ctx)
        assert not r.is_error

        recall = RecallTool()
        out = await recall.execute({"query": "redis locks"}, ctx)
        assert "redis" in out.content
        await db.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# add 的守卫：去重 + 每用户上限（记忆写入会随压缩自动发生，必须防脏）
# ---------------------------------------------------------------------------


def test_memory_add_dedup_verbatim(tmp_path):
    """完全相同的记忆第二次 add 返回 False，且不重复落库。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_memory_add_dedup_near_identical(tmp_path):
    """几乎相同（只多一个尾字，Jaccard 仍 ≥ 阈值）也去重。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion啊") is False
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_memory_add_keeps_distinct_facts(tmp_path):
    """语义不同的事实不去重（即使共享部分词）。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "项目 A 的代号是 Orion") is True
        assert await repo.add(1, "项目 B 的代号是 Atlas") is True
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_memory_add_per_user_cap_evicts_oldest(tmp_path):
    """超过 memory_limit 后，最旧的被驱逐（保留最新 limit 条）。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db, memory_limit=3)
        for i in range(5):
            assert await repo.add(1, f"fact number {i}") is True
        rows = await repo.list_for_user(1)
        assert len(rows) == 3
        texts = {r.text for r in rows}
        assert "fact number 0" not in texts  # 最旧的被驱逐
        assert "fact number 1" not in texts
        assert "fact number 4" in texts
        await db.dispose()

    asyncio.run(main())


def test_memory_add_empty_text_rejected(tmp_path):
    """空白文本不落库。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "   ") is False
        assert len(await repo.list_for_user(1)) == 0
        await db.dispose()

    asyncio.run(main())


def test_memory_dedup_scoped_per_user(tmp_path):
    """去重只在同一用户内生效；不同用户互不干扰。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "部署在 ECS") is True
        assert await repo.add(2, "部署在 ECS") is True  # 不同用户，不算重复
        assert len(await repo.list_for_user(1)) == 1
        assert len(await repo.list_for_user(2)) == 1
        await db.dispose()

    asyncio.run(main())


def test_remember_tool_reports_dedup(tmp_path):
    """remember 工具在去重时返回明确提示，而不是假装记住了。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        ctx = ToolContext(cwd=tmp_path)
        ctx.memory = repo
        ctx.user_db_id = 7
        remember = RememberTool()
        r1 = await remember.execute({"text": "项目代号是 Orion"}, ctx)
        assert "remembered" in r1.content and "already" not in r1.content
        r2 = await remember.execute({"text": "项目代号是 Orion"}, ctx)
        assert "already" in r2.content
        await db.dispose()

    asyncio.run(main())
