"""记忆精判测试：LLM judge 三分类（duplicate/conflict/new）+ 多候选 target 选择。

架构（去 reranker 后）：embedding/词法召回 top-k 候选 → 全部喂给 judge → judge 输出
(verdict, target_idx)。本文件用假 judge 把 `_resolve_verdict` 的分支逐个钉死。
"""

from __future__ import annotations

import asyncio

from conftest import TEST_DB_URL

from pi.server.cache import MemoryBackend
from pi.server.db import Database, MemoryRepo


def _repo(db, judge=None):
    return MemoryRepo(db, judge=judge)


def test_judge_conflict_supersedes_target(tmp_path):
    """judge 判 conflict + target → 版本化：写新行 + 旧行退役（superseded_by）。

    检索只返回新行（当前有效）；旧行仍在表里（历史保留，可回滚）。
    """
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "conflict", 0

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "用户偏好中文回答") is True
        old = (await repo.list_for_user(1))[0]
        assert await repo.add(1, "用户偏好英文回答") is True  # conflict → 版本化
        rows = await repo.list_for_user(1)
        assert len(rows) == 1  # 只有当前有效的（新行）
        assert rows[0].id != old.id  # 是新行，不是原地覆盖
        assert rows[0].text == "用户偏好英文回答"
        # 旧行仍在表里且被退役（历史保留）
        async with db.engine.connect() as c:
            from sqlalchemy import text as sqltext
            old_text = (await c.execute(
                sqltext("SELECT text FROM memories WHERE id = :i"), {"i": old.id}
            )).scalar_one()
            assert old_text == "用户偏好中文回答"
            sup = (await c.execute(
                sqltext("SELECT superseded_by FROM memories WHERE id = :i"), {"i": old.id}
            )).scalar_one()
            assert sup == rows[0].id  # 旧行指向新行
        await db.dispose()

    asyncio.run(main())


def test_judge_duplicate_falls_back_to_new(tmp_path):
    """judge 判 duplicate → 降级为写新（宁可重复不丢）。

    因为 cosine 快速路径已处理了 ≥0.92 的可靠同义改写，走到 judge 的 duplicate
    不可靠（可能是 conflict 被误判）。宁可写重，不丢新值。
    """
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "duplicate", 0

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "这个项目的代号叫 Orion") is True  # judge 判 duplicate → 写新
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_judge_new_inserts(tmp_path):
    """judge 判 new → 写新（两条都保留）。"""
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "new", None

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "项目 A 用 MySQL") is True
        assert await repo.add(1, "项目 B 用 MySQL") is True
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_judge_picks_target_among_multiple_candidates(tmp_path):
    """核心：judge 从多条候选中自己挑 target（不依赖预选 top-1）。

    「用户0的主语言改成Go」会同时召回「用户0的主语言是Rust」（正确冲突对象）和
    「用户1的主语言是Go」（共享"Go"的干扰项）。judge 必须选对前者。
    """
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        # 模拟真实 judge：从候选里找含"用户0"的那条
        for i, c in enumerate(candidates):
            if "用户0" in c:
                return "conflict", i
        return "new", None

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "用户0的主语言是Rust") is True
        assert await repo.add(1, "用户1的主语言是Go") is True
        # 第三次：judge 应覆盖"用户0"那条，而不是"用户1"那条
        assert await repo.add(1, "用户0的主语言改成Go") is True
        rows = await repo.list_for_user(1)
        texts = {r.text for r in rows}
        assert len(rows) == 2  # 覆盖一条，仍两条
        assert "用户0的主语言改成Go" in texts  # 新值覆盖了旧值
        assert "用户0的主语言是Rust" not in texts  # 旧值消失
        assert "用户1的主语言是Go" in texts  # 干扰项未被误覆盖
        await db.dispose()

    asyncio.run(main())


def test_judge_failure_treats_as_new(tmp_path):
    """judge 抛异常 → fail-open 判 new（写新）。丢写比偶发重复更糟。"""
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        raise RuntimeError("llm down")

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "这个项目的代号叫 Orion") is True  # judge 挂了 → 写新
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_judge_target_out_of_range_ignored(tmp_path):
    """judge 返回越界 target → 忽略，按 new 处理（写新，不崩）。"""
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "conflict", 99  # 越界

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "这个项目的代号叫 Orion") is True  # 越界 target 忽略 → 写新
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_negation_wording_conflict_overwrites(tmp_path):
    """否定措辞（'不是X了'）被判 conflict → 覆盖成否定文本（已知边界，记录非推崇）。

    这是「不区分 UPDATE 和 DELETE」的代价：否定义被当成新值覆盖，而非删除旧值。
    """
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "conflict", 0

    async def main():
        await db.init()
        repo = _repo(db, judge=judge)
        assert await repo.add(1, "用户的主语言是Rust") is True
        assert await repo.add(1, "用户的主语言不是Rust了") is True  # conflict → 覆盖
        rows = await repo.list_for_user(1)
        assert len(rows) == 1
        assert rows[0].text == "用户的主语言不是Rust了"
        await db.dispose()

    asyncio.run(main())


def test_no_judge_falls_back_to_cosine_path(tmp_path):
    """无 judge → 完全走原有 cosine + 词法路径（行为不变）。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)  # 无 judge/embedder
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_add_long_text_no_crash(tmp_path):
    """超长文本（>4096 字符）add 不崩，MySQL 存全量、可词法检索。"""
    db = Database(TEST_DB_URL)
    long_text = "alpha " * 3000 + "特殊标记XYZ789"  # ~18000 字符

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, long_text) is True
        hits = await repo.search(1, "特殊标记XYZ789", k=1)
        assert hits and "特殊标记XYZ789" in hits[0].text
        await db.dispose()

    asyncio.run(main())


def test_concurrent_add_with_judge_and_eviction(tmp_path):
    """并发 add + judge + 驱逐三者叠加：锁串行化，最终只留 limit 条、不崩。"""
    db = Database(TEST_DB_URL)

    async def judge(user_id, new, candidates):
        return "new", None  # 全部判 new，逼出驱逐路径

    async def main():
        await db.init()
        repo = MemoryRepo(db, cache=MemoryBackend(), judge=judge, memory_limit=3)
        texts = [f"fact number {i}" for i in range(10)]
        results = await asyncio.gather(*[repo.add(1, t) for t in texts])
        assert all(results)
        rows = await repo.list_for_user(1)
        assert len(rows) == 3
        texts_kept = {r.text for r in rows}
        assert "fact number 9" in texts_kept
        assert "fact number 0" not in texts_kept
        await db.dispose()

    asyncio.run(main())
