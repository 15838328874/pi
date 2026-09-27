"""Canonical run trajectory — the single source of truth for a run.

P1 of the eval stack: instead of three parallel side-streams (SSE ``AgentEvent``,
``AuditLogger`` jsonl, tracer spans) each recording a partial view of the same run,
the loop appends every step to one append-only, typed event log here.

Every downstream view is a projection of this log:

    audit    = security projection (redacted tool calls + auth events)
    tracing  = span projection (run -> llm.call / tool.call)
    eval     = replay + score
    debug    = replay
    metering = sum of LlmCall usage

The trajectory stores RAW values (tool arguments are not redacted) because it is
the internal record used for evaluation and replay; redaction belongs to the
audit projection, not here.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RunStarted:
    run_id: str
    session_id: str
    user_id: str
    model: str
    cwd: str
    tools: list[str]
    prompt: str
    # wall clock at event creation; the loop passes the START of each LlmCall/
    # ToolCall explicitly so timeline "time" mode shows true wall-clock gaps
    ts: float = field(default_factory=time.time)


@dataclass
class LlmCall:
    """One assistant turn (one provider.stream call)."""

    turn: int
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    latency_ms: int
    text: str  # aggregated assistant text for this turn
    tool_calls: list[dict]  # [{id, name, arguments(raw json string)}]
    ts: float = field(default_factory=time.time)


@dataclass
class ToolCall:
    """One tool execution."""

    call_id: str
    name: str
    arguments: dict | None  # parsed args (None when the model sent malformed JSON)
    result: str
    is_error: bool
    denied: bool  # rejected by the security policy
    latency_ms: int
    ts: float = field(default_factory=time.time)


@dataclass
class Compaction:
    dropped: int
    chars_before: int
    chars_after: int
    ts: float = field(default_factory=time.time)


@dataclass
class RunError:
    message: str
    ts: float = field(default_factory=time.time)


@dataclass
class RunFinished:
    input_tokens: int
    output_tokens: int
    turns: int
    latency_ms: int
    ts: float = field(default_factory=time.time)


class Trajectory:
    """Append-only, JSON-serializable record of one run."""

    def __init__(
        self,
        *,
        session_id: str = "",
        user_id: str = "",
        model: str = "",
        cwd: str = "",
        tools: list[str] | None = None,
        prompt: str = "",
    ):
        self.run_id = uuid.uuid4().hex[:12]
        self.started_at = time.time()
        self.events: list[Any] = [
            RunStarted(
                run_id=self.run_id,
                session_id=session_id,
                user_id=user_id,
                model=model,
                cwd=cwd,
                tools=list(tools or []),
                prompt=prompt,
            )
        ]

    def record(self, event: Any) -> None:
        self.events.append(event)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "events": [
                {"type": type(e).__name__, **asdict(e)} for e in self.events
            ],
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
