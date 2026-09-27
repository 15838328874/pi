"""Real-stack semantic memory: cloud embedding -> local Milvus -> vector recall.

Requires the full PI_ITEST_EMBEDDING_* + PI_ITEST_MILVUS_URI set (skips
otherwise). Uses a throwaway user id and removes its Postgres rows afterwards;
the orphan vectors in Milvus are harmless (rows_by_ids skips missing ids).
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from sqlalchemy import text

from integration.conftest import require_embedding_vars
from pi.llm.embedding import EmbeddingClient
from pi.server.db import Database, MemoryRepo
from pi.server.vectorstore import MilvusStore

pytestmark = pytest.mark.skipif(
    os.environ.get("PI_INTEGRATION") != "1",
    reason="real-stack integration tests; set PI_INTEGRATION=1 + PI_ITEST_* to run",
)


def test_memory_vector_chain():
    require_embedding_vars()
    db = Database(os.environ["PI_ITEST_DATABASE_URL"])
    embedder = EmbeddingClient(
        os.environ["PI_ITEST_EMBEDDING_URL"],
        os.environ["PI_ITEST_EMBEDDING_API_KEY"],
        os.environ["PI_ITEST_EMBEDDING_MODEL"],
    )
    store = MilvusStore(os.environ["PI_ITEST_MILVUS_URI"])
    usage: list[tuple[int, int]] = []
    repo = MemoryRepo(
        db,
        vector_store=store,
        embedder=embedder,
        on_embed_usage=lambda uid, tokens: usage.append((uid, tokens)),
    )
    stamp = int(time.time())
    username = f"itest_mem_{stamp}"

    async def main():
        await db.init()
        # memories.user_id has a real FK: create a throwaway user row first
        async with db.engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (username, password_hash, is_admin, is_active, quota_tokens, created_at)"
                    " VALUES (:u, 'x', FALSE, TRUE, 1000000, '2026-09-27T00:00:00+00:00')"
                ),
                {"u": username},
            )
            await conn.commit()
            user_id = (
                await conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username})
            ).scalar_one()

        await repo.add(user_id, "itest 用户最喜欢的编程语言是 Rust,爱好钓鱼")
        await repo.add(user_id, "itest 用户部署在火山引擎 ECS 上")
        hits = await repo.search(user_id, "编程偏好是什么", k=2)
        assert hits and "Rust" in hits[0].text, [h.text for h in hits]
        assert any(u[0] == user_id and u[1] > 0 for u in usage), usage

        async with db.engine.connect() as conn:
            await conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": user_id})
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            await conn.commit()
        await db.dispose()

    asyncio.run(main())
