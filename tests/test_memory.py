"""Tests for semantic memory (P3): cross-session long-term memory."""

from __future__ import annotations

import asyncio

from pi.server.db import Database, MemoryRepo
from pi.tools.base import ToolContext
from pi.tools.memory import RecallTool, RememberTool


def test_memory_repo_search(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")

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
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'm2.db').as_posix()}")

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        ctx = ToolContext(cwd=tmp_path, memory=repo, user_db_id=7)

        remember = RememberTool()
        r = await remember.execute({"text": "project uses redis for locks"}, ctx)
        assert not r.is_error

        recall = RecallTool()
        out = await recall.execute({"query": "redis locks"}, ctx)
        assert "redis" in out.content
        await db.dispose()

    asyncio.run(main())
