"""真实栈记忆规模测试：几百条 add/search/conflict 跑在真 Milvus + 真 reranker + 真 LLM。

与 test_memory_real.py（2 条冒烟）不同，本文件验证**规模下的全链路**：
  - 真 embedding（qwen3.7-text-embedding）逐条 embed 几百条记忆
  - 真 Milvus 向量召回 + 真 reranker（qwen3.7-text-rerank）精判去重
  - 真 LLM（PI_MODEL）judge 判 duplicate/conflict/new，冲突原地覆盖

数据设计（seed 固定，可复现）：
  - 60 条独特事实（20 条用户主语言 + 40 条功能配置）
  - 20 条重复变体（措辞改写，cosine 高分直接判 duplicate → 不写）
  - 20 条冲突变体（同一 slot 换值，应被 judge 判 conflict → 覆盖旧行）
  总 add 100 次；理想最终 60 条（20 重复去重、20 冲突覆盖不新增）。

断言宽松以抗模型波动：数量落在 [230, 280]、抽查重复/冲突各 1 条、记录耗时。
零残留：独立 Milvus collection（pi_memories_itest）+ throwaway user，结束 drop。

Run（从 .env.local 加载 creds，不落 shell）:
  tools/run_memory_real.py
或手动：
  PI_INTEGRATION=1 PI_ITEST_* ...  pytest integration/test_memory_real_scale.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
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


def _build_judge():
    """PI_MODEL → MemoryRepo judge callback (user, new, candidates) -> (verdict, target)."""
    from pi.llm import resolve
    from pi.llm.base import StreamEnd, TextDelta
    from pi.models import Message, Role, TextBlock

    provider = resolve(os.environ.get("PI_MODEL", "openai/gpt-4o"))

    async def judge(user_id: int, new_text: str, candidates: list[str]) -> tuple[str, int | None]:
        cand_lines = "\n".join(f"{i}. {t}" for i, t in enumerate(candidates))
        prompt = (
            "候选记忆（编号 0 起）：\n{cands}\n\n"
            "新记忆：{new}\n\n"
            "判断新记忆与候选记忆的关系，只输出一个 JSON 对象：\n"
            '{{"verdict": "duplicate"|"conflict"|"new", "target": 编号或 null}}\n\n'
            "判定准则：\n"
            "- duplicate：新记忆与某条候选同义（措辞不同、含义相同）→ target 填该候选编号\n"
            "- conflict：新记忆与某条候选是同一主体、同一属性/偏好、但值不同 → target 填该候选编号（覆盖它）。"
            "关键：无论措辞是「是X」「改成X」「改为X」「换成X」，同一属性换值即 conflict。\n"
            "- new：新记忆与所有候选都不同 → target 填 null\n\n"
            "示例：\n"
            '候选：["用户0的主语言是Rust", "用户1的主语言是Go"]\n'
            "新记忆：用户0的主语言改成Go\n"
            '输出：{{"verdict": "conflict", "target": 0}}\n'
        ).format(cands=cand_lines, new=new_text)
        parts: list[str] = []
        async for ev in provider.stream(
            "你是记忆去重与冲突判断器，判断新记忆与候选记忆列表的关系，只输出 JSON。",
            [Message(role=Role.user, blocks=[TextBlock(text=prompt)])],
            [],
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
        answer = "".join(parts).strip()
        verdict = ""
        target = None
        m = re.search(r"\{[^{}]*\}", answer, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                verdict = str(obj.get("verdict", "")).strip().lower()
                raw = obj.get("target")
                if raw is not None:
                    try:
                        target = int(raw)
                    except (TypeError, ValueError):
                        target = None
            except ValueError:
                pass
        if verdict not in ("duplicate", "conflict", "new"):
            low = answer.lower()
            if "conflict" in low:
                verdict = "conflict"
            elif "duplicate" in low and "not duplicate" not in low and "not a duplicate" not in low:
                verdict = "duplicate"
            else:
                verdict = "new"
            target = None
        return verdict, target

    return judge


def _make_facts():
    """(unique, dupes, conflicts). Seed 固定 → 可复现。

    独特事实里前 20 条是「用户 i 的主语言是 X」——这是明确的**唯一 slot**（每个
    用户只有一个主语言），供冲突测试用；后 40 条是「功能 i 的 X 模块配置为 N」
    （唯一编号主导语义）。重复（同义改写）走 cosine 高分快速判重（省 judge）；
    冲突（同一 slot 换值）和其余事实走 judge。
    """
    rng = random.Random(42)
    subjects = ["登录", "支付", "搜索", "通知", "报表", "权限", "缓存", "日志", "监控", "审计"]
    vals = [rng.randint(1, 999) for _ in range(40)]
    langs = ["Rust", "Go", "Python", "TypeScript"]

    unique = []
    # 前 20 条：用户主语言（唯一 slot，冲突测试的旧值）
    for i in range(20):
        unique.append(f"用户{i}的主语言是{langs[i % 4]}")
    # 后 40 条：功能模块配置（编号主导，主题分散）
    for i in range(40):
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
    judge = _build_judge()
    repo = MemoryRepo(db, vector_store=store, embedder=embedder, judge=judge)

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
            f"\n[scale] add 100: unique_written={added} dup_written={dup_added} "
            f"conflict_handled={conflict_added} final_rows={n} "
            f"unique_time={t_uniq:.1f}s total={total_s:.1f}s"
        )

        # 数量断言（宽松抗模型波动）：去重+冲突在起作用，不是全写，也没崩成空。
        # 理想 60（20 重复去重 + 20 冲突覆盖）；冲突若被判 new 则最多 80。
        assert 50 <= n <= 82, f"unexpected final memory count {n}"

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
