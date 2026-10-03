"""记忆精判阶段测试：reranker 判重 + LLM judge 冲突覆盖 + 降级链。

这些测试只注入假的 reranker/judge 回调，不依赖 embedding/Milvus——目的是把
`_resolve_verdict` 的分支（重复/冲突/新事实/降级）逐个钉死。
"""

from __future__ import annotations

import asyncio

from conftest import TEST_DB_URL

from pi.server.cache import MemoryBackend
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


def test_reranker_length_mismatch_treats_as_new(tmp_path):
    """reranker 返回长度与候选数不一致 → 判 new（写新），不崩。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return []  # 长度 0，与候选数（≥1）不匹配 → falsy → 判 new

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank)
        assert await repo.add(1, "项目 A 的代号是 Orion") is True
        assert await repo.add(1, "项目 B 的代号是 Atlas") is True  # 长度不匹配 → new
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_reranker_low_score_overrides_lexical_dup(tmp_path):
    """reranker 低分优先于词法：即使词法会判重，reranker 说低分就写新。

    这是有意的设计——reranker 比词法准，低分意味着"不相关"，词法的逐字重复
    判断反而可能是噪声。固化为显式断言，防未来有人"顺手"加回词法兜底。
    """
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.3] * len(docs)

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank)
        assert await repo.add(1, "项目代号是 Orion") is True
        # 词法 Jaccard 会判重（逐字相同），但 reranker 低分 → 写新
        assert await repo.add(1, "项目代号是 Orion") is True
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


def test_reranker_exact_threshold_boundary(tmp_path):
    """reranker 分数恰好等于 0.65 → 判重（>= 阈值）。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.65] * len(docs)

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank)
        assert await repo.add(1, "项目代号是 Orion") is True
        assert await repo.add(1, "项目代号是 Orion") is False  # 恰好 0.65 → 判重
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


def test_negation_wording_conflict_overwrites(tmp_path):
    """否定措辞（'不是X了'）被判 conflict → 覆盖成否定文本（已知边界，记录非推崇）。

    这是「不区分 UPDATE 和 DELETE」的代价：否定义被当成新值覆盖，而非删除旧值。
    后果轻（检索可读），真正的删除是显式操作（待办）。固化当前行为防意外改变。
    """
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.9] * len(docs)

    async def judge(user_id, new, existing):
        return "conflict"

    async def main():
        await db.init()
        repo = _repo(db, reranker=rerank, judge=judge)
        assert await repo.add(1, "用户的主语言是Rust") is True
        assert await repo.add(1, "用户的主语言不是Rust了") is True  # conflict → 覆盖
        rows = await repo.list_for_user(1)
        assert len(rows) == 1
        assert rows[0].text == "用户的主语言不是Rust了"  # 否定义被当成新值（已知边界）
        await db.dispose()

    asyncio.run(main())


def test_concurrent_add_with_reranker_and_eviction(tmp_path):
    """并发 add + reranker + 驱逐三者叠加：锁串行化，最终只留 limit 条、不崩。"""
    db = Database(TEST_DB_URL)

    async def rerank(user_id, query, docs):
        return [0.3] * len(docs)  # 低分 → 全部判 new（不判重，逼出驱逐路径）

    async def main():
        await db.init()
        repo = MemoryRepo(db, cache=MemoryBackend(), reranker=rerank, memory_limit=3)
        texts = [f"fact number {i}" for i in range(10)]
        results = await asyncio.gather(*[repo.add(1, t) for t in texts])
        assert all(results)  # 低分 → 每条都写
        rows = await repo.list_for_user(1)
        assert len(rows) == 3  # 驱逐后只留最新 3 条
        texts_kept = {r.text for r in rows}
        assert "fact number 9" in texts_kept  # 最新保留
        assert "fact number 0" not in texts_kept  # 最旧驱逐
        await db.dispose()

    asyncio.run(main())
