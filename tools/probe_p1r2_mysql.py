"""Throwaway smoke for P1 (LIKE escaping) + R2 (replace_chunks) on REAL MySQL.

Local MySQL (pi_py_test). Uses a throwaway user id so the itest DB is left
clean. Does NOT touch the remote RDS in .env.
"""
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from pi.rag.defaults.mysql_store import MysqlChunkStore
from pi.rag.types import Chunk, DocMeta

URL = "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
UID = 888000931  # throwaway


def mk(seq, text, tp=""):
    return Chunk(
        chunk_id=0, doc_key="smoke-meta", user_id=UID, seq=seq,
        text=text, embed_text=text, title_path=tp, page=None,
    )


async def main():
    eng = create_async_engine(URL, pool_pre_ping=True)
    store = MysqlChunkStore(eng, create_schema=True)
    try:
        chunks = [
            mk(0, "配额 100% 且形状 a_b 路径 C:\\tmp"),
            mk(1, "只有百分号 % 这一处"),
            mk(2, "干净块 无特殊符号"),
        ]
        ids = await store.add_chunks(chunks)
        print("P1 seed ids:", ids)

        hits = await store.search_text(UID, "100%", 10)
        print("P1 '%'  hits:", [(h.chunk_id, h.text[:20]) for h in hits])
        assert hits and all("100%" in h.text for h in hits), "literal % mis-matched"

        hits = await store.search_text(UID, "a_b", 10)
        print("P1 '_'  hits:", [(h.chunk_id, h.text[:20]) for h in hits])
        assert hits and all("a_b" in h.text for h in hits), "literal _ mis-matched"

        hits = await store.search_text(UID, "C:\\tmp", 10)
        print("P1 '\\' hits:", [(h.chunk_id, h.text[:20]) for h in hits])
        assert hits and all("C:\\tmp" in h.text for h in hits), "literal backslash mis-matched"

        hits = await store.search_text(UID, "无特殊符号", 10)
        print("P1 control:", [(h.chunk_id, h.text[:20]) for h in hits])
        assert hits and all("无特殊符号" in h.text for h in hits)

        old_ids = set(ids)
        new_chunks = [mk(0, "替换后块 A"), mk(1, "替换后块 B")]
        new_ids = await store.replace_chunks(UID, "smoke-meta", new_chunks)
        after = await store.list_chunks_for_user(UID)
        print("R2 new ids:", new_ids, "after texts:", sorted(c.text for c in after))
        assert len(after) == 2
        assert {c.text for c in after} == {"替换后块 A", "替换后块 B"}
        assert old_ids.isdisjoint({c.chunk_id for c in after}), "old ids survived"
        assert set(new_ids) == {c.chunk_id for c in after}, "id mapping broken"

        assert await store.replace_chunks(UID, "smoke-meta", []) == []
        assert await store.list_chunks_for_user(UID) == []

        print("ALL SMOKE PASSED")
    finally:
        await store.delete_doc(UID, "smoke-meta")
        await eng.dispose()


asyncio.run(main())