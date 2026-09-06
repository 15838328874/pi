"""Memory write/read/arbitrate paths against the live endpoints.

Everything here costs real tokens: extraction (qwen-flash), embeddings
(qwen3.7-text-embedding), rerank (qwen3.7-text-rerank) and arbitration
(qwen-plus) all hit the real gateway, and the store is the real Milvus
collection it_memories. Assertions are therefore scoped to what the measured
gateway behaviour guarantees - fact counts land in ranges, not exact lists,
because every chat model at this gateway drifts run to run.

The store/embedder/reranker stack is session-scoped, so tests call drain()
rather than close(): close() would shut the shared Milvus thread pool down
for every later test. The session fixture closes it once, at teardown.
"""

from __future__ import annotations

import asyncio
import os

from conftest import T_GOOD, FixedProvider


def test_extraction_stores_facts_and_a_repeat_reconfirms_them(service_factory, run):
    svc, meter = service_factory()
    uid = 2101

    async def main():
        await svc.setup()
        first = await svc.record(
            user_id=uid, username="it", session_id="r1",
            messages=T_GOOD, fallback_model=os.environ["PI_MEMORY_MODEL"],
        )
        facts1 = await svc.list_for_user(uid)
        await asyncio.sleep(1.2)  # created_at/last_seen_at have second granularity
        # Re-emit exactly the stored texts: cosine 1.0 against their own vectors,
        # so every fact must be re-confirmed (touched) rather than duplicated.
        await svc.record(
            user_id=uid, username="it", session_id="r2",
            messages=T_GOOD, fallback_model=os.environ["PI_MEMORY_MODEL"],
            provider=FixedProvider([f.text for f in facts1]),
        )
        facts2 = await svc.list_for_user(uid)
        await svc.drain()
        return first, facts1, facts2

    usage, facts1, facts2 = run(main())
    assert 3 <= len(facts1) <= 5, f"extraction stored {len(facts1)} facts"
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert meter.calls, "extraction spend must reach the meter"
    assert len(facts2) == len(facts1), "re-confirmation must not add rows"
    seen_before = {f.text: f.last_seen_at for f in facts1}
    created_before = {f.text: f.created_at for f in facts1}
    for f in facts2:
        assert f.last_seen_at > seen_before[f.text]
        assert f.created_at == created_before[f.text]


def test_retrieval_injects_stored_facts(service_factory, run):
    svc, _ = service_factory()
    uid = 2102

    async def main():
        await svc.setup()
        await svc.record(
            user_id=uid, username="it", session_id="r1",
            messages=T_GOOD, fallback_model=os.environ["PI_MEMORY_MODEL"],
        )
        text, usage = await svc.retrieve(uid, "这个项目用什么工具管理 Python 依赖？")
        await svc.drain()
        return text, usage

    text, usage = run(main())
    assert "uv" in text
    assert usage.input_tokens > 0, "embedding spend must be metered"


def test_arbitration_merges_a_real_contradiction(service_factory, run):
    svc, meter = service_factory()
    uid = 2103
    arbiter = os.environ["PI_MEMORY_ARBITER_MODEL"]
    assert arbiter, "PI_MEMORY_ARBITER_MODEL must be set for this tier"

    async def main():
        from pi.memory.repo import MemoryRowIn

        await svc.setup()
        embedder = svc._embedder
        # Seed through the repo-first protocol the service itself uses: MySQL row
        # first, index upsert as the mirror. Two contradictory statements, one
        # second apart so their created_at (second granularity) differ - the
        # merge must then inherit the older row's timestamp, not the newer's.
        old_text, new_text = "项目统一用 uv 管理 Python 依赖", "项目后来改用 pip 管理依赖了，不再用 uv"
        vecs, _ = await embedder.embed([old_text, new_text])
        (fid_old,) = await svc._repo.insert_many(
            uid, [MemoryRowIn(text=old_text, kind="convention",
                              source_session="r1", embedding=list(vecs[0]))]
        )
        await asyncio.sleep(1.2)
        (fid_new,) = await svc._repo.insert_many(
            uid, [MemoryRowIn(text=new_text, kind="convention",
                              source_session="r2", embedding=list(vecs[1]))]
        )
        await svc._store.upsert(uid, [(fid_old, vecs[0]), (fid_new, vecs[1])])
        before = await svc.list_for_user(uid)
        oldest = min(f.created_at for f in before)

        usage = await svc.arbitrate(uid, "it")  # real qwen-plus call
        after = await svc.list_for_user(uid)
        return before, after, oldest, usage

    before, after, oldest, usage = run(main())
    assert len(before) == 2
    assert usage.input_tokens > 0, "arbitration spend must be metered"
    assert [c for c in meter.calls if c["model"] == arbiter and c["session_id"] == "arbiter"]
    assert len(after) == 1, f"qwen-plus left {len(after)} facts instead of a merged one"
    # The newer statement wins per the prompt, and provenance survives the merge.
    assert "pip" in after[0].text, f"merged text lost the newer statement: {after[0].text!r}"
    assert after[0].created_at == oldest
    assert after[0].source_session == "arbiter"
