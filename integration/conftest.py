"""Real-infrastructure test tier: live LLM gateway, live MySQL (pi_py_test),
live Redis (ns=test), live Milvus (ns=it).

Run explicitly with `pytest integration/` - plain `pytest` stays the offline
unit suite (pyproject's testpaths is tests/ only, and tests/conftest.py is not
on this directory's conftest chain, so its offline pins never apply here).

Environment: .env is loaded first, .env.test second (it wins), exactly like the
documented manual workflow
    set -a; . ./.env; . ./.env.test; set +a
Two guards then refuse to run against anything that is not clearly test
infrastructure, because a mis-edited env file is the one failure mode that
would write into production data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"integration tier requires {path} to exist")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load(ROOT / ".env")
_load(ROOT / ".env.test")

# The guarantee does not depend on an env file someone might edit: the Milvus
# namespace is forced here even though .env.test already carries it.
os.environ["PI_MILVUS_NS"] = "it"

_db_name = os.environ["PI_DATABASE_URL"].rsplit("/", 1)[-1]
if not _db_name.endswith("_test"):
    raise SystemExit(
        f"integration tier refuses to run against database {_db_name!r}: "
        "expected a *_test schema"
    )
if os.environ.get("PI_MILVUS_NS") == "pi":
    raise SystemExit("integration tier refuses to run against the pi Milvus namespace")

# .env keeps fake/demo for cheap local dev; real calls are the entire point of
# this tier. qwen-flash is the measured choice: non-thinking, ~1.5s per turn.
if os.environ.get("PI_MODEL", "").startswith("fake/"):
    os.environ["PI_MODEL"] = "openai/qwen-flash"

import asyncio

import pytest  # noqa: E402  (after the env setup above)

from pi.memory.embed import get_embedder  # noqa: E402
from pi.memory.rerank import get_reranker  # noqa: E402
from pi.memory.service import MemoryService  # noqa: E402
from pi.memory.store import get_store  # noqa: E402
from pi.models import Message, Role, TextBlock  # noqa: E402


def _msg(text: str) -> Message:
    return Message(role=Role.user, blocks=[TextBlock(text=text)])


#: A transcript with five durable facts, the same one the extraction A/B ran on.
T_GOOD = [
    _msg(
        "我们这个项目统一用 uv 管理依赖，别再用 pip 了，lockfile 必须提交。"
        "后端是 FastAPI，数据库是 MySQL 8，ORM 用 SQLAlchemy 2.0 的异步引擎。"
        "测试跑 pytest，但不要引入 pytest-asyncio。我叫林薇，负责后端。"
    ),
    _msg("好的林薇，我记下了这些约定。"),
]


class FixedProvider:
    """Streams a fixed fact list, bypassing gateway nondeterminism.

    Every chat model at this gateway drifts run to run (qwen-plus 3/5, qwen-flash
    1/5 identical consecutive pairs), so re-recording through the real model
    cannot assert the dedup/touch path deterministically. Re-emitting the exact
    stored texts pins cosine at 1.0 and isolates the mechanism under test.
    """

    def __init__(self, texts: list[str]):
        self.texts = texts

    async def stream(self, system, messages, tools):
        import json

        from pi.llm.base import StreamEnd, TextDelta, Usage

        payload = json.dumps(
            [{"text": t, "kind": "fact"} for t in self.texts], ensure_ascii=False
        )
        yield TextDelta(payload)
        yield StreamEnd("end_turn", Usage(input_tokens=1, output_tokens=1))


class Meter:
    """UsageSink recorder: the row shape usage_records is about to receive."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture(scope="session")
def loop():
    """One event loop for the whole session.

    The shared embedder/reranker hold httpx connection pools that bind to the
    loop they first ran on; a second asyncio.run() would reuse pooled sockets
    from a closed loop. Every coroutine in this tier runs on this one loop.
    """
    l = asyncio.new_event_loop()
    yield l
    l.close()


@pytest.fixture(scope="session")
def run(loop):
    """asyncio.run(), but always on the session loop."""

    def _run(coro):
        return loop.run_until_complete(coro)

    return _run


@pytest.fixture(scope="session")
def stack(loop):
    """A real store+embedder+reranker on the throwaway it_memories collection.

    Dropped before AND after the session: before, so a previous crashed run
    cannot leak state in; after, so the cluster is left clean.
    """
    dim = int(os.environ.get("PI_EMBEDDING_DIM", "0") or 0) or 512
    uri = os.environ["PI_MILVUS_URI"]
    assert uri, "integration tier needs PI_MILVUS_URI"
    store = get_store(uri, os.environ.get("PI_MILVUS_TOKEN", ""),
                      namespace=os.environ["PI_MILVUS_NS"], dim=dim)
    embedder = get_embedder(os.environ["PI_EMBEDDING_MODEL"], dim=dim)
    reranker = get_reranker(os.environ.get("PI_RERANK_URL", ""),
                            os.environ.get("PI_RERANK_MODEL", ""))
    assert store.enabled, "Milvus unreachable - get_store degraded to NoOp"
    assert embedder is not None, "PI_EMBEDDING_MODEL is not set"

    async def reset() -> None:
        if await store._call(store._client.has_collection, store._collection):
            await store._call(store._client.drop_collection, store._collection)
        await store.setup(dim=dim)

    loop.run_until_complete(reset())
    yield store, embedder, reranker

    async def teardown() -> None:
        if await store._call(store._client.has_collection, store._collection):
            await store._call(store._client.drop_collection, store._collection)
        await store.close()

    loop.run_until_complete(teardown())


@pytest.fixture()
def service_factory(stack):
    """Builds MemoryServices wired to the session stack; the caller cleans up."""
    store, embedder, reranker = stack

    def build(**over) -> tuple[MemoryService, Meter]:
        meter = Meter()
        kwargs = dict(
            store=store,
            embedder=embedder,
            reranker=reranker,
            memory_model=os.environ.get("PI_MEMORY_MODEL", ""),
            arbiter_model=os.environ.get("PI_MEMORY_ARBITER_MODEL", ""),
            dim=int(os.environ.get("PI_EMBEDDING_DIM", "0") or 0),
            max_facts=100,
            extract_min_chars=0,
            meter=meter,
        )
        kwargs.update(over)
        return MemoryService(**kwargs), meter

    return build
