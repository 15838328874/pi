"""OpenAI wire-format repair: broken tool_calls sequences must not reach the LLM.

A run interrupted mid-tool (timeout / client disconnect / hard restart) can
leave an assistant message with tool_calls persisted but its tool results
missing. Sending that sequence to an OpenAI-compatible endpoint raises 400
"insufficient tool messages following tool_calls message". `_repair_tool_sequence`
patches the gaps with empty tool messages so the request stays valid.
"""

from __future__ import annotations

from pi.llm.openai_provider import OpenAIProvider


def _call(cid: str) -> dict:
    return {"id": cid, "type": "function", "function": {"name": "bash", "arguments": "{}"}}


def test_repair_fills_missing_tool_messages():
    wire = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [_call("A")]},
        {"role": "user", "content": "follow-up"},  # tool result missing before this
    ]
    fixed = OpenAIProvider._repair_tool_sequence(wire)
    assert [e["role"] for e in fixed] == ["system", "user", "assistant", "tool", "user"]
    assert fixed[3]["tool_call_id"] == "A"
    assert fixed[3]["content"] == ""


def test_repair_fills_partial_multi_calls():
    wire = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None, "tool_calls": [_call("A"), _call("B")]},
        {"role": "tool", "tool_call_id": "A", "content": "ok"},
        {"role": "user", "content": "next"},  # B missing
    ]
    fixed = OpenAIProvider._repair_tool_sequence(wire)
    # B's placeholder must sit between A's real result and the user message
    assert fixed[3]["role"] == "tool" and fixed[3]["tool_call_id"] == "B"
    assert fixed[4]["role"] == "user"


def test_repair_complete_sequence_unchanged():
    wire = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [_call("A")]},
        {"role": "tool", "tool_call_id": "A", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert OpenAIProvider._repair_tool_sequence(wire) == wire


def test_repair_trailing_tool_calls():
    wire = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": None, "tool_calls": [_call("A")]},
    ]
    fixed = OpenAIProvider._repair_tool_sequence(wire)
    assert fixed[-1]["role"] == "tool" and fixed[-1]["tool_call_id"] == "A"
