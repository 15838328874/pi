"""Long-term memory: tenant isolation, extraction, injection and degradation.

Runs with no Milvus, no API key and no network: DictMemoryRepo + InMemoryStore for
the truth/index pair, HashEmbedder for vectors, FakeProvider for the extraction
model. MilvusStore is exercised through a stub client, which is what pins the
filter expressions - the one part of this feature where a bug is a cross-tenant
read rather than a wrong answer. UserMemoryRepo (the SQL repo) runs against
SQLite. The rerank stage is exercised through httpx.MockTransport, which pins the
response parsing without billing a call against the live endpoint.

The MySQL-source-of-truth invariants this file exists to pin:
* every write lands in the repo before the index is asked to mirror it;
* a failed mirror write leaves the row pending, and sync_pending heals it;
* an index hit whose repo row is gone (zombie) never reaches a prompt;
* an unreachable index degrades to a repo scan instead of amnesia.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from conftest import StrictFakeProvider

from pi.agent.events import ErrorEvent, TurnEndEvent
from pi.agent.loop import AgentLoop
from pi.llm.fake import FakeProvider
from pi.memory import (
    DashScopeReranker,
    DictMemoryRepo,
    Fact,
    HashEmbedder,
    InMemoryStore,
    MemoryRowIn,
    MemoryService,
    MilvusStore,
    NoOpStore,
    get_embedder,
    get_reranker,
    get_store,
    parse_facts,
)
from pi.memory.service import MEMORY_FOOTER, MEMORY_HEADER
from pi.memory.store import _user_filter
from pi.models import Message, Role, TextBlock, ToolCallBlock, Usage
from pi.tools import all_tools

SECRET = "sk-abcdefghijklmnopqrst1234"
ONE_FACT = '[{"text":"用户偏好 uv 而非 pip","kind":"preference"}]'


def _service(**over: Any) -> MemoryService:
    """A working in-process service.

    min_similarity 0 because hash vectors are not real embeddings: two Chinese
    sentences about the same thing may share no tokens at all.
    """
    kwargs: dict[str, Any] = dict(
        store=InMemoryStore(),
        embedder=HashEmbedder(dim=64),
        memory_model="fake/mem",
        min_similarity=0.0,
        extract_min_chars=0,
    )
    kwargs.update(over)
    return MemoryService(**kwargs)


def _transcript(text: str = "我习惯用 uv 管理依赖，别用 pip") -> list[Message]:
    return [
        Message(role=Role.user, blocks=[TextBlock(text=text)]),
        Message(role=Role.assistant, blocks=[TextBlock(text="好的，已记录")]),
    ]


def _extractor(payload: str) -> FakeProvider:
    """An extraction model that answers with exactly this JSON."""
    return FakeProvider(responses=[[TextBlock(text=payload)]])


class RecordingEmbedder(HashEmbedder):
    """HashEmbedder that logs every text handed to the endpoint.

    Used by the tests that assert certain inputs must never leave the process: the
    live embedding endpoint returns a different vector width for the empty string,
    so what reaches it matters, not just what comes back.
    """

    def __init__(self, dim: int = 64) -> None:
        super().__init__(dim=dim)
        self.seen: list[str] = []

    async def embed(self, texts):
        self.seen.extend(texts)
        return await super().embed(texts)


async def _learn(svc: MemoryService, payload: str = ONE_FACT, user_id: int = 7) -> Usage:
    return await svc.record(
        user_id=user_id,
        username="alice",
        session_id="s1",
        messages=_transcript(),
        fallback_model="fake/demo",
        provider=_extractor(payload),
    )


async def _seed_direct(
    svc: MemoryService,
    texts: Sequence[str],
    user_id: int = 7,
    kind: str = "fact",
    advance=None,
) -> list[int]:
    """Write facts straight into the repo (and mirror them), no extraction model.

    For tests that need a known store state: the arbitration seeds and the budget
    test want specific rows, not whatever the fake model happens to say. One insert
    per text so a frozen clock can separate their timestamps.
    """
    ids: list[int] = []
    for i, text in enumerate(texts):
        if advance and i > 0:
            advance()
        vecs, _ = await svc._embedder.embed([text])
        (fid,) = await svc._repo.insert_many(
            user_id, [MemoryRowIn(text=text, kind=kind, source_session="s1", embedding=list(vecs[0]))]
        )
        await svc._store.upsert(user_id, [(fid, vecs[0])])
        await svc._repo.mark_synced([fid])
        ids.append(fid)
    return ids


def _clock(monkeypatch, start: str = "2026-01-01T00:00:00+00:00"):
    """Freeze repo.now_iso; the returned advance() moves it a minute at a time.

    created_at and last_seen_at have second granularity, so two records inside one
    real second are indistinguishable - exactly what the refresh and eviction tests
    need to tell apart.
    """
    state = {"now": datetime.fromisoformat(start)}

    def fake_now() -> str:
        return state["now"].isoformat(timespec="seconds")

    def advance(minutes: int = 1) -> None:
        state["now"] += timedelta(minutes=minutes)

    monkeypatch.setattr("pi.memory.repo.now_iso", fake_now)
    return advance


class _Recording(StrictFakeProvider):
    """StrictFakeProvider that also keeps every (system, messages) pair it saw.

    Strict rather than plain because the pairing rule is what makes the injection
    tests meaningful: a memory message spliced in anywhere but index 0 breaks it
    here exactly as it breaks a real provider.
    """

    def __init__(self, responses: list[list[object]] | None = None):
        super().__init__(responses=responses)
        self.seen: list[tuple[str, list[Message]]] = []

    async def stream(self, system, messages, tools):
        self.seen.append((system, list(messages)))
        async for ev in super().stream(system, messages, tools):
            yield ev


class _StubClient:
    """Stands in for pymilvus's MilvusClient and records what it was asked for."""

    def __init__(self, **results: Any):
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.arglists: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            self.arglists.append((name, args))
            return self.results.get(name, {})

        return call

    def filters(self) -> list[str]:
        return [kw["filter"] for _, kw in self.calls if "filter" in kw]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def rows(self) -> list[dict[str, Any]]:
        """Insert payloads in order: what actually got written."""
        return [args[1][0] for name, args in self.arglists if name == "insert"]


class _Spy:
    """Stands in for the schema and index-params builders create_collection wants."""

    def __init__(self) -> None:
        self.fields: list[str] = []
        self.indexes: list[dict[str, Any]] = []
        self.partition_key = ""

    def add_field(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.fields.append(name)
        if kwargs.get("is_partition_key"):
            self.partition_key = name

    def add_index(self, **kwargs: Any) -> None:
        self.indexes.append(kwargs)


class _DataType:
    """Stands in for pymilvus's DataType enum; only the names are used."""

    INT64 = "INT64"
    VARCHAR = "VARCHAR"
    FLOAT_VECTOR = "FLOAT_VECTOR"


class _StubMilvus(MilvusStore):
    """MilvusStore with the client and the thread pool replaced.

    Its own __init__, so the real one - which imports pymilvus and opens a
    connection - never runs.
    """

    def __init__(self, client: _StubClient, dim: int = 8):
        self._DataType = _DataType
        self._client = client
        self._collection = "pi_memories"
        self._dim = dim
        self._num_partitions = 1024
        self._uri = "stub://test"

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)


class TestTenantFilter:
    def test_the_filter_is_an_integer_comparison(self):
        assert _user_filter(7) == "user_id == 7"

    def test_a_non_integer_cannot_reach_the_filter(self):
        """The int() is the isolation guarantee, so it has to fail loudly.

        A string that survived into the expression would be an injection hole:
        '1 or user_id == 2' reads somebody else's facts.
        """
        with pytest.raises((TypeError, ValueError)):
            _user_filter("1 or user_id == 2")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            _user_filter(None)  # type: ignore[arg-type]

    def test_a_numeric_string_is_coerced_not_interpolated(self):
        assert _user_filter("7") == "user_id == 7"


class TestMilvusQueries:
    """The stub pins exactly what reaches Milvus: filters, payloads, schema."""

    def test_every_query_is_scoped_to_one_user(self):
        store = _StubMilvus(_StubClient())

        async def main():
            await store.search(7, [0.1, 0.2], 5)
            await store.delete(7, [3])
            await store.delete_user(7)

        asyncio.run(main())
        filters = store._client.filters()
        assert len(filters) == 3
        assert all(f.startswith("user_id == 7") for f in filters), filters

    def test_delete_carries_both_predicates(self):
        """Deleting by id alone would let a crafted id remove another tenant's fact."""
        store = _StubMilvus(_StubClient(delete={"delete_count": 1}))

        async def main():
            assert await store.delete(7, [3]) == 1

        asyncio.run(main())
        assert store._client.filters() == ["user_id == 7 and id in [3]"]

    def test_a_batched_delete_lists_every_id_int_coerced(self):
        """One call per eviction batch, and nothing non-numeric can reach the
        expression - the same injection hole _user_filter closes for user_id."""
        store = _StubMilvus(_StubClient(delete={"delete_count": 3}))

        async def main():
            assert await store.delete(7, ["3", 5, 7]) == 3

        asyncio.run(main())
        assert store._client.filters() == ["user_id == 7 and id in [3, 5, 7]"]

    def test_retrieval_searches_stay_on_the_cheap_stale_read(self):
        """Bounded, always: retrieval is the per-turn hot path, and freshness now
        comes from the repo join rather than from the index read. Session and its
        ~370ms sync cost were only ever needed for read-your-writes dedup, which
        reads the repo now."""
        store = _StubMilvus(_StubClient())

        async def main():
            await store.search(7, [0.1], 5)

        asyncio.run(main())
        levels = [kw["consistency_level"] for n, kw in store._client.calls if n == "search"]
        assert levels == ["Bounded"]

    def test_upsert_writes_id_user_id_vector_and_nothing_else(self):
        """The index is a pure accelerator: no text, no provenance, no timestamps.
        The id is the MySQL row id - the primary key that makes re-sends replace
        rather than duplicate."""
        client = _StubClient(upsert={})
        store = _StubMilvus(client)

        async def main():
            assert await store.upsert(7, [(9, [0.1, 0.2]), (12, [0.3, 0.4])]) is True

        asyncio.run(main())
        name, kw = store._client.calls[0]
        _, args = store._client.arglists[0]
        assert name == "upsert"
        assert args[0] == "pi_memories"
        assert args[1] == [
            {"id": 9, "user_id": 7, "vector": [0.1, 0.2]},
            {"id": 12, "user_id": 7, "vector": [0.3, 0.4]},
        ]
        assert not kw  # no flush, no consistency level: nothing but the rows

    def test_search_returns_id_score_pairs(self):
        client = _StubClient(search=[[{"id": 4, "distance": 0.9}, {"id": 2, "distance": 0.5}]])
        store = _StubMilvus(client)

        async def main():
            return await store.search(7, [0.1], 5)

        assert asyncio.run(main()) == [(4, 0.9), (2, 0.5)]

    def test_setup_refuses_a_collection_whose_dim_disagrees(self):
        """Silently rebuilding would delete every user's memory."""
        client = _StubClient(
            has_collection=True,
            describe_collection={"fields": [{"name": "vector", "params": {"dim": 768}}]},
        )
        with pytest.raises(RuntimeError, match="dim=768"):
            asyncio.run(_StubMilvus(client, dim=512).setup())

    def test_setup_refuses_a_collection_from_before_deterministic_ids(self):
        """auto_id collections predate the MySQL source-of-truth design; upserts
        against them cannot carry the row id, so the operator must rebuild."""
        client = _StubClient(
            has_collection=True,
            describe_collection={"fields": [{"name": "id", "auto_id": True}]},
        )
        with pytest.raises(RuntimeError, match="auto_id"):
            asyncio.run(_StubMilvus(client).setup())

    def test_setup_accepts_a_matching_collection(self):
        client = _StubClient(
            has_collection=True,
            describe_collection={"fields": [{"name": "id", "auto_id": False}, {"name": "vector", "params": {"dim": 512}}]},
        )
        asyncio.run(_StubMilvus(client, dim=512).setup())
        assert client.names() == ["has_collection", "describe_collection"]

    def test_a_new_collection_hashes_user_id_into_buckets(self):
        """At 1M users a search must prune to one bucket, not scan everything.

        A partition per user is far over Milvus's partition limit, and no partition
        key at all means every query filters the whole collection.
        """
        spy = _Spy()
        client = _StubClient(
            has_collection=False, create_schema=spy, prepare_index_params=spy
        )
        asyncio.run(_StubMilvus(client, dim=512).setup())

        assert spy.partition_key == "user_id"
        # The full field list, pinned: the index stores nothing but the address
        # space (id, user_id) and the vector - everything else lives in MySQL.
        assert spy.fields == ["id", "user_id", "vector"]
        created = [kw for name, kw in client.calls if name == "create_collection"]
        assert created[0]["num_partitions"] == 1024
        assert spy.indexes == [
            {"field_name": "vector", "index_type": "AUTOINDEX", "metric_type": "COSINE"}
        ]

    def test_the_schema_is_created_without_auto_id(self):
        spy = _Spy()
        client = _StubClient(
            has_collection=False, create_schema=spy, prepare_index_params=spy
        )
        asyncio.run(_StubMilvus(client, dim=512).setup())
        schema_calls = [kw for n, kw in client.calls if n == "create_schema"]
        assert schema_calls == [{"auto_id": False, "enable_dynamic_field": False}]


class TestStoreIsolation:
    def test_search_never_returns_another_users_fact(self):
        store = InMemoryStore()

        async def main():
            await store.upsert(1, [(10, [1.0, 0.0])])
            await store.upsert(2, [(11, [1.0, 0.0])])
            return await store.search(1, [1.0, 0.0], 10)

        assert [fid for fid, _ in asyncio.run(main())] == [10]

    def test_delete_needs_both_keys(self):
        store = InMemoryStore()

        async def main():
            await store.upsert(1, [(1, [1.0])])
            assert await store.delete(2, [1]) == 0  # bob cannot delete alice's fact
            assert await store.search(1, [1.0], 10)
            assert await store.delete(1, [1]) == 1
            assert await store.search(1, [1.0], 10) == []

        asyncio.run(main())

    def test_delete_user_only_touches_one_user(self):
        store = InMemoryStore()

        async def main():
            await store.upsert(1, [(1, [1.0]), (2, [1.0])])
            await store.upsert(2, [(3, [1.0])])
            return await store.delete_user(1), await store.search(2, [1.0], 10)

        count, remaining = asyncio.run(main())
        assert count == 2
        assert [fid for fid, _ in remaining] == [3]


class TestParseFacts:
    def test_survives_code_fences_and_prose(self):
        raw = 'Sure! Here you go:\n```json\n[{"text":"用 uv 管理依赖","kind":"preference"}]\n```'
        assert [f.text for f in parse_facts(raw)] == ["用 uv 管理依赖"]

    @pytest.mark.parametrize(
        "raw",
        ["", "not json at all", "[]", "{}", "[null]", '[{"text":123}]', '[{"text":"ab"}]'],
    )
    def test_bad_output_yields_nothing_rather_than_raising(self, raw):
        assert parse_facts(raw) == []

    def test_unknown_kind_falls_back_to_fact(self):
        assert parse_facts('[{"text":"something durable","kind":"banana"}]')[0].kind == "fact"

    def test_dedupes_within_one_extraction(self):
        raw = '[{"text":"用 uv","kind":"fact"},{"text":"用 UV","kind":"fact"}]'
        assert len(parse_facts(raw)) == 1

    def test_caps_the_number_of_facts(self):
        raw = "[" + ",".join('{"text":"fact number %d"}' % i for i in range(40)) + "]"
        assert len(parse_facts(raw)) == 5

    def test_a_long_fact_is_trimmed_to_the_cap(self):
        got = parse_facts('[{"text":"' + "x" * 500 + '"}]')
        assert len(got) == 1 and len(got[0].text) == 200

    def test_a_paragraph_is_dropped_rather_than_cut_mid_sentence(self):
        """Past 4x the cap this is not a fact, it is the model ignoring instructions.

        Truncating it would store a fragment whose meaning ended before the cut.
        """
        assert parse_facts('[{"text":"' + "x" * 5000 + '"}]') == []


class TestTouch:
    """The re-confirmation write, now a single repo UPDATE."""

    def test_cannot_resurrect_a_deleted_fact(self):
        """The stale fact a background task holds must not undo the user's delete.

        The touch runs after an HTTP delete may already have landed; the UPDATE
        matches zero rows, returns False, and the caller skips the index upsert -
        without that rowcount guard the refresh would re-create the fact.
        """
        repo = DictMemoryRepo()

        async def main():
            (fid,) = await repo.insert_many(
                7, [MemoryRowIn(text="用户偏好 uv", kind="preference", source_session="s1", embedding=[0.1])]
            )
            await repo.delete(7, fid)
            ok = await repo.touch(7, fid, [0.1])
            return ok, await repo.get_active(7)

        ok, facts = asyncio.run(main())
        assert ok is False
        assert facts == []

    def test_refreshes_last_seen_and_keeps_everything_else(self, monkeypatch):
        advance = _clock(monkeypatch)
        repo = DictMemoryRepo()

        async def main():
            (fid,) = await repo.insert_many(
                7, [MemoryRowIn(text="用户偏好 uv", kind="preference", source_session="s1", embedding=[0.1])]
            )
            before, _ = (await repo.get_active(7))[0]
            advance()
            ok = await repo.touch(7, fid, [0.2])
            after, vec = (await repo.get_active(7))[0]
            return ok, before, after, vec

        ok, before, after, vec = asyncio.run(main())
        assert ok is True
        assert after.id == before.id  # in place: the id, and the index entry, are stable
        assert after.created_at == before.created_at == "2026-01-01T00:00:00+00:00"
        assert after.last_seen_at == "2026-01-01T00:01:00+00:00"
        assert (after.text, after.kind, after.source_session) == (
            before.text, before.kind, before.source_session
        )
        assert vec == [0.2]  # the confirming candidate's vector replaces the old one

    def test_a_touched_row_is_pending_until_the_index_sees_it(self):
        repo = DictMemoryRepo()

        async def main():
            ids = await repo.insert_many(
                7, [MemoryRowIn(text="a", kind="fact", source_session="s", embedding=[0.1])]
            )
            await repo.mark_synced(ids)
            assert await repo.pending_sync(10) == []
            assert await repo.touch(7, ids[0], [0.5]) is True
            return await repo.pending_sync(10)

        pending = asyncio.run(main())
        assert [f.id for f, _ in pending] and pending[0][1] == [0.5]


class TestRecord:
    def test_stores_extracted_facts_and_returns_usage(self):
        svc = _service()

        async def main():
            await svc.setup()
            usage = await _learn(svc)
            facts = await svc.list_for_user(7)
            await svc.close()
            return usage, facts

        usage, facts = asyncio.run(main())
        assert [(f.kind, f.text, f.source_session) for f in facts] == [
            ("preference", "用户偏好 uv 而非 pip", "s1")
        ]
        assert usage.input_tokens > 0  # extraction is real spend

    def test_unparseable_model_output_stores_nothing(self):
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc, "I cannot help with that.")
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        assert asyncio.run(main()) == []

    def test_a_near_duplicate_is_not_stored_twice(self):
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)
            await svc.record(
                user_id=7, username="alice", session_id="s2",
                messages=_transcript(), fallback_model="fake/demo", provider=_extractor(ONE_FACT),
            )
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        assert len(asyncio.run(main())) == 1

    def test_the_per_user_cap_evicts_the_oldest(self):
        svc = _service(max_facts=3)
        five = "[" + ",".join('{"text":"事实 %d 号"}' % i for i in range(5)) + "]"

        async def main():
            await svc.setup()
            await _learn(svc, five)
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        assert [f.text for f in asyncio.run(main())] == ["事实 2 号", "事实 3 号", "事实 4 号"]

    def test_reconfirming_a_fact_refreshes_it_without_duplicating(self, monkeypatch):
        """The dedup hit is not just a skip: it is the eviction signal.

        A fact restated across sessions is the durable kind. Leaving its timestamps
        at the original insert would keep it at the front of the eviction queue no
        matter how often it comes back.
        """
        advance = _clock(monkeypatch)
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)  # t0: stored
            advance()
            await _learn(svc)  # t+1min: same payload -> dedup hit -> touch
            await svc.close()
            return await svc.list_for_user(7)

        facts = asyncio.run(main())
        assert len(facts) == 1
        assert facts[0].created_at == "2026-01-01T00:00:00+00:00"
        assert facts[0].last_seen_at == "2026-01-01T00:01:00+00:00"

    def test_eviction_drops_the_unconfirmed_not_the_oldest(self, monkeypatch):
        """Oldest-first evicted exactly backwards: the oldest row is often the one
        re-confirmed most often. last_seen_at is what separates the two."""
        advance = _clock(monkeypatch)
        svc = _service(max_facts=2)
        B = '[{"text":"数据库是 MySQL 8","kind":"environment"}]'
        C = '[{"text":"前端是 Vue3 不用 React","kind":"convention"}]'

        async def main():
            await svc.setup()
            await _learn(svc)  # A, t0
            advance()
            await _learn(svc, B)  # B, t1
            advance()
            await _learn(svc)  # A re-confirmed, t2 - only a touch, no new row
            advance()
            await _learn(svc, C)  # C, t3: over the cap, one must go
            await svc.close()
            return [f.text for f in await svc.list_for_user(7)]

        # B (unconfirmed since t1) goes; A survives despite being the oldest
        assert asyncio.run(main()) == ["用户偏好 uv 而非 pip", "前端是 Vue3 不用 React"]

    def test_a_failing_touch_does_not_abort_the_batch(self):
        class BadTouch(DictMemoryRepo):
            async def touch(self, user_id, fact_id, embedding):
                raise RuntimeError("mysql went away mid-update")

        svc = _service(repo=BadTouch())
        two = (
            '[{"text":"用户偏好 uv 而非 pip","kind":"preference"},'
            '{"text":"数据库是 MySQL 8","kind":"environment"}]'
        )

        async def main():
            await svc.setup()
            await _learn(svc)
            await svc.record(
                user_id=7, username="alice", session_id="s2",
                messages=_transcript(), fallback_model="fake/demo",
                provider=_extractor(two),
            )
            await svc.close()
            return [f.text for f in await svc.list_for_user(7)]

        # The first candidate's touch died; the second still landed
        assert asyncio.run(main()) == ["用户偏好 uv 而非 pip", "数据库是 MySQL 8"]

    def test_eviction_is_enforced_by_the_repo_not_the_index(self):
        """The cap is a property of the truth, not of the accelerator.

        A Milvus that cannot delete used to mean "no eviction at all" when the store
        was the truth; now it means a zombie vector, which the read path filters.
        The repo must come out at the cap with the run unaffected either way.
        """
        class Sticky(InMemoryStore):
            async def delete(self, user_id, fact_ids):
                return 0

        svc = _service(store=Sticky(), max_facts=1)
        two = '[{"text":"用户偏好 uv 而非 pip"},{"text":"数据库是 MySQL 8"}]'

        async def main():
            await svc.setup()
            usage = await _learn(svc, two)  # must not raise
            await svc.close()
            return [f.text for f in await svc.list_for_user(7)], usage

        texts, usage = asyncio.run(main())
        assert texts == ["数据库是 MySQL 8"]  # cap enforced; the zombie stays in the index
        assert usage.input_tokens > 0

    def test_paraphrases_within_one_extraction_collapse(self):
        """parse_facts only drops exact repeats, and the dedup search runs against
        what was stored *before* this batch - so both phrasings of one fact land
        unless the batch checks itself."""
        svc = _service()
        pair = (
            '[{"text":"用户 喜欢 用 uv 管理 这个 项目的 所有 python 依赖 每次 安装 都 很 快",'
            '"kind":"preference"},'
            '{"text":"用户 喜欢 用 uv 管理 这个 项目的 所有 python 依赖 每次 安装 都 很",'
            '"kind":"preference"}]'
        )

        async def main():
            await svc.setup()
            await _learn(svc, pair)
            await svc.close()
            return [f.text for f in await svc.list_for_user(7)]

        assert asyncio.run(main()) == [
            "用户 喜欢 用 uv 管理 这个 项目的 所有 python 依赖 每次 安装 都 很 快"
        ]

    def test_only_dedup_reads_the_repo_retrieval_reads_the_index(self):
        """Dedup needs fresh reads; retrieval is the per-turn hot path.

        With the store as the truth this was a consistency-level trade-off (Session
        for dedup, Bounded for retrieval). With the repo as the truth it is a
        division of labour: dedup's freshness is a plain MySQL read that never
        touches the index, and the index is consulted only by retrieval. A single
        search in the log after both operations is what that asymmetry looks like.
        """

        class RecordingStore(InMemoryStore):
            def __init__(self) -> None:
                super().__init__()
                self.searches = 0

            async def search(self, user_id, vector, limit):
                self.searches += 1
                return await super().search(user_id, vector, limit)

        class RecordingRepo(DictMemoryRepo):
            def __init__(self) -> None:
                super().__init__()
                self.reads = 0

            async def get_active(self, user_id, limit=500):
                self.reads += 1
                return await super().get_active(user_id, limit)

        store = RecordingStore()
        repo = RecordingRepo()
        svc = _service(store=store, repo=repo, min_similarity=0.0)

        async def main():
            await svc.setup()
            await _learn(svc)
            await svc.retrieve(7, "uv")
            await svc.close()

        asyncio.run(main())
        assert store.searches == 1, store.searches  # retrieval only
        assert repo.reads >= 1, repo.reads  # dedup's fresh read

    def test_a_secret_in_the_transcript_never_reaches_the_store(self):
        """Both hops are outbound: the extraction endpoint and the vector database."""
        svc = _service()
        provider = _Recording(responses=[[TextBlock(text='[{"text":"api key is %s"}]' % SECRET)]])

        async def main():
            await svc.setup()
            await svc.record(
                user_id=7, username="alice", session_id="s1",
                messages=_transcript(f"my key is {SECRET}, remember it"),
                fallback_model="fake/demo", provider=provider,
            )
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        facts = asyncio.run(main())
        prompt = provider.seen[0][1][0].blocks[0].text
        assert "sk-" not in prompt  # redacted before it left the process
        assert facts and all("sk-" not in f.text for f in facts)

    def test_a_secret_in_the_audit_preview_is_redacted_too(self, tmp_path):
        from pi.security.audit import AuditLogger

        path = tmp_path / "audit.jsonl"
        svc = _service(audit=AuditLogger(path))

        async def main():
            await svc.setup()
            await svc.record(
                user_id=7, username="alice", session_id="s1",
                messages=_transcript(), fallback_model="fake/demo",
                provider=_extractor('[{"text":"key %s"}]' % SECRET),
            )
            await svc.close()

        asyncio.run(main())
        # AuditLogger rotates daily, so the configured path is not the written one.
        written = list(tmp_path.glob("audit-*.jsonl"))
        assert written, "no audit record was written"
        assert "sk-" not in written[0].read_text(encoding="utf-8")

    def test_a_failing_meter_does_not_break_the_run(self):
        async def broken_meter(**kwargs: Any) -> None:
            raise RuntimeError("database gone")

        svc = _service(meter=broken_meter)

        async def main():
            await svc.setup()
            usage = await _learn(svc)
            facts = await svc.list_for_user(7)
            await svc.close()
            return usage, facts

        usage, facts = asyncio.run(main())
        assert len(facts) == 1 and usage.input_tokens > 0


class TestMetering:
    def _metered(self) -> tuple[MemoryService, list[dict[str, Any]]]:
        calls: list[dict[str, Any]] = []

        async def meter(**kwargs: Any) -> None:
            calls.append(kwargs)

        return _service(meter=meter), calls

    def test_extraction_spend_reaches_the_meter_with_turns_zero(self):
        """turns=0 is what marks the row as memory overhead rather than a run."""
        svc, calls = self._metered()

        async def main():
            await svc.setup()
            await _learn(svc)
            await svc.close()

        asyncio.run(main())
        assert len(calls) == 1
        assert calls[0]["turns"] == 0
        assert calls[0]["user_id"] == 7 and calls[0]["username"] == "alice"
        assert calls[0]["model"] == "fake/mem"
        assert calls[0]["input_tokens"] > 0

    def test_a_run_that_yields_no_facts_is_still_metered(self):
        """The common outcome. Not reporting it would hide most of the cost."""
        svc, calls = self._metered()

        async def main():
            await svc.setup()
            await _learn(svc, "nothing worth remembering here")
            await svc.close()

        asyncio.run(main())
        assert len(calls) == 1
        assert calls[0]["input_tokens"] > 0
        assert calls[0]["output_tokens"] > 0

    def test_no_metering_row_when_memory_is_off(self):
        svc, calls = self._metered()

        async def main():
            await svc.close()  # never set up
            await _learn(svc)

        asyncio.run(main())
        assert calls == []


class TestArbitration:
    MERGE = json.dumps(
        [{"action": "merge", "ids": [1, 2],
          "text": "后来改用 pip 管理依赖了", "kind": "preference"}],
        ensure_ascii=False,
    )

    def _metered(self, **over: Any) -> tuple[MemoryService, list[dict[str, Any]], list[dict[str, Any]]]:
        calls: list[dict[str, Any]] = []

        async def meter(**kwargs: Any) -> None:
            calls.append(kwargs)

        audited: list[dict[str, Any]] = []

        class _Audit:
            def memory(self, **kwargs: Any) -> None:
                audited.append(kwargs)

        kwargs = dict(store=InMemoryStore(), embedder=HashEmbedder(dim=64),
                      memory_model="fake/mem", min_similarity=0.0, arbiter_model="fake/plus",
                      meter=meter, audit=_Audit())
        kwargs.update(over)
        return MemoryService(**kwargs), calls, audited

    async def _seed(self, svc: MemoryService, texts: list[str], advance=None) -> None:
        await _seed_direct(svc, texts, user_id=7, advance=advance)

    def test_merging_keeps_the_oldest_provenance_and_meters_the_plus_call(self, monkeypatch):
        advance = _clock(monkeypatch)
        svc, calls, audited = self._metered()

        async def main():
            await svc.setup()
            await self._seed(svc, ["项目用 uv 管理依赖", "项目改用 pip 管理依赖了"], advance)
            advance()
            usage = await svc.arbitrate(7, "alice", provider=_extractor(self.MERGE))
            facts = await svc.list_for_user(7)
            await svc.close()
            return usage, facts

        usage, facts = asyncio.run(main())
        assert usage.input_tokens > 0
        assert [f.text for f in facts] == ["后来改用 pip 管理依赖了"]
        assert facts[0].created_at == "2026-01-01T00:00:00+00:00"  # the oldest constituent
        assert facts[0].last_seen_at == "2026-01-01T00:02:00+00:00"  # t0, t1 inserts, then t2
        assert facts[0].source_session == "arbiter"
        assert len(calls) == 1
        assert calls[0]["model"] == "fake/plus"
        assert calls[0]["session_id"] == "arbiter"
        assert calls[0]["turns"] == 0
        assert [a["action"] for a in audited] == ["arbitrate"]

    def test_arbitration_is_off_without_a_model(self, monkeypatch):
        advance = _clock(monkeypatch)
        svc, calls, audited = self._metered(arbiter_model="")

        async def main():
            await svc.setup()
            await self._seed(svc, ["事实一关于 uv", "事实二关于 pip"], advance)
            usage = await svc.arbitrate(7, "alice", provider=_extractor(self.MERGE))
            facts = await svc.list_for_user(7)
            await svc.close()
            return usage, facts

        usage, facts = asyncio.run(main())
        assert usage == Usage()
        assert len(facts) == 2
        assert calls == [] and audited == []

    def test_a_single_fact_never_reaches_the_model(self, monkeypatch):
        svc, calls, _ = self._metered()

        class _Boom(StrictFakeProvider):
            async def stream(self, *args, **kwargs):
                raise AssertionError("one fact must not be arbitrated")
                yield  # pragma: no cover

        async def main():
            await svc.setup()
            await self._seed(svc, ["只有一条事实"])
            usage = await svc.arbitrate(7, "alice", provider=_Boom(responses=[]))
            await svc.close()
            return usage

        assert asyncio.run(main()) == Usage()
        assert calls == []

    def test_unknown_ids_and_non_merge_actions_change_nothing(self, monkeypatch):
        advance = _clock(monkeypatch)
        svc, _, _ = self._metered()
        payload = json.dumps(
            [{"action": "drop", "ids": [1, 2]},          # not a merge: ignored
             {"action": "merge", "ids": [404, 405], "text": "幽灵事实", "kind": "fact"},
             {"action": "merge", "ids": [1], "text": "单条不合并", "kind": "fact"}],
            ensure_ascii=False,
        )

        async def main():
            await svc.setup()
            await self._seed(svc, ["事实一关于 uv", "事实二关于 pip"], advance)
            await svc.arbitrate(7, "alice", provider=_extractor(payload))
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        facts = asyncio.run(main())
        assert [f.text for f in facts] == ["事实一关于 uv", "事实二关于 pip"]

    def test_a_failing_merge_does_not_abort_the_batch(self, monkeypatch):
        advance = _clock(monkeypatch)
        svc, _, _ = self._metered()

        class _BoomEmbed(HashEmbedder):
            async def embed(self, texts):
                if any("引爆" in t for t in texts):
                    raise RuntimeError("boom")
                return await super().embed(texts)

        payload = json.dumps(
            [{"action": "merge", "ids": [1, 2], "text": "引爆这个分组", "kind": "fact"},
             {"action": "merge", "ids": [3, 4], "text": "合并后的第三组", "kind": "fact"}],
            ensure_ascii=False,
        )
        svc._embedder = _BoomEmbed()

        async def main():
            await svc.setup()
            await self._seed(svc, ["事实一", "事实二", "事实三", "事实四"], advance)
            await svc.arbitrate(7, "alice", provider=_extractor(payload))
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        facts = asyncio.run(main())
        assert [f.text for f in facts] == ["事实一", "事实二", "合并后的第三组"]

    def test_secrets_in_a_merged_fact_are_redacted_before_storing(self, monkeypatch):
        advance = _clock(monkeypatch)
        svc, _, _ = self._metered()
        payload = json.dumps(
            [{"action": "merge", "ids": [1, 2],
              "text": "网关密钥是 sk-abcdefghijklmnop1234 改用 pip", "kind": "fact"}],
            ensure_ascii=False,
        )

        async def main():
            await svc.setup()
            await self._seed(svc, ["事实一关于 uv", "事实二关于 pip"], advance)
            await svc.arbitrate(7, "alice", provider=_extractor(payload))
            facts = await svc.list_for_user(7)
            await svc.close()
            return facts

        facts = asyncio.run(main())
        assert len(facts) == 1
        assert "[REDACTED:api_key]" in facts[0].text
        assert "sk-abcdefghijklmnop1234" not in facts[0].text


class TestRetrieval:
    def test_formats_the_header_lines_and_footer(self):
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)
            text, usage = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text, usage

        text, usage = asyncio.run(main())
        lines = text.splitlines()
        assert lines[0] == MEMORY_HEADER
        assert lines[1] == "- (preference) 用户偏好 uv 而非 pip"
        assert lines[-1] == MEMORY_FOOTER
        assert usage.input_tokens > 0  # the query embedding is billed too

    def test_a_dissimilar_query_injects_nothing(self):
        svc = _service(min_similarity=0.99)

        async def main():
            await svc.setup()
            await _learn(svc)
            text, _ = await svc.retrieve(7, "completely unrelated words here")
            await svc.close()
            return text

        assert asyncio.run(main()) == ""

    def test_a_blank_query_never_reaches_the_embedder(self):
        """The live embedding endpoint returns 2560 dims for "" but 1024 for anything else.

        A wrong-width vector is rejected by a collection created at 1024, so the
        `not query.strip()` guard in retrieve() is the only thing between an empty
        prompt and a failed retrieval. Whitespace-only input is well-behaved at the
        endpoint, but there is nothing to look up for either case.
        """

        emb = RecordingEmbedder()
        svc = _service(embedder=emb)

        async def main():
            await svc.setup()
            await _learn(svc)
            emb.seen.clear()  # setup()'s dim probe and _learn both embed
            results = [await svc.retrieve(7, q) for q in ("", "   ", "\n")]
            await svc.close()
            return results

        results = asyncio.run(main())
        assert [text for text, _ in results] == ["", "", ""]
        assert emb.seen == [], emb.seen

    def test_the_injection_budget_drops_lines_rather_than_overflowing(self):
        svc = _service(inject_max_chars=200)

        async def main():
            await svc.setup()
            texts = ["事实 %d %s" % (i, "填充" * 10) for i in range(5)]
            await _seed_direct(svc, texts, user_id=7)
            retrieved, _ = await svc.retrieve(7, "事实 填充")
            await svc.close()
            return retrieved

        text = asyncio.run(main())
        assert len(text) <= 200
        assert text.startswith(MEMORY_HEADER) and text.endswith(MEMORY_FOOTER)
        assert 0 < text.count("\n- (fact)") < 5  # some fit, the rest were dropped

    def test_one_user_never_sees_another_users_facts(self):
        svc = _service()

        async def main():
            await svc.setup()
            # The same fact for both users: dedup is tenant-scoped too, so the
            # second insert must not be swallowed by the first user's copy.
            await _learn(svc, user_id=7)
            await _learn(svc, user_id=8)
            text, _ = await svc.retrieve(8, "uv 依赖管理")
            await svc.close()
            return text

        assert asyncio.run(main()).count("- (preference)") == 1


class _FakeReranker:
    """Reranker with scripted scores, recording every call it was asked to make.

    Scores are positional, matching the Reranker contract: one per document, in
    input order. Passing an Exception instead makes rerank raise, and passing the
    wrong number of scores is deliberate in the tests that do it - the service has
    to survive a reranker that breaks its own contract.
    """

    def __init__(self, scores: Sequence[float] | Exception, tokens: int = 11) -> None:
        self.scores = scores
        self.tokens = tokens
        self.calls: list[tuple[str, list[str]]] = []
        self.closed = False

    async def rerank(
        self, query: str, documents: Sequence[str]
    ) -> tuple[list[float], Usage]:
        self.calls.append((query, list(documents)))
        if isinstance(self.scores, Exception):
            raise self.scores
        return list(self.scores), Usage(input_tokens=self.tokens, output_tokens=0)

    async def close(self) -> None:
        self.closed = True


class _OrderedStore(InMemoryStore):
    """InMemoryStore whose search returns a fixed result, ignoring the query vector.

    The second-stage tests need to know exactly what cosine recalled and in what
    order. HashEmbedder cannot promise either, so the store supplies both and the
    assertions stay about reranking rather than about hash collisions. Search
    answers in the index dialect - (fact id, score) pairs - because that is all
    the index knows; the facts themselves come from the repo via _bind().
    """

    def __init__(self, facts: list[Fact]) -> None:
        super().__init__()
        self.facts = facts
        self.limits: list[int] = []

    async def search(self, user_id: int, vector: Sequence[float], limit: int) -> list[tuple[int, float]]:
        self.limits.append(int(limit))
        return [(f.id, f.score) for f in self.facts[: int(limit)]]


async def _bind(svc: MemoryService, store: _OrderedStore, user_id: int = 7) -> None:
    """Give the ordered store's facts repo rows under the ids search reports.

    Retrieval joins index hits back through repo.get_by_ids, so a second-stage test
    must seed both layers with agreeing ids - otherwise the join drops everything
    and the test quietly passes on empty output.
    """
    vecs, _ = await svc._embedder.embed([f.text for f in store.facts])
    ids = await svc._repo.insert_many(
        user_id,
        [
            MemoryRowIn(text=f.text, kind="fact", source_session="s1", embedding=list(v))
            for f, v in zip(store.facts, vecs, strict=True)
        ],
    )
    for fact, fid in zip(store.facts, ids, strict=True):
        fact.id = fid


def _facts(*texts: str) -> list[Fact]:
    """Facts in descending cosine score, so recall order is the order given."""
    return [
        Fact(
            id=i,
            user_id=7,
            text=t,
            kind="fact",
            source_session="s1",
            created_at="2026-01-01T00:00:00",
            score=0.9 - i * 0.1,
        )
        for i, t in enumerate(texts)
    ]


def _reranker(body: dict[str, Any], seen: list[Any] | None = None, status: int = 200):
    """A DashScopeReranker whose HTTP client is a MockTransport, so no network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json=body)

    r = DashScopeReranker("https://rerank.invalid/v1", "qwen-rerank", api_key="k")
    r._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return r


class TestRerankEndpoint:
    def test_scores_land_on_the_documents_they_describe(self):
        """The API sorts results by score, so response order is not input order.

        Zipping the two would attach every score to the wrong fact and silently
        invert the ranking - the exact failure the second stage exists to prevent.
        """
        body = {
            "output": {
                "results": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.40},
                    {"index": 1, "relevance_score": 0.05},
                ]
            },
            "usage": {"total_tokens": 347},
        }

        async def main():
            r = _reranker(body)
            out = await r.rerank("q", ["a", "b", "c"])
            await r.close()
            return out

        scores, usage = asyncio.run(main())
        assert scores == [0.40, 0.05, 0.91]
        assert usage.input_tokens == 347 and usage.output_tokens == 0

    def test_the_request_asks_for_every_document_back(self):
        """A default top_n would truncate the list before the caller has scored it."""
        seen: list[Any] = []

        async def main():
            r = _reranker({"output": {"results": []}}, seen)
            await r.rerank("query text", ["a", "b", "c"])
            await r.close()

        asyncio.run(main())
        assert seen == [
            {
                "model": "qwen-rerank",
                "input": {"query": "query text", "documents": ["a", "b", "c"]},
                "parameters": {"return_documents": False, "top_n": 3},
            }
        ]

    @pytest.mark.parametrize(
        "results",
        [
            [],
            [{"index": 9, "relevance_score": 1.0}],  # out of range
            [{"index": "0", "relevance_score": 1.0}],  # not an int
            [{"index": 0, "relevance_score": "high"}],  # not a number
            [{"relevance_score": 0.9}],  # no index at all
            [{"index": 0}],  # no score at all
        ],
    )
    def test_a_malformed_result_scores_zero_rather_than_raising(self, results):
        """Zero is the right answer: it loses one candidate, not the whole turn."""

        async def main():
            r = _reranker({"output": {"results": results}})
            scores, _ = await r.rerank("q", ["a", "b"])
            await r.close()
            return scores

        scores = asyncio.run(main())
        assert len(scores) == 2  # always one score per document, whatever came back
        assert scores == [0.0, 0.0]

    def test_an_empty_document_list_never_reaches_the_endpoint(self):
        seen: list[Any] = []

        async def main():
            r = _reranker({"output": {"results": []}}, seen)
            out = await r.rerank("q", [])
            await r.close()
            return out

        assert asyncio.run(main()) == ([], Usage())
        assert seen == []

    def test_an_http_error_propagates_so_the_caller_can_degrade(self):
        """Swallowing it here would report an outage as "nothing is relevant"."""

        async def main():
            r = _reranker({"error": "boom"}, status=500)
            await r.rerank("q", ["a"])

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(main())

    @pytest.mark.parametrize("url,model", [("", "m"), ("https://u", ""), ("", "")])
    def test_an_unconfigured_reranker_is_absent_not_broken(self, url, model):
        """Either half missing means single-stage retrieval, which still works."""
        assert get_reranker(url, model) is None

    def test_a_configured_reranker_is_a_dashscope_one(self):
        r = get_reranker("https://rerank.invalid/v1", "qwen-rerank")
        assert isinstance(r, DashScopeReranker)
        asyncio.run(r.close())


class TestTwoStageRetrieval:
    """retrieve() with a reranker: recall wide, then let the reranker decide."""

    TEXTS = ("用 uv 管理依赖", "后端框架是 FastAPI", "测试不用 pytest-asyncio")

    def _svc(self, reranker: Any, **over: Any):
        store = _OrderedStore(_facts(*self.TEXTS))
        return _service(store=store, reranker=reranker, **over), store

    def test_the_reranker_decides_the_order_not_cosine(self):
        rr = _FakeReranker([0.50, 0.95, 0.40])
        svc, store = self._svc(rr)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, usage = await svc.retrieve(7, "测试框架有什么约定")
            await svc.close()
            return text, usage

        text, usage = asyncio.run(main())
        assert text.splitlines()[1:-1] == [
            "- (fact) 后端框架是 FastAPI",
            "- (fact) 用 uv 管理依赖",
            "- (fact) 测试不用 pytest-asyncio",
        ]
        assert rr.calls == [("测试框架有什么约定", list(self.TEXTS))]
        # Both stages are billed: loop.py folds this Usage into the turn, which is
        # how rerank tokens reach usage_records.
        assert usage.input_tokens >= rr.tokens

    def test_a_weak_rerank_score_is_dropped_even_though_cosine_kept_it(self):
        """The point of the second stage: cosine recalled it, rerank rejects it."""
        rr = _FakeReranker([0.05, 0.95, 0.29])
        svc, store = self._svc(rr, rerank_min_score=0.3)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, _ = await svc.retrieve(7, "后端用什么框架")
            await svc.close()
            return text

        assert asyncio.run(main()).splitlines()[1:-1] == ["- (fact) 后端框架是 FastAPI"]

    def test_everything_below_the_gate_injects_nothing(self):
        rr = _FakeReranker([0.01, 0.02, 0.03])
        svc, store = self._svc(rr, rerank_min_score=0.3)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, usage = await svc.retrieve(7, "量子色动力学的最新进展")
            await svc.close()
            return text, usage

        text, usage = asyncio.run(main())
        assert text == ""
        assert usage.input_tokens >= rr.tokens  # a rejected rerank is still paid for

    def test_top_k_truncates_after_reranking(self):
        rr = _FakeReranker([0.90, 0.80, 0.70])
        svc, store = self._svc(rr, top_k=2, recall_k=3)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, _ = await svc.retrieve(7, "uv")
            await svc.close()
            return text

        lines = asyncio.run(main()).splitlines()[1:-1]
        assert lines == ["- (fact) 用 uv 管理依赖", "- (fact) 后端框架是 FastAPI"]

    def test_recall_is_wider_than_what_gets_injected(self):
        """Recall 20, inject 5: the reranker needs a candidate pool to choose from."""
        rr = _FakeReranker([0.9, 0.8, 0.7])
        svc, store = self._svc(rr, top_k=5, recall_k=20)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            await svc.retrieve(7, "uv")
            await svc.close()

        asyncio.run(main())
        assert store.limits == [20]

    def test_recall_k_never_narrows_below_top_k(self):
        """Recalling fewer than we inject would be single-stage with an extra hop."""
        rr = _FakeReranker([0.9, 0.8, 0.7])
        svc, store = self._svc(rr, top_k=5, recall_k=2)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            await svc.retrieve(7, "uv")
            await svc.close()

        asyncio.run(main())
        assert store.limits == [5]

    def test_a_reranker_outage_falls_back_to_cosine_order(self):
        """Memory survives the reranker. Losing it would cost the user their facts."""
        rr = _FakeReranker(RuntimeError("rerank endpoint down"))
        svc, store = self._svc(rr)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, usage = await svc.retrieve(7, "uv")
            await svc.close()
            return text, usage

        text, usage = asyncio.run(main())
        assert len(rr.calls) == 1
        assert text.splitlines()[1:-1] == [f"- (fact) {t}" for t in self.TEXTS]
        assert usage.input_tokens < rr.tokens  # nothing was billed for the failure

    def test_a_score_count_mismatch_falls_back_to_cosine_order(self):
        """A reranker that breaks its own contract must not misalign scores to facts."""
        rr = _FakeReranker([0.99])
        svc, store = self._svc(rr)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, usage = await svc.retrieve(7, "uv")
            await svc.close()
            return text, usage

        text, usage = asyncio.run(main())
        assert text.splitlines()[1:-1] == [f"- (fact) {t}" for t in self.TEXTS]
        assert usage.input_tokens >= rr.tokens  # the call happened, so it is billed

    def test_one_recalled_fact_skips_the_round_trip(self):
        """Nothing to order it against; the hot path should not pay for a threshold."""
        rr = _FakeReranker([0.99])
        store = _OrderedStore(_facts(self.TEXTS[0]))
        svc = _service(store=store, reranker=rr)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, _ = await svc.retrieve(7, "uv")
            await svc.close()
            return text

        assert asyncio.run(main()) != ""
        assert rr.calls == []

    def test_no_reranker_means_the_cosine_order_is_kept(self):
        svc, store = self._svc(None)

        async def main():
            await svc.setup()
            await _bind(svc, store)
            text, _ = await svc.retrieve(7, "uv")
            await svc.close()
            return text

        lines = asyncio.run(main()).splitlines()[1:-1]
        assert lines == [f"- (fact) {t}" for t in self.TEXTS]

    def test_a_reranked_fact_carries_the_rerank_score_not_cosine(self):
        """Anything reading Fact.score downstream must not be told it is cosine."""
        rr = _FakeReranker([0.11, 0.22, 0.33])
        kept: list[Fact] = []
        store = _OrderedStore(_facts(*self.TEXTS))
        svc = _service(store=store, reranker=rr, rerank_min_score=0.0)
        original = svc._format

        def spy(facts):
            kept.extend(facts)
            return original(facts)

        svc._format = spy

        async def main():
            await svc.setup()
            await _bind(svc, store)
            await svc.retrieve(7, "uv")
            await svc.close()

        asyncio.run(main())
        assert [f.score for f in kept] == [0.33, 0.22, 0.11]

    def test_closing_the_service_closes_the_reranker(self):
        """Its httpx client holds a connection pool; leaking it per boot adds up."""
        rr = _FakeReranker([0.9, 0.8, 0.7])
        svc, _ = self._svc(rr)

        async def main():
            await svc.setup()
            await svc.close()

        asyncio.run(main())
        assert rr.closed is True

    def test_a_reranker_that_fails_to_close_does_not_break_shutdown(self):
        class Broken(_FakeReranker):
            async def close(self) -> None:
                raise RuntimeError("pool already gone")

        rr = Broken([0.9, 0.8, 0.7])
        svc, _ = self._svc(rr)

        async def main():
            await svc.setup()
            await svc.close()  # must not raise

        asyncio.run(main())


class TestDegradation:
    def test_no_uri_means_memory_is_off(self):
        assert isinstance(get_store(""), NoOpStore)
        assert get_store("").enabled is False

    def test_no_embedding_model_means_no_embedder(self):
        """A retrieval store that cannot vectorise the query is useless."""
        assert get_embedder("") is None

    def test_an_off_service_answers_empty_and_spends_nothing(self):
        svc = _service(store=NoOpStore())

        async def main():
            await svc.setup()
            text, usage = await svc.retrieve(7, "anything")
            spent = await _learn(svc)
            facts = await svc.list_for_user(7)
            queued = svc.spawn_extraction(
                user_id=7, username="alice", session_id="s1",
                messages=_transcript(), fallback_model="fake/demo",
            )
            ok = await svc.ping()
            await svc.close()
            return svc.status, text, usage, spent, facts, queued, ok

        status, text, usage, spent, facts, queued, ok = asyncio.run(main())
        assert status == "disabled"
        assert text == "" and usage == Usage() and spent == Usage()
        assert facts == [] and queued is False
        assert ok is True  # 'off' is not 'broken'

    def test_a_store_that_fails_setup_disables_memory_instead_of_raising(self):
        class Broken(InMemoryStore):
            async def setup(self, dim: int | None = None) -> None:
                raise RuntimeError("milvus said no")

        svc = _service(store=Broken())

        async def main():
            await svc.setup()  # must not raise
            text, _ = await svc.retrieve(7, "anything")
            return svc.status, svc.enabled, text

        status, enabled, text = asyncio.run(main())
        assert status == "unavailable: RuntimeError"
        assert enabled is False and text == ""

    def test_the_vector_width_is_probed_when_not_configured(self):
        """PI_EMBEDDING_DIM=0 means the model's native width; the schema must match."""
        svc = _service(dim=0, embedder=HashEmbedder(dim=32))

        async def main():
            await svc.setup()
            return svc.status, svc._dim

        assert asyncio.run(main()) == ("ready", 32)


class TestDualWrite:
    """The repo-first write protocol: MySQL is written even when the index is not.

    A Milvus outage during a write costs a stale index, never a lost fact - and the
    pending_sync sweep is what closes the gap once Milvus answers again.
    """

    def _flaky(self):
        class Flaky(InMemoryStore):
            def __init__(self) -> None:
                super().__init__()
                self.broken = False

            async def upsert(self, user_id, rows):
                if self.broken:
                    raise RuntimeError("milvus down")
                return await super().upsert(user_id, rows)

        return Flaky()

    def test_a_failed_index_sync_leaves_the_fact_stored_and_pending(self):
        store = self._flaky()
        store.broken = True
        svc = _service(store=store)

        async def main():
            await svc.setup()
            usage = await _learn(svc)
            stored = await svc._repo.get_active(7)
            pending = await svc._repo.pending_sync(10)
            await svc.close()
            return usage, stored, pending

        usage, stored, pending = asyncio.run(main())
        assert usage.input_tokens > 0  # the extraction was paid for either way
        assert [f.text for f, _ in stored] == ["用户偏好 uv 而非 pip"]  # the truth landed
        assert [f.id for f, _ in pending] == [stored[0][0].id]  # the mirror did not

    def test_an_upsert_that_returns_false_stays_pending_too(self):
        """A refusal is as stale as an exception; both mean 'retry later'."""

        class Refusing(InMemoryStore):
            async def upsert(self, user_id, rows):
                return False

        svc = _service(store=Refusing())

        async def main():
            await svc.setup()
            await _learn(svc)
            pending = await svc._repo.pending_sync(10)
            await svc.close()
            return pending

        assert len(asyncio.run(main())) == 1

    def test_the_sweep_lands_what_the_index_missed(self, monkeypatch):
        monkeypatch.setattr("pi.memory.service.INDEX_COOLDOWN_SECONDS", 0.0)
        store = self._flaky()
        store.broken = True
        svc = _service(store=store)

        async def main():
            await svc.setup()
            await _learn(svc)  # the failed upsert trips the breaker
            fid = (await svc._repo.get_active(7))[0][0].id
            store.broken = False  # milvus comes back; the window has passed
            landed = await svc.sync_pending()
            pending = await svc._repo.pending_sync(10)
            await svc.close()
            return landed, pending, fid

        landed, pending, fid = asyncio.run(main())
        assert landed == 1
        assert pending == []
        # The index now addresses the fact by the same id the repo assigned it -
        # the deterministic-PK contract that makes the retry idempotent.
        assert list(store._index) == [fid]

    def test_a_row_deleted_while_pending_never_reaches_the_index(self):
        """pending_sync only lists active rows, so a delete wins the race."""
        store = self._flaky()
        store.broken = True
        svc = _service(store=store)

        async def main():
            await svc.setup()
            await _learn(svc)
            fid = (await svc._repo.get_active(7))[0][0].id
            await svc._repo.delete(7, fid)
            landed = await svc.sync_pending()
            await svc.close()
            return landed

        assert asyncio.run(main()) == 0


class TestZombieVectors:
    """Index hits join back through the repo; rows the repo no longer owns read
    as absent. This is what makes a lagging or partially-failed index safe: it can
    be stale, but it cannot inject a memory the user erased."""

    def test_a_fact_deleted_from_the_repo_reads_as_absent(self):
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)
            fid = (await svc._repo.get_active(7))[0][0].id
            await svc._repo.delete(7, fid)  # the index still holds the vector
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text

        assert asyncio.run(main()) == ""

    def test_a_decayed_fact_is_not_injected(self):
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)
            await svc._repo.deactivate_older_than("3000-01-01T00:00:00+00:00")
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text

        assert asyncio.run(main()) == ""

    def test_a_live_fact_is_still_injected(self):
        """The control: the join filters the dead, not everything."""
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text

        assert "用户偏好 uv 而非 pip" in asyncio.run(main())


class TestIndexDegradation:
    """A dead index degrades to a repo scan, and the breaker keeps the dead index
    off the per-turn hot path until its cooldown window expires."""

    def test_a_failing_search_falls_back_to_the_repo(self):

        class DeadSearch(InMemoryStore):
            async def search(self, user_id, vector, limit):
                raise RuntimeError("milvus search down")

        svc = _service(store=DeadSearch())

        async def main():
            await svc.setup()
            await _learn(svc)  # upserts still work; only search is dead
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text

        assert "用户偏好 uv 而非 pip" in asyncio.run(main())

    def test_a_dead_index_and_a_dead_repo_still_meter_the_embedding(self):
        """The fallback above can die too, and the embedding was paid for regardless.

        `_recall` swallows a failing search and scans the repo instead, so reaching
        `recall_failed` means MySQL went away as well. The embed call has already
        happened by then: returning a fresh Usage() would drop paid tokens from
        usage_records, and with them the quota charge and the arbiter's dirty-user
        scan. `join_failed`, the next guard down, has always returned `total`.
        """

        class DeadSearch(InMemoryStore):
            async def search(self, user_id, vector, limit):
                raise RuntimeError("milvus search down")

        class FlakyRepo(DictMemoryRepo):
            def __init__(self) -> None:
                super().__init__()
                self.dead = False

            async def get_active(self, user_id: int, limit: int = 500):
                if self.dead:
                    raise RuntimeError("mysql is down too")
                return await super().get_active(user_id, limit=limit)

        repo = FlakyRepo()
        svc = _service(store=DeadSearch(), repo=repo)

        async def main():
            await svc.setup()
            await _learn(svc)  # repo healthy here, so the fact lands
            repo.dead = True  # ...and now the fallback dies with the index
            text, usage, stats = await svc.retrieve_traced(7, "uv 依赖管理")
            repo.dead = False  # let close() drain the extraction cleanly
            await svc.close()
            return text, usage, stats

        text, usage, stats = asyncio.run(main())
        assert text == "", "a total retrieval failure injects nothing"
        assert (stats["outcome"], stats["stage"]) == ("recall_failed", "recall")
        assert usage.input_tokens > 0, "the embedding was spent; it must be metered"

    def test_an_index_that_lags_its_upsert_still_recalls_from_the_repo(self):
        """Bounded consistency: a vector upserted moments ago is invisible to
        search for ~0.5s on the live cluster. Zero hits must resolve against
        MySQL - the join below can only filter index hits, never recover
        invisible ones, so without this a follow-up turn in the same second
        would see yesterday's memory but not the one just extracted."""

        class Lagging(InMemoryStore):
            async def search(self, user_id, vector, limit):
                return []  # the upsert has not landed in the searchable set yet

        svc = _service(store=Lagging())

        async def main():
            await svc.setup()
            await _learn(svc)  # repo row + mirror landed, index just can't see it
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text

        assert "用户偏好 uv 而非 pip" in asyncio.run(main())

    def test_one_timeout_per_cooldown_window_not_one_per_turn(self):

        class Counting(InMemoryStore):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            async def search(self, user_id, vector, limit):
                self.attempts += 1
                raise RuntimeError("down")

        store = Counting()
        svc = _service(store=store)

        async def main():
            await svc.setup()
            await _learn(svc)
            for _ in range(5):
                await svc.retrieve(7, "uv 依赖管理")
            await svc.close()

        asyncio.run(main())
        assert store.attempts == 1, store.attempts

    def test_the_breaker_recovers_when_the_window_passes(self, monkeypatch):
        monkeypatch.setattr("pi.memory.service.INDEX_COOLDOWN_SECONDS", 0.0)

        class Healing(InMemoryStore):
            def __init__(self) -> None:
                super().__init__()
                self.broken = True
                self.attempts = 0

            async def search(self, user_id, vector, limit):
                self.attempts += 1
                if self.broken:
                    raise RuntimeError("down")
                return await super().search(user_id, vector, limit)

        store = Healing()
        svc = _service(store=store)

        async def main():
            await svc.setup()
            await _learn(svc)
            await svc.retrieve(7, "uv 依赖管理")  # trips the breaker
            store.broken = False
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return text

        assert "用户偏好 uv 而非 pip" in asyncio.run(main())
        assert store.attempts == 2, store.attempts  # retried once the window passed

    def test_an_unreachable_index_at_boot_runs_degraded_not_disabled(self):
        """Milvus down at startup used to disable memory entirely. With the repo as
        the truth it means repo-only mode: writes persist, reads scan."""

        class BootFail(InMemoryStore):
            async def setup(self, dim: int | None = None) -> None:
                raise ConnectionError("milvus unreachable")

        svc = _service(store=BootFail())

        async def main():
            await svc.setup()
            await _learn(svc)
            text, _ = await svc.retrieve(7, "uv 依赖管理")
            await svc.close()
            return svc.status, svc.enabled, text

        status, enabled, text = asyncio.run(main())
        assert status == "ready (index degraded)"
        assert enabled is True
        assert "用户偏好 uv 而非 pip" in text


class TestDecay:
    """Facts nobody re-confirms fade from recall, but stay in MySQL.

    Decay's cutoff is real wall-clock time; the seeds use the frozen repo clock so
    "90 days ago" and "last week" can both be arranged in one test.
    """

    def test_only_unconfirmed_facts_fade(self, monkeypatch):
        advance = _clock(monkeypatch, start="2025-12-01T00:00:00+00:00")
        svc = _service()

        async def main():
            await svc.setup()
            await _seed_direct(svc, ["陈年旧事"], user_id=7)  # last_seen 2025-12-01
            advance(300 * 24 * 60)  # ~300 days later: well past any 90-day cutoff
            await _seed_direct(svc, ["新鲜事实"], user_id=7)  # last_seen ~2026-09
            faded = await svc.decay(90)
            facts = await svc.list_for_user(7)
            text, _ = await svc.retrieve(7, "新鲜事实")
            await svc.close()
            return faded, facts, text

        faded, facts, text = asyncio.run(main())
        assert faded == 1
        assert [f.text for f in facts] == ["新鲜事实"]
        assert "新鲜事实" in text

    def test_decayed_rows_stay_in_mysql(self, monkeypatch):
        """Soft by design: 落库所有记忆 means the row survives the fade - PIPL
        erasure (delete_user) is a different, harder operation."""
        _clock(monkeypatch, start="2025-01-01T00:00:00+00:00")
        svc = _service()

        async def main():
            await svc.setup()
            ids = await _seed_direct(svc, ["陈年旧事"], user_id=7)
            await svc.decay(90)
            rows = svc._repo._rows
            await svc.close()
            return ids, rows

        ids, rows = asyncio.run(main())
        assert len(rows) == 1 and ids[0] in rows
        assert rows[ids[0]]["is_active"] is False

    def test_zero_days_disables_decay(self):
        svc = _service()

        async def main():
            await svc.setup()
            await _learn(svc)
            faded = await svc.decay(0)
            facts = await svc.list_for_user(7)
            await svc.close()
            return faded, facts

        faded, facts = asyncio.run(main())
        assert faded == 0
        assert len(facts) == 1

    def test_decay_drops_the_index_vectors(self, monkeypatch):
        _clock(monkeypatch, start="2025-01-01T00:00:00+00:00")
        store = InMemoryStore()
        svc = _service(store=store)

        async def main():
            await svc.setup()
            ids = await _seed_direct(svc, ["陈年旧事"], user_id=7)
            await svc.decay(90)
            hits = await store.search(7, [1.0] * 64, 10)
            await svc.close()
            return ids, hits

        ids, hits = asyncio.run(main())
        assert [fid for fid, _ in hits] == []


class TestUserMemoryRepoSQLite:
    """The SQL repo on a real database: the semantics MySQL will run in production.

    The DictMemoryRepo tests above pin the protocol's intent; this class pins the
    SQL translation of it - rowcount-based existence checks, autoincrement ids,
    the float32 blob round trip.
    """

    @pytest.fixture()
    def sql(self, tmp_path):
        from pi.server.db import Database, UserMemoryRepo, UserRepo

        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'mem.db').as_posix()}")

        async def build() -> tuple[int, int]:
            await db.init()
            users = UserRepo(db)
            alice = await users.create("alice", "hash", is_admin=False)
            bob = await users.create("bob", "hash", is_admin=False)
            return alice.id, bob.id

        alice, bob = asyncio.run(build())
        yield UserMemoryRepo(db), alice, bob
        asyncio.run(db.dispose())

    def test_ids_never_recycle_after_a_delete(self, sql):
        """The id is the vector-store primary key: recycling it after a delete would
        alias a stale Milvus row onto a new fact. Plain SQLite rowids restart at
        max+1; AUTOINCREMENT is what keeps the sequence monotonic."""
        repo, alice, _ = sql

        async def main():
            (first,) = await repo.insert_many(
                alice, [MemoryRowIn(text="第一条", kind="fact", source_session="s1", embedding=[0.1])]
            )
            await repo.delete(alice, first)
            (second,) = await repo.insert_many(
                alice, [MemoryRowIn(text="替换", kind="fact", source_session="s1", embedding=[0.2])]
            )
            return first, second

        first, second = asyncio.run(main())
        assert second > first

    def test_touch_is_an_update_not_a_resurrection(self, sql):
        repo, alice, _ = sql

        async def main():
            (fid,) = await repo.insert_many(
                alice,
                [MemoryRowIn(text="事实", kind="fact", source_session="s1", embedding=[1.0, 0.0])],
                synced=True,
            )
            ok = await repo.touch(alice, fid, [0.0, 1.0])
            stored = await repo.get_active(alice)
            await repo.delete(alice, fid)
            gone = await repo.touch(alice, fid, [0.5])
            return ok, stored, gone

        ok, stored, gone = asyncio.run(main())
        assert ok is True and gone is False
        fact, vec = stored[0]
        assert fact.id is not None
        assert all(abs(a - b) < 1e-6 for a, b in zip(vec, [0.0, 1.0], strict=True))

    def test_update_text_keeps_the_row_identity(self, sql):
        """Arbitration's merge: same id, new text, marked as the arbiter's work."""
        repo, alice, bob = sql

        async def main():
            (fid,) = await repo.insert_many(
                alice, [MemoryRowIn(text="旧表述", kind="fact", source_session="s1", embedding=[0.1])]
            )
            ok = await repo.update_text(
                alice, fid, "合并后的表述", "convention", [0.9], source_session="arbiter"
            )
            cross = await repo.update_text(bob, fid, "越权改写", "fact", [0.9])
            stored = await repo.get_active(alice)
            return ok, cross, stored

        ok, cross, stored = asyncio.run(main())
        assert ok is True and cross is False  # another tenant's id reads as absent
        [(fact, _)] = stored
        assert fact.text == "合并后的表述"
        assert fact.kind == "convention"
        assert fact.source_session == "arbiter"

    def test_get_by_ids_skips_rows_the_repo_no_longer_owns(self, sql):
        repo, alice, _ = sql

        async def main():
            ids = await repo.insert_many(
                alice,
                [
                    MemoryRowIn(text="存活", kind="fact", source_session="s1", embedding=[0.1]),
                    MemoryRowIn(text="已删", kind="fact", source_session="s1", embedding=[0.2]),
                ],
            )
            await repo.delete(alice, ids[1])
            live = await repo.get_by_ids(alice, ids)
            absent = await repo.get_by_ids(alice, [9999])
            return live, absent

        live, absent = asyncio.run(main())
        assert [f.text for f in live] == ["存活"]
        assert absent == []

    def test_deactivate_older_than_returns_tenant_safe_pairs(self, sql):
        repo, alice, bob = sql

        async def main():
            await repo.insert_many(
                alice,
                [
                    MemoryRowIn(text="甲", kind="fact", source_session="s1", embedding=[0.1]),
                    MemoryRowIn(text="乙", kind="fact", source_session="s1", embedding=[0.2]),
                ],
            )
            await repo.insert_many(
                bob, [MemoryRowIn(text="丙", kind="fact", source_session="s1", embedding=[0.3])]
            )
            pairs = await repo.deactivate_older_than("3000-01-01T00:00:00+00:00")
            alice_left = await repo.get_active(alice)
            bob_left = await repo.get_active(bob)
            again = await repo.deactivate_older_than("2000-01-01T00:00:00+00:00")
            return pairs, alice_left, bob_left, again

        pairs, alice_left, bob_left, again = asyncio.run(main())
        assert sorted(pairs) == [(alice, 1), (alice, 2), (bob, 3)]
        assert alice_left == [] and bob_left == []
        assert again == []  # already inactive: idempotent

    def test_pending_sync_only_lists_unmirrored_rows(self, sql):
        repo, alice, _ = sql

        async def main():
            (synced,) = await repo.insert_many(
                alice,
                [MemoryRowIn(text="已同步", kind="fact", source_session="s1", embedding=[0.1])],
                synced=True,
            )
            (unsynced,) = await repo.insert_many(
                alice,
                [MemoryRowIn(text="未同步", kind="fact", source_session="s1", embedding=[0.2])],
            )
            pending = await repo.pending_sync(10)
            marked = await repo.mark_synced([unsynced])
            after = await repo.pending_sync(10)
            return synced, pending, marked, after

        synced, pending, marked, after = asyncio.run(main())
        assert [f.text for f, _ in pending] == ["未同步"]
        assert marked == 1
        assert after == []

    def test_delete_is_tenant_scoped(self, sql):
        repo, alice, bob = sql

        async def main():
            (fid,) = await repo.insert_many(
                alice, [MemoryRowIn(text="alice 的", kind="fact", source_session="s1", embedding=[0.1])]
            )
            cross = await repo.delete(bob, fid)
            nobody = await repo.delete_user(bob)
            own = await repo.delete(alice, fid)
            return cross, nobody, own

        assert asyncio.run(main()) == (False, 0, True)

    def test_the_embedding_survives_the_float32_round_trip(self, sql):
        repo, alice, _ = sql
        vec = [0.1, -0.2, 0.3333333, 1.0, 0.0]

        async def main():
            await repo.insert_many(
                alice, [MemoryRowIn(text="向量", kind="fact", source_session="s1", embedding=vec)]
            )
            stored = await repo.get_active(alice)
            return stored[0][1]

        out = asyncio.run(main())
        assert len(out) == len(vec)
        assert all(abs(a - b) < 1e-6 for a, b in zip(out, vec, strict=True))


class TestBackgroundExtraction:
    class _Spy(MemoryService):
        def __init__(self, **kwargs: Any):
            super().__init__(**kwargs)
            self.calls: list[dict[str, Any]] = []

        async def record(self, **kwargs: Any) -> Usage:
            self.calls.append(kwargs)
            return Usage()

    def _spy(self, **over: Any) -> Any:
        kwargs: dict[str, Any] = dict(
            store=InMemoryStore(), embedder=HashEmbedder(dim=64),
            memory_model="fake/mem", extract_min_chars=10,
        )
        kwargs.update(over)
        return self._Spy(**kwargs)

    def test_spawn_queues_one_task_that_drain_waits_for(self):
        svc = self._spy()

        async def main():
            await svc.setup()
            queued = svc.spawn_extraction(
                user_id=7, username="alice", session_id="s1",
                messages=_transcript(), fallback_model="fake/demo",
            )
            pending = len(svc._tasks)
            await svc.drain()
            return queued, pending, svc.calls

        queued, pending, calls = asyncio.run(main())
        assert queued is True and pending == 1
        assert len(calls) == 1
        assert calls[0]["user_id"] == 7 and calls[0]["fallback_model"] == "fake/demo"

    def test_a_short_turn_is_skipped_entirely(self):
        """Most turns are 'yes' and 'done'; paying a model call for those is waste."""
        svc = self._spy(extract_min_chars=400)

        async def main():
            await svc.setup()
            return svc.spawn_extraction(
                user_id=7, username="alice", session_id="s1",
                messages=_transcript(), fallback_model="fake/demo",
            )

        assert asyncio.run(main()) is False

    def test_a_background_failure_never_reaches_the_caller(self):
        class Boom(MemoryService):
            async def record(self, **kwargs: Any) -> Usage:
                raise RuntimeError("milvus exploded mid-extraction")

        svc = Boom(
            store=InMemoryStore(), embedder=HashEmbedder(dim=64),
            memory_model="fake/mem", extract_min_chars=0,
        )

        async def main():
            await svc.setup()
            svc.spawn_extraction(
                user_id=7, username="alice", session_id="s1",
                messages=_transcript(), fallback_model="fake/demo",
            )
            await svc.drain()  # must not raise

        asyncio.run(main())


class TestErasureSuppression:
    """Deregistration has to be terminal.

    purge_user() deletes user_memories and usage_records in one transaction, so an
    extraction still in flight writes both again *afterwards*: the erased account
    leaves rows behind and the receipt that claimed N deletions under-reports.
    These cover the two halves of the fix - refusing new work for the user being
    erased, and waiting only for that user's in-flight work.
    """

    @staticmethod
    def _gate(release: threading.Event):
        """extract_facts keyed on the transcript text.

        The real signature takes no user_id, and the transcript is the only
        per-call input a test controls, so "HOLD" in the text is how one service
        parks a task for one user while letting another through.
        """

        async def _extract(provider, messages):
            text = "".join(
                b.text for m in messages for b in m.blocks if isinstance(b, TextBlock)
            )
            if "HOLD" in text:
                while not release.is_set():
                    await asyncio.sleep(0.005)
            return [], Usage()

        return _extract

    @staticmethod
    def _spawn(svc: MemoryService, uid: int, text: str) -> bool:
        return svc.spawn_extraction(
            user_id=uid, username=f"u{uid}", session_id="s1",
            messages=_transcript(text), fallback_model="fake/demo",
        )

    def test_begin_erasure_refuses_that_user_and_only_that_user(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("pi.memory.service.extract_facts", self._gate(release))
        svc = _service()

        async def main():
            await svc.setup()
            svc.begin_erasure(7)
            try:
                return self._spawn(svc, 7, "alice 的习惯"), self._spawn(svc, 8, "bob 的习惯")
            finally:
                release.set()
                svc.end_erasure(7)
                await svc.drain(timeout=2)

        refused, allowed = asyncio.run(main())
        assert refused is False, "an account being erased must not gain new facts"
        assert allowed is True, "suppression is scoped to one user, not global"

    def test_end_erasure_restores_the_normal_path(self, monkeypatch):
        """A purge that raised leaves the account alive, so the caller's finally
        block lifting suppression is what keeps it entitled to memory afterwards."""
        release = threading.Event()
        release.set()
        monkeypatch.setattr("pi.memory.service.extract_facts", self._gate(release))
        svc = _service()

        async def main():
            await svc.setup()
            svc.begin_erasure(7)
            svc.end_erasure(7)
            queued = self._spawn(svc, 7, "alice 的习惯")
            await svc.drain(timeout=2)
            return queued

        assert asyncio.run(main()) is True

    def test_a_scoped_drain_waits_for_that_user_and_not_for_others(self, monkeypatch):
        """One account's erasure must not stall on everyone else's traffic."""
        release = threading.Event()
        monkeypatch.setattr("pi.memory.service.extract_facts", self._gate(release))
        svc = _service()

        async def main():
            await svc.setup()
            self._spawn(svc, 7, "HOLD alice")
            self._spawn(svc, 8, "bob goes straight through")
            other = await svc.drain(timeout=2, user_id=8)
            stuck = await svc.drain(timeout=0.05, user_id=7)
            release.set()
            settled = await svc.drain(timeout=2, user_id=7)
            return other, stuck, settled

        other, stuck, settled = asyncio.run(main())
        assert other is True, "the unblocked user's drain must not be held up"
        assert stuck is False, "a drain that gave up has to say so, not look settled"
        assert settled is True

    def test_finished_tasks_are_forgotten(self, monkeypatch):
        release = threading.Event()
        release.set()
        monkeypatch.setattr("pi.memory.service.extract_facts", self._gate(release))
        svc = _service()

        async def main():
            await svc.setup()
            for uid in (7, 8, 9):
                self._spawn(svc, uid, "习惯")
            await svc.drain(timeout=2)
            await asyncio.sleep(0.01)  # done callbacks are scheduled, not inline
            return len(svc._tasks), len(svc._task_users)

        tasks, task_users = asyncio.run(main())
        assert (tasks, task_users) == (0, 0), (
            "the task->user map has to be cleaned up with the task, or a "
            "long-lived process grows it without bound"
        )


class TestLoopInjection:
    """The injection contract, which is where a mistake corrupts real requests."""

    MEMORY_TEXT = f"{MEMORY_HEADER}\n- (preference) 用 uv\n{MEMORY_FOOTER}"

    #: Stands in for MemoryService.retrieve_traced's third element. Only the keys
    #: loop.py reads back out into a RetrievalEvent - the fakes here are about the
    #: injection contract, not about recall accounting.
    STATS = {"ok": True, "outcome": "injected", "kept": 1}

    def _loop(self, provider, cwd, retrieve):
        return AgentLoop(
            provider=provider,
            tools=all_tools(),
            system_prompt="SYSTEM",
            messages=[],
            cwd=cwd,
            retrieve_context=retrieve,
        )

    def test_memory_goes_to_index_zero_of_the_outbound_copy_only(self, tmp_path):
        provider = _Recording()
        queries: list[str] = []

        async def retrieve(query: str) -> tuple[str, Usage, dict]:
            queries.append(query)
            return self.MEMORY_TEXT, Usage(input_tokens=3), self.STATS

        async def main():
            agent = self._loop(provider, tmp_path, retrieve)
            async for _ in agent.run("帮我装依赖"):
                pass
            return agent.messages

        messages = asyncio.run(main())
        assert queries == ["帮我装依赖"]  # the prompt, once
        sent = provider.seen[0][1]
        assert sent[0].role is Role.user
        assert sent[0].blocks[0].text == self.MEMORY_TEXT
        assert sent[1].blocks[0].text == "帮我装依赖"
        # ...and it is not in the history that gets persisted
        persisted = [b.text for m in messages for b in m.blocks if isinstance(b, TextBlock)]
        assert self.MEMORY_TEXT not in persisted

    def test_the_system_prompt_is_never_used_for_memory(self, tmp_path):
        """loop.py hands system_prompt to the provider raw - it bypasses redaction."""
        provider = _Recording()

        async def retrieve(query: str) -> tuple[str, Usage, dict]:
            return self.MEMORY_TEXT, Usage(), self.STATS

        async def main():
            agent = self._loop(provider, tmp_path, retrieve)
            async for _ in agent.run("hi"):
                pass

        asyncio.run(main())
        assert provider.seen[0][0] == "SYSTEM"

    def test_retrieval_usage_is_folded_into_the_turn(self, tmp_path):
        provider = _Recording()

        async def retrieve(query: str) -> tuple[str, Usage, dict]:
            return "", Usage(input_tokens=11), {"ok": True, "outcome": "no_hits", "kept": 0}

        async def main():
            agent = self._loop(provider, tmp_path, retrieve)
            return [ev async for ev in agent.run("hi")]

        ends = [ev for ev in asyncio.run(main()) if isinstance(ev, TurnEndEvent)]
        assert ends[0].usage.input_tokens == 12  # 11 embedding + 1 from the model

    def test_it_is_retrieved_once_even_across_tool_round_trips(self, tmp_path):
        provider = _Recording(
            responses=[
                [ToolCallBlock(id="c1", name="ls", arguments='{"path": "."}')],
                [TextBlock(text="done")],
            ]
        )
        calls = 0

        async def retrieve(query: str) -> tuple[str, Usage, dict]:
            nonlocal calls
            calls += 1
            return self.MEMORY_TEXT, Usage(), self.STATS

        async def main():
            agent = self._loop(provider, tmp_path, retrieve)
            return [ev async for ev in agent.run("列一下目录")]

        events = asyncio.run(main())
        assert calls == 1  # not once per turn of the loop
        assert len(provider.seen) == 2
        # Both requests carry the memory message, and StrictFakeProvider accepted
        # both - so the tool_call/tool_result adjacency survived the injection.
        assert all(m[0].blocks[0].text == self.MEMORY_TEXT for _, m in provider.seen)
        assert any(getattr(ev, "name", "") == "ls" for ev in events)

    def test_a_failing_retrieval_does_not_fail_the_run(self, tmp_path):
        provider = _Recording()

        async def retrieve(query: str) -> tuple[str, Usage, dict]:
            raise RuntimeError("milvus is down")

        async def main():
            agent = self._loop(provider, tmp_path, retrieve)
            return [ev async for ev in agent.run("hi")]

        events = asyncio.run(main())
        assert not [ev for ev in events if isinstance(ev, ErrorEvent)]
        assert [ev for ev in events if isinstance(ev, TurnEndEvent)]
        assert provider.seen[0][1][0].blocks[0].text == "hi"  # nothing injected

    def test_no_callback_means_no_change_at_all(self, tmp_path):
        provider = _Recording()

        async def main():
            agent = self._loop(provider, tmp_path, None)
            async for _ in agent.run("hi"):
                pass

        asyncio.run(main())
        assert [b.text for b in provider.seen[0][1][0].blocks] == ["hi"]
        assert len(provider.seen[0][1]) == 1
