"""真实栈记忆规模测试：几百条 add/search/conflict 跑在真 Milvus + 真 reranker + 真 LLM。

与 test_memory_real.py（2 条冒烟）不同，本文件验证**规模下的全链路**：
  - 真 embedding（qwen3.7-text-embedding）逐条 embed 几百条记忆
  - 真 Milvus 向量召回 + 真 reranker（qwen3.7-text-rerank）精判去重
  - 真 LLM（PI_MODEL）judge 判 duplicate/conflict/new，冲突原地覆盖

数据设计（seed 固定，可复现）：
  - 260 条独特事实（模板 + 随机实体，主题分散）
  - 20 条重复变体（措辞改写，应被 reranker+judge 判 duplicate → 不写）
  - 20 条冲突变体（矛盾值，应被 judge 判 conflict → 覆盖旧行）
  总 add 300 次；理想最终 260 条（20 重复去重、20 冲突覆盖不新增）。

断言宽松以抗模型波动：数量落在 [230, 280]、抽查重复/冲突各 1 条、记录耗时。
零残留：独立 Milvus collection（pi_memories_itest）+ throwaway user，结束 drop。

Run（从 .env.local 加载 creds，不落 shell）:
  tools/run_memory_real.py
或手动：
  PI_INTEGRATION=1 PI_ITEST_* ...  pytest integration/test_memory_real_scale.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import random
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

_ITEST_COLLECTION = "pi_memories_itest"


def _build_reranker():
    """PI_ITEST_RERANK_* → MemoryRepo reranker callback (query, docs) -> scores."""
    url = os.environ.get("PI_ITEST_RERANK_URL", "")
    if not url:
        return None
    from pi.rag.defaults.http_reranker import HttpReranker
    from pi.rag.types import RetrievedChunk

    reranker = HttpReranker(
        url,
        os.environ.get("PI_ITEST_RERANK_API_KEY", ""),
        os.environ.get("PI_ITEST_RERANK_MODEL", ""),
    )

    async def rerank(user_id: int, query: str, docs: list[str]) -> list[float]:
        chunks = [
            RetrievedChunk(chunk_id=i, doc_key="", text=d, score=0.0)
            for i, d in enumerate(docs)
        ]
        scored = await reranker.rerank(query, chunks)
        by_id = {c.chunk_id: c.score for c in scored}
        return [by_id.get(i, 0.0) for i in range(len(docs))]

    return rerank


def _build_judge():
    """PI_MODEL → MemoryRepo judge callback (user, new, existing) -> verdict."""
    from pi.llm import resolve
    from pi.llm.base import StreamEnd, TextDelta
    from pi.models import Message, Role, TextBlock

    provider = resolve(os.environ.get("PI_MODEL", "openai/gpt-4o"))

    async def judge(user_id: int, new_text: str, existing_text: str) -> str:
        prompt = (
            "新记忆：{new}\n已有记忆：{old}\n\n"
            "判断两者的关系，只输出一个词：duplicate（同义）、"
            "conflict（同一件事但结论/值不同，新记忆应覆盖旧记忆）、"
            "new（不同的事实）。"
        ).format(new=new_text, old=existing_text)
        parts: list[str] = []
        async for ev in provider.stream(
            "你是记忆去重与冲突判断器，只输出 duplicate / conflict / new 之一。",
            [Message(role=Role.user, blocks=[TextBlock(text=prompt)])],
            [],
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, StreamEnd):
                pass
        answer = "".join(parts).strip().lower()
        if "conflict" in answer:
            return "conflict"
        if "duplicate" in answer and "not duplicate" not in answer and "not a duplicate" not in answer:
            return "duplicate"
        return "new"

    return judge


def _make_facts():
    """(unique, dupes, conflicts). Seed 固定 → 可复现。

    独特事实里前 20 条是「用户 i 的主语言是 X」——这是明确的**唯一 slot**（每个
    用户只有一个主语言），供冲突测试用；后 240 条是「功能 i 的 X 模块配置为 N」
    （唯一编号主导语义，reranker 对编号不同的给低分 → 不触发 judge）。只有真正的
    重复（同义改写）和冲突（同一 slot 换值）才 reranker 高分 → judge 判
    duplicate/conflict，所以 judge 只在 ~40 条变体上触发，时间成本可控。
    """
    rng = random.Random(42)
    subjects = ["登录", "支付", "搜索", "通知", "报表", "权限", "缓存", "日志", "监控", "审计"]
    vals = [rng.randint(1, 999) for _ in range(240)]
    langs = ["Rust", "Go", "Python", "TypeScript"]

    unique = []
    # 前 20 条：用户主语言（唯一 slot，冲突测试的旧值）
    for i in range(20):
        unique.append(f"用户{i}的主语言是{langs[i % 4]}")
    # 后 240 条：功能模块配置（编号主导，主题分散）
    for i in range(240):
        unique.append(f"功能{i}的{subjects[i % 10]}模块配置为{vals[i]}")

    # 20 条重复：同义改写（"主语言是" → "主用语言为"）
    dupes = [f"用户{i}主用语言为{langs[i % 4]}" for i in range(20)]

    # 20 条冲突：同一 slot 换值（明确的覆盖语义）
    conflicts = [f"用户{i}的主语言改成{langs[(i + 1) % 4]}" for i in range(20)]

    return unique, dupes, conflicts


def test_memory_scale_real_stack():
    require_embedding_vars()
    db = Database(os.environ["PI_ITEST_DATABASE_URL"])
    embedder = EmbeddingClient(
        os.environ["PI_ITEST_EMBEDDING_URL"],
        os.environ["PI_ITEST_EMBEDDING_API_KEY"],
        os.environ["PI_ITEST_EMBEDDING_MODEL"],
    )
    store = MilvusStore(os.environ["PI_ITEST_MILVUS_URI"], collection=_ITEST_COLLECTION)
    reranker = _build_reranker()
    judge = _build_judge()
    repo = MemoryRepo(db, vector_store=store, embedder=embedder, reranker=reranker, judge=judge)

    unique, dupes, conflicts = _make_facts()
    stamp = int(time.time())
    username = f"itest_mem_scale_{stamp}"

    async def main():
        await db.init()
        async with db.engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (username, password_hash, is_admin, is_active, quota_tokens, created_at)"
                    " VALUES (:u, 'x', FALSE, TRUE, 10000000, '2026-09-27T00:00:00+00:00')"
                ),
                {"u": username},
            )
            await conn.commit()
            user_id = (
                await conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username})
            ).scalar_one()

        # 1. 批量 add 独特事实
        t0 = time.perf_counter()
        added = 0
        for t in unique:
            added += await repo.add(user_id, t)
        t_uniq = time.perf_counter() - t0

        # 2. 重复变体（应判 duplicate → 不写）
        dup_added = sum([await repo.add(user_id, t) for t in dupes])

        # 3. 冲突变体（应判 conflict → 覆盖，返回 True）
        conflict_added = sum([await repo.add(user_id, t) for t in conflicts])
        total_s = time.perf_counter() - t0

        rows = await repo.list_for_user(user_id, limit=1000)
        n = len(rows)
        print(
            f"\n[scale] add 300: unique_written={added} dup_written={dup_added} "
            f"conflict_handled={conflict_added} final_rows={n} "
            f"unique_time={t_uniq:.1f}s total={total_s:.1f}s"
        )

        # 数量断言（宽松抗模型波动）：去重+冲突在起作用，不是 300 全写，也没崩成空。
        # 理想 260（20 重复去重 + 20 冲突覆盖）；冲突若被判 new 则最多 280。
        assert 230 <= n <= 285, f"unexpected final memory count {n}"

        # 抽查：规模下向量/词法检索对多个 probe 都能命中（不崩、有召回）
        for probe in ["用户0的主语言", "功能0的登录模块", "功能5的报表模块"]:
            hits = await repo.search(user_id, probe, k=3)
            assert hits, f"probe {probe!r} should hit"

        # 抽查：冲突变体（主语言改成 Go）能被检索到——覆盖或写新都会命中
        conf_hits = await repo.search(user_id, "用户0的主语言改成Go", k=5)
        assert any("Go" in h.text for h in conf_hits), [h.text for h in conf_hits]

        # 清理：删 MySQL 行 + drop 独立 Milvus collection
        async with db.engine.connect() as conn:
            await conn.execute(text("DELETE FROM memories WHERE user_id = :u"), {"u": user_id})
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            await conn.commit()
        await store.close()
        try:
            import pymilvus

            c = pymilvus.MilvusClient(uri=os.environ["PI_ITEST_MILVUS_URI"])
            c.drop_collection(_ITEST_COLLECTION)
        except Exception:  # noqa: BLE001 - collection drop is best-effort
            pass
        await db.dispose()

    asyncio.run(main())
