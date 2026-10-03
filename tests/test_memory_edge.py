"""记忆实现的边界/攻击性测试：token 化、垃圾文本、k 边界、空 query。

这些不是"正常路径"测试，而是刻意戳边角：中文单字 token 的局限、纯符号
文本、k 的极端值。目的有二——守住已经修掉的坑（无 token 文本不落库），
并把词法层的固有局限钉成显式断言，防止未来改动悄悄改变行为。
"""

from __future__ import annotations

import asyncio

from conftest import TEST_DB_URL

from pi.server.db import Database, MemoryRepo, _terms


# ---------------------------------------------------------------------------
# token 化
# ---------------------------------------------------------------------------


def test_terms_chinese_is_single_char():
    """中文按单字切分（不是词），英文/数字按整词切分。"""
    assert _terms("项目代号是 Orion") == {"项", "目", "代", "号", "是", "orion"}
    assert _terms("API 版本 2") == {"api", "版", "本", "2"}


def test_terms_no_token_for_symbols():
    """纯 emoji / 标点 / 空白没有可检索 token。"""
    assert _terms("😀😀😀") == set()
    assert _terms("！！！…") == set()
    assert _terms("  \t\n ") == set()


# ---------------------------------------------------------------------------
# add 的输入守卫
# ---------------------------------------------------------------------------


def test_add_rejects_no_token_text(tmp_path):
    """纯 emoji/标点文本不落库（检索不到的垃圾记忆，等同空白拒绝）。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "😀😀😀") is False
        assert await repo.add(1, "！！！") is False
        assert await repo.add(1, "...") is False
        assert len(await repo.list_for_user(1)) == 0
        await db.dispose()

    asyncio.run(main())


def test_add_english_dedup_case_insensitive(tmp_path):
    """英文大小写不敏感：API vs api 视为同一记忆。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "the API uses snake_case") is True
        assert await repo.add(1, "the api uses snake_case") is False
        assert len(await repo.list_for_user(1)) == 1
        await db.dispose()

    asyncio.run(main())


def test_lexical_dedup_chinese_synonym_is_leak(tmp_path):
    """词法层的已知局限：中文近义（回答 vs 回复）Jaccard < 阈值 → 漏判。

    单字 token 下 Jaccard 0.85 只对「几乎逐字相同」判重；语义近但措辞略变
    就漏。语义层（embedding）在向量部署下是主力，纯词法部署会重复。此测试
    把该局限钉成显式断言——如果未来改 _terms 分词，这里会响。
    """
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        assert await repo.add(1, "用户偏好中文回答") is True
        assert await repo.add(1, "用户偏好中文回复") is True  # 词法漏判
        assert len(await repo.list_for_user(1)) == 2
        await db.dispose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# search 边界
# ---------------------------------------------------------------------------


def test_search_k_zero_or_negative_no_crash(tmp_path):
    """k=0 / 负数不崩，返回空列表。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        await repo.add(1, "the API uses snake_case naming")
        assert await repo.search(1, "api", k=0) == []
        assert await repo.search(1, "api", k=-1) == []
        assert await repo.search(1, "api", k=3) != []  # 正常 k 仍有结果
        await db.dispose()

    asyncio.run(main())


def test_search_empty_query_no_crash(tmp_path):
    """空 query 不崩，返回空列表。"""
    db = Database(TEST_DB_URL)

    async def main():
        await db.init()
        repo = MemoryRepo(db)
        await repo.add(1, "the API uses snake_case naming")
        assert await repo.search(1, "", k=3) == []
        assert await repo.search(1, "😀", k=3) == []
        await db.dispose()

    asyncio.run(main())
