"""Tests for episodic memory (P3): compaction summaries persisted and reused."""

from __future__ import annotations

import asyncio

from pi.agent.loop import AgentLoop
from pi.llm.fake import FakeProvider
from pi.models import Message, Role, TextBlock
from pi.server.db import Database, MessageRepo
from pi.tools import all_tools


def _drain(agent, *args, **kwargs):
    async def main():
        async for _ in agent.run(*args, **kwargs):
            pass

    asyncio.run(main())


def test_compaction_reports_covered_upto_idx(tmp_path):
    provider = FakeProvider(
        model="demo",
        responses=[
            [TextBlock(text="the summary")],  # consumed by compaction()
            [TextBlock(text="final answer")],  # the turn after compaction
        ],
    )
    messages = [
        Message(role=Role.user, blocks=[TextBlock(text=f"m{i}")]) for i in range(4)
    ]
    captured: list = []

    agent = AgentLoop(
        provider=provider,
        tools=all_tools(),
        messages=messages,
        message_idx=[0, 1, 2, 3],
        compact_threshold=1,  # any non-empty history exceeds it -> compacts
        compact_keep=2,
        cwd=tmp_path,
        on_compact=lambda new_msgs, upto: captured.append((new_msgs, upto)),
    )
    _drain(agent, "hi")

    assert len(captured) == 1
    new_msgs, covered_upto_idx = captured[0]
    # the summary covers messages idx 0,1,2 (the last kept tail is idx 3 + new user msg)
    assert covered_upto_idx == 2
    assert "the summary" in new_msgs[0].blocks[0].text


def test_compaction_repo_roundtrip(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")

    async def main():
        await db.init()
        repo = MessageRepo(db)
        for idx, text in enumerate(["m0", "m1", "m2"]):
            blocks = Message(role=Role.user, blocks=[TextBlock(text=text)]).model_dump_json()
            await repo.append_many(
                "s1",
                [{"idx": idx, "role": "user", "blocks": blocks}],
            )

        await repo.save_compaction("s1", covered_upto_idx=1, summary="summary text")
        latest = await repo.latest_compaction("s1")
        assert latest is not None
        assert latest.covered_upto_idx == 1
        assert latest.summary == "summary text"

        # after_idx skips the summarized prefix
        rows = await repo.list_for_session("s1", after_idx=1)
        assert [r.idx for r in rows] == [2]

        # without a filter, everything is still there (non-destructive)
        assert await repo.count_for_session("s1") == 3
        await db.dispose()

    asyncio.run(main())
