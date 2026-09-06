"""Provider-agnostic streaming interface.

Implementations yield, in order:
- TextDelta       (zero or more, in content order)
- ToolCallDelta   (zero or more, each complete: id + name + full JSON arguments)
- StreamEnd       (exactly one, terminating the stream)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pi.models import Message, ToolSpec, Usage


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallDelta:
    id: str
    name: str
    arguments: str


@dataclass
class StreamEnd:
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | ...
    usage: Usage


StreamEvent = TextDelta | ToolCallDelta | StreamEnd


class LLMProvider(ABC):
    """A streaming chat completion backend for one provider/model pair."""

    name: str = "abstract"
    model: str = "abstract"

    @abstractmethod
    def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn for the current conversation state."""
        raise NotImplementedError
