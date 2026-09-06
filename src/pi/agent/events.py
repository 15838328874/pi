"""Agent-level events emitted by the loop to UIs (pi's AgentSessionEvent analogue)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pi.models import Plan, Usage


@dataclass
class TextDeltaEvent:
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


# RetrievalEvent and LlmCallEvent are trace-only: event_to_sse has no frame for
# either, and the run endpoint drops them rather than sending `event: unknown`.
# They exist because these are the two stages whose behaviour the transcript
# cannot reconstruct. The memory a run injected never enters the messages table
# (loop.py keeps it out of the persisted history on purpose), and a run's token
# total cannot say whether it was one slow round-trip or five fast ones.

@dataclass
class RetrievalEvent:
    """What long-term memory did before the first model call of the run.

    `stats` is MemoryService.retrieve_traced's dict, passed through untouched:
    per-stage milliseconds, which recall path ran, and a per-candidate verdict
    (kept / below_cosine_gate / below_rerank_gate / over_top_k / absent_in_repo).
    Plain data, because this module must not learn what a Fact is - the same
    reason the loop takes a callable instead of importing pi.memory.
    """

    ok: bool  # False only when a stage raised; "nothing relevant" is ok=True
    outcome: str  # injected | no_hits | gated_out | rerank_empty | *_failed | disabled
    error: str
    kept: int  # facts selected for injection
    injected_chars: int  # 0 when nothing was injected
    duration_ms: float
    stats: dict[str, Any]


@dataclass
class LlmCallEvent:
    """One model round-trip, emitted whether it completed or raised."""

    turn: int  # 1-based; the run's turns total is the count of these
    model: str
    ok: bool
    error: str
    stop_reason: str  # end_turn | tool_use | ... ; "" when the call never finished
    input_tokens: int
    output_tokens: int
    duration_ms: float


@dataclass
class PlanEvent:
    # Emitted at most once per run, after the last ToolCallEndEvent and before
    # TurnEndEvent: submit_plan is terminal, so the run ends right after this.
    plan: Plan


@dataclass
class TurnEndEvent:
    usage: Usage
    turns: int  # number of assistant turns used


@dataclass
class ErrorEvent:
    message: str


AgentEvent = (
    TextDeltaEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | CompactionEvent
    | RetrievalEvent
    | LlmCallEvent
    | PlanEvent
    | TurnEndEvent
    | ErrorEvent
)
