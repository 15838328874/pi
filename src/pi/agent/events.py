"""Agent-level events emitted by the loop to UIs (pi's AgentSessionEvent analogue)."""

from __future__ import annotations

from dataclasses import dataclass

from pi.models import Usage


@dataclass
class TextDeltaEvent:
    text: str


@dataclass
class ThinkingEvent:
    """Streamed reasoning text (<think>…</think>), surfaced separately and not persisted."""

    text: str


@dataclass
class ToolCallStartEvent:
    id: str
    name: str


@dataclass
class ToolCallEndEvent:
    id: str
    name: str
    ok: bool
    result: str  # possibly truncated preview


@dataclass
class CompactionEvent:
    dropped: int  # number of old messages replaced by the summary
    chars_before: int
    chars_after: int


@dataclass
class TurnEndEvent:
    usage: Usage
    turns: int  # number of assistant turns used


@dataclass
class ErrorEvent:
    message: str


AgentEvent = (
    TextDeltaEvent
    | ThinkingEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | CompactionEvent
    | TurnEndEvent
    | ErrorEvent
)
