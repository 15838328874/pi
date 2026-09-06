"""Model-native capabilities: FileBlock, endpoint-side tools, attachments.

Three layers are pinned here. The wire layer: a user message carrying files
translates to the content-array form the gateway demands, and the capability
flags reach the request (extra_body / bare tool types). The loop layer: a
call to a gateway-executed tool is answered with an EMPTY tool result - the
gateway runs the tool when it receives that empty answer - while history
stays paired. The integration layer lives in test_server.py (TestSessionFiles).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pi.agent.events import ToolCallEndEvent, TurnEndEvent
from pi.agent.loop import AgentLoop
from pi.llm.openai_provider import BUILTIN_TOOL_TYPES, OpenAIProvider
from pi.llm.registry import resolve
from pi.models import FileBlock, Message, Role, TextBlock, ToolCallBlock, ToolResultBlock
from pi.security.audit import AuditLogger
from pi.tools import all_tools
from conftest import StrictFakeProvider


def tc(id: str, name: str, args: dict | str) -> ToolCallBlock:
    return ToolCallBlock(
        id=id, name=name, arguments=args if isinstance(args, str) else json.dumps(args)
    )


def _run(loop: AgentLoop, prompt: str, files: list[FileBlock] | None = None) -> list:
    async def main():
        return [ev async for ev in loop.run(prompt, files=files)]

    return asyncio.run(main())


def _tool_ends(events: list) -> list[ToolCallEndEvent]:
    return [e for e in events if isinstance(e, ToolCallEndEvent)]


class TestFileBlocksOnTheWire:
    def test_a_user_message_with_files_becomes_a_content_array(self):
        msg = Message(
            role=Role.user,
            blocks=[
                TextBlock(text="总结一下"),
                FileBlock(file_url="http://x/a.pdf", name="a.pdf"),
            ],
        )
        wire = OpenAIProvider._to_wire("sys", [msg])
        assert wire[1]["content"] == [
            {"type": "file", "file": {"file_url": "http://x/a.pdf"}},
            {"type": "text", "text": "总结一下"},
        ]

    def test_a_plain_user_message_stays_a_string(self):
        msg = Message(role=Role.user, blocks=[TextBlock(text="hi")])
        wire = OpenAIProvider._to_wire("sys", [msg])
        assert wire[1]["content"] == "hi"

    def test_tool_results_still_ride_as_role_tool_messages(self):
        msg = Message(
            role=Role.user,
            blocks=[ToolResultBlock(tool_use_id="c1", content="")],
        )
        wire = OpenAIProvider._to_wire("sys", [msg])
        assert wire[1] == {"role": "tool", "tool_call_id": "c1", "content": ""}


def _chunk(content=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice], usage=usage)


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.captured: dict = {}

    async def create(self, **kwargs):
        self.captured = kwargs

        async def gen():
            for c in self._chunks:
                yield c

        return gen()


def _provider_with_client(chunks) -> tuple[OpenAIProvider, _FakeCompletions]:
    p = OpenAIProvider(
        model="qwen-flash", api_key="k", base_url="http://localhost:9/v1"
    )
    comps = _FakeCompletions(chunks)
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=comps))
    return p, comps


class TestCapabilityFlagsReachTheRequest:
    def test_enable_search_lands_in_extra_body(self):
        p, comps = _provider_with_client(
            [_chunk(content="答", finish="stop", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))]
        )
        p.enable_search = True
        events = asyncio.run(
            _collect(p.stream("s", [Message(role=Role.user, blocks=[TextBlock(text="q")])], []))
        )
        assert events
        # forced_search: without it the gateway skips searching unless the model
        # feels like it - the toggle must be deterministic.
        assert comps.captured["extra_body"] == {
            "enable_search": True,
            "search_options": {"forced_search": True},
        }

    def test_builtin_tools_land_as_bare_type_entries(self):
        p, comps = _provider_with_client([_chunk(finish="stop")])
        p.builtin_tools = ["web_search", "code_interpreter"]
        asyncio.run(_collect(p.stream("s", [], [])))
        assert {"type": "web_search"} in comps.captured["tools"]
        assert {"type": "code_interpreter"} in comps.captured["tools"]

    def test_no_capabilities_send_nothing_extra(self):
        p, comps = _provider_with_client([_chunk(finish="stop")])
        asyncio.run(_collect(p.stream("s", [], [])))
        assert comps.captured["extra_body"] is None
        assert comps.captured["tools"] is None

    def test_unknown_builtin_types_are_dropped_at_construction(self):
        p = OpenAIProvider(
            model="m",
            api_key="k",
            base_url="http://localhost:9/v1",
            builtin_tools=["web_search", "made_up"],
        )
        assert p.builtin_tools == ["web_search"]

    def test_registry_forwards_the_flags(self):
        p = resolve(
            "openai/qwen-flash",
            api_key="k",
            base_url="http://localhost:9/v1",
            enable_search=True,
            builtin_tools=["web_extractor"],
        )
        assert isinstance(p, OpenAIProvider)
        assert p.enable_search and p.builtin_tools == ["web_extractor"]
        assert set(BUILTIN_TOOL_TYPES) == {
            "web_search", "web_extractor", "code_interpreter"
        }


async def _collect(aiter):
    out = []
    async for ev in aiter:
        out.append(ev)
    return out


class TestServerExecutedTools:
    """The loop's half of the gateway round trip."""

    def _loop(self, tmp_path: Path, responses, audit_path: Path | None = None) -> AgentLoop:
        audit = AuditLogger(audit_path) if audit_path else None
        return AgentLoop(
            provider=StrictFakeProvider(responses=responses),
            tools=all_tools(),
            server_tools=["web_search", "web_extractor", "code_interpreter"],
            cwd=tmp_path,
            audit=audit,
        )

    def test_the_call_is_answered_with_an_empty_result(self, tmp_path: Path):
        loop = self._loop(
            tmp_path,
            [
                [tc("c1", "web_search", {"query": "杭州天气"})],
                [TextBlock(text="明天多云")],
            ],
        )
        events = _run(loop, "天气")
        ends = _tool_ends(events)
        assert len(ends) == 1 and ends[0].ok
        assert ends[0].result == "(executed by the model gateway)"

        results = [
            b for b in loop.messages if b.role == Role.user for b in b.blocks
            if isinstance(b, ToolResultBlock)
        ]
        assert results == [
            ToolResultBlock(tool_use_id="c1", content="", is_error=False)
        ]

    def test_history_stays_paired_for_the_next_request(self, tmp_path: Path):
        # StrictFakeProvider validates call/result pairing on every stream();
        # a server-tool round that left the call unanswered would raise here.
        loop = self._loop(
            tmp_path,
            [
                [tc("c1", "code_interpreter", {"code": "2**100"})],
                [TextBlock(text="done")],
            ],
        )
        events = _run(loop, "算一下")
        assert isinstance(events[-1], TurnEndEvent)
        assert events[-1].turns == 2

    def test_the_call_is_audited(self, tmp_path: Path):
        audit_path = tmp_path / "audit.jsonl"
        loop = self._loop(
            tmp_path,
            [
                [tc("c1", "web_extractor", {"url": "https://example.com"})],
                [TextBlock(text="ok")],
            ],
            audit_path=audit_path,
        )
        _run(loop, "抓取")
        # AuditLogger rotates daily: audit.jsonl -> audit-YYYY-MM-DD.jsonl
        audit_file = next(audit_path.parent.glob("audit-*.jsonl"))
        records = [json.loads(l) for l in audit_file.read_text().splitlines()]
        assert any(
            r["event"] == "tool_call"
            and r["tool"] == "web_extractor"
            and r["ok"] is None
            for r in records
        )

    def test_a_disabled_server_tool_is_still_just_unknown(self, tmp_path: Path):
        loop = AgentLoop(
            provider=StrictFakeProvider(
                responses=[[tc("c1", "web_search", {"query": "x"})], [TextBlock(text="ok")]]
            ),
            tools=all_tools(),
            cwd=tmp_path,
        )
        events = _run(loop, "q")
        ends = _tool_ends(events)
        assert not ends[0].ok
        assert "unknown tool" in ends[0].result


class TestAttachments:
    def test_files_ride_in_the_persisted_user_message(self, tmp_path: Path):
        loop = AgentLoop(
            provider=StrictFakeProvider(responses=[[TextBlock(text="ok")]]),
            tools=[],
            cwd=tmp_path,
        )
        _run(loop, "总结这个", files=[FileBlock(file_url="http://x/a.pdf", name="a.pdf")])
        assert loop.messages[0].blocks == [
            TextBlock(text="总结这个"),
            FileBlock(file_url="http://x/a.pdf", name="a.pdf"),
        ]

    def test_a_round_trip_through_json_keeps_the_file_block(self, tmp_path: Path):
        loop = AgentLoop(
            provider=StrictFakeProvider(responses=[[TextBlock(text="ok")]]),
            tools=[],
            cwd=tmp_path,
        )
        _run(loop, "q", files=[FileBlock(file_url="http://x/a.pdf", name="a.pdf")])
        again = Message.model_validate_json(loop.messages[0].model_dump_json())
        assert isinstance(again.blocks[1], FileBlock)
        assert again.blocks[1].file_url == "http://x/a.pdf"
