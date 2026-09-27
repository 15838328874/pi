"""The agent loop (pi-agent-core's agent-loop, in asyncio).

Flow for one user turn:
1. append user message
2. stream one assistant turn from the provider
3. if the assistant requested tool calls -> execute them, append tool results, goto 2
4. else -> done, emit TurnEndEvent
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from pi.agent.compaction import compact, estimate_size
from pi.agent.trajectory import (
    Compaction,
    LlmCall,
    RunError,
    RunFinished,
    ToolCall,
    Trajectory,
)
from pi.agent.events import (
    AgentEvent,
    CompactionEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from pi.llm.base import LLMProvider, StreamEnd, TextDelta, ToolCallDelta
from pi.models import Message, Role, TextBlock, ToolCallBlock, ToolResultBlock, ToolSpec, Usage
from pi.observability.tracing import NoOpTracer, Tracer
from pi.security.audit import AuditLogger
from pi.security.policy import Policy, check as policy_check
from pi.security.redact import redact_messages, redact_text
from pi.tools.base import Tool, ToolContext

MessageCallback = Callable[[Message], None]

PREVIEW_LEN = 200


@dataclass
class _ToolOutcome:
    block: ToolResultBlock
    name: str
    usage: Usage | None = None  # token usage a tool incurred (sub-agents)
    arguments: dict | None = None  # parsed tool args (None if malformed)
    denied: bool = False  # rejected by the security policy


@dataclass
class Checkpoint:
    """Durable run state: everything needed to resume a run after a step."""

    step: int
    messages: list[Message]
    input_tokens: int
    output_tokens: int
    turns: int

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "messages": [m.model_dump() for m in self.messages],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "turns": self.turns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(
            step=d["step"],
            messages=[Message.model_validate(m) for m in d["messages"]],
            input_tokens=d["input_tokens"],
            output_tokens=d["output_tokens"],
            turns=d["turns"],
        )


class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        tools: list[Tool],
        system_prompt: str = "",
        messages: list[Message] | None = None,
        cwd: Path | None = None,
        on_message: MessageCallback | None = None,
        max_turns: int = 40,
        compact_threshold: int = 80_000,
        compact_keep: int = 8,
        policy: Policy | None = None,
        audit: AuditLogger | None = None,
        session_id: str = "",
        user_id: str = "local",
        tracer: Tracer | None = None,
        on_compact: "Callable[[list[Message], int | None], None] | None" = None,
        on_checkpoint: "Callable[[Checkpoint], None] | None" = None,
        message_idx: list[int | None] | None = None,
    ):
        self.provider = provider
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.messages: list[Message] = messages if messages is not None else []
        self.message_idx: list[int | None] = (
            list(message_idx) if message_idx is not None else [None] * len(self.messages)
        )
        self.ctx = ToolContext(cwd=cwd or Path.cwd())
        self.on_message = on_message
        self.max_turns = max_turns
        self.compact_threshold = compact_threshold
        self.compact_keep = compact_keep
        self.policy = policy
        self.audit = audit
        self.session_id = session_id
        self.user_id = user_id
        self.tracer = tracer or NoOpTracer()
        self.on_compact = on_compact
        self.on_checkpoint = on_checkpoint
        self._resume_usage: Usage | None = None
        self._resume_turns: int = 0
        self.trajectory: Trajectory | None = None
        self._run_started_at = 0.0

        # Expose runtime deps to tools so a tool (spawn_subagents) can delegate to
        # a child AgentLoop with the same model / policy / audit / tracer context.
        self.ctx.provider = self.provider
        self.ctx.policy = self.policy
        self.ctx.audit = self.audit
        self.ctx.tracer = self.tracer
        self.ctx.session_id = self.session_id
        self.ctx.user_id = self.user_id

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in self.tools.values()
        ]

    def _append(self, msg: Message) -> None:
        self.messages.append(msg)
        self.message_idx.append(None)  # new message, not yet persisted (no DB idx)
        if self.on_message is not None:
            self.on_message(msg)

    def _make_checkpoint(self, total: Usage, turns: int) -> Checkpoint:
        return Checkpoint(
            step=turns,
            messages=list(self.messages),
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
            turns=turns,
        )

    async def run(
        self,
        user_text: str = "",
        resume_from: Checkpoint | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop until the assistant stops requesting tools.

        With ``resume_from`` the loop continues from a durable checkpoint instead
        of starting a fresh turn: history, token usage and turn count are restored
        and no new user message is appended.
        """
        if resume_from is not None:
            self.messages = list(resume_from.messages)
            self._resume_usage = Usage(
                input_tokens=resume_from.input_tokens,
                output_tokens=resume_from.output_tokens,
            )
            self._resume_turns = resume_from.turns
        else:
            self._append(Message(role=Role.user, blocks=[TextBlock(text=user_text)]))
            self._resume_usage = None
            self._resume_turns = 0

        self.trajectory = Trajectory(
            session_id=self.session_id,
            user_id=self.user_id,
            model=getattr(self.provider, "model", ""),
            cwd=str(self.ctx.cwd),
            tools=sorted(self.tools.keys()),
            prompt=user_text,
        )
        self._run_started_at = time.perf_counter()

        with self.tracer.track(
            "agent.run",
            {"session": self.session_id, "user": self.user_id},
        ) as run_span:
            async for ev in self._run_inner(user_text):
                yield ev
            run_span.set_attribute("messages", len(self.messages))

    async def _run_inner(self, user_text: str) -> AsyncIterator[AgentEvent]:
        if self.compact_threshold > 0:
            async for ev in self._maybe_compact():
                yield ev

        total = self._resume_usage or Usage()
        turns = self._resume_turns

        try:
            while True:
                turns += 1
                if turns > self.max_turns:
                    yield ErrorEvent(f"exceeded max_turns={self.max_turns}, aborting")
                    break

                text_parts: list[str] = []
                by_id: dict[str, dict[str, str]] = {}
                stop_reason = "end_turn"
                call_usage = Usage()
                t0 = time.perf_counter()

                outbound = self.messages
                if self.policy is not None and self.policy.redact:
                    outbound = redact_messages(self.messages)
                with self.tracer.track(
                    "llm.call", {"model": getattr(self.provider, "model", ""), "turn": turns}
                ):
                    async for ev in self.provider.stream(
                        self.system_prompt, outbound, self.tool_specs
                    ):
                        if isinstance(ev, TextDelta):
                            text_parts.append(ev.text)
                            yield TextDeltaEvent(ev.text)
                        elif isinstance(ev, ToolCallDelta):
                            slot = by_id.setdefault(ev.id, {"name": ev.name, "arguments": ""})
                            slot["name"] = slot["name"] or ev.name
                            slot["arguments"] += ev.arguments
                            yield ToolCallStartEvent(id=ev.id, name=ev.name)
                        elif isinstance(ev, StreamEnd):
                            stop_reason = ev.stop_reason
                            call_usage = ev.usage
                            total = total.add(ev.usage)

                calls = [
                    ToolCallBlock(id=cid, name=slot["name"], arguments=slot["arguments"])
                    for cid, slot in by_id.items()
                ]

                self.trajectory.record(
                    LlmCall(
                        turn=turns,
                        model=getattr(self.provider, "model", ""),
                        input_tokens=call_usage.input_tokens,
                        output_tokens=call_usage.output_tokens,
                        stop_reason=stop_reason,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        text="".join(text_parts),
                        tool_calls=[
                            {"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in calls
                        ],
                    )
                )

                assistant_blocks: list[TextBlock | ToolCallBlock] = []
                joined = "".join(text_parts)
                if joined:
                    assistant_blocks.append(TextBlock(text=joined))
                assistant_blocks.extend(calls)
                if assistant_blocks:
                    self._append(Message(role=Role.assistant, blocks=assistant_blocks))

                if not calls or stop_reason != "tool_use":
                    break

                outcomes: list[_ToolOutcome] = []
                for call in calls:
                    tool_t0 = time.perf_counter()
                    outcome = await self._run_tool(call)
                    outcomes.append(outcome)
                    if outcome.usage is not None:
                        total = total.add(outcome.usage)
                    self.trajectory.record(
                        ToolCall(
                            call_id=call.id,
                            name=outcome.name,
                            arguments=outcome.arguments,
                            result=outcome.block.content,
                            is_error=outcome.block.is_error,
                            denied=outcome.denied,
                            latency_ms=int((time.perf_counter() - tool_t0) * 1000),
                        )
                    )
                    preview = outcome.block.content[:PREVIEW_LEN].replace("\n", " ")
                    yield ToolCallEndEvent(
                        id=call.id,
                        name=outcome.name,
                        ok=not outcome.block.is_error,
                        result=preview,
                    )
                self._append(
                    Message(role=Role.user, blocks=[o.block for o in outcomes])
                )
                if self.on_checkpoint is not None:
                    try:
                        self.on_checkpoint(self._make_checkpoint(total, turns))
                    except Exception:  # noqa: BLE001 - durable state must not break the run
                        logging.getLogger("pi.agent").exception(
                            "on_checkpoint callback failed"
                        )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.trajectory.record(RunError(message=f"{type(exc).__name__}: {exc}"))
            yield ErrorEvent(f"{type(exc).__name__}: {exc}")

        self.trajectory.record(
            RunFinished(
                input_tokens=total.input_tokens,
                output_tokens=total.output_tokens,
                turns=turns,
                latency_ms=int((time.perf_counter() - self._run_started_at) * 1000),
            )
        )
        yield TurnEndEvent(usage=total, turns=turns)

    async def _maybe_compact(self) -> AsyncIterator[AgentEvent]:
        """Compact history if it exceeds the threshold (summary + recent tail)."""
        size = estimate_size(self.messages)
        if size <= self.compact_threshold:
            return
        try:
            new_messages, dropped = await compact(
                self.provider, self.messages, keep_last=self.compact_keep
            )
        except Exception as exc:  # noqa: BLE001
            yield ErrorEvent(f"compaction failed: {type(exc).__name__}: {exc}")
            return
        if dropped == 0:
            return
        after = estimate_size(new_messages)
        old_idx = self.message_idx
        # The summary covers the dropped head; report the DB idx of its last
        # message so the caller can reload [summary] + [messages after that idx].
        covered_upto_idx = (
            old_idx[-self.compact_keep - 1] if len(old_idx) > self.compact_keep else None
        )
        self.messages = new_messages
        # New list = [summary marker (no idx)] + [kept tail (keeps its idxs)].
        self.message_idx = [None] + old_idx[-self.compact_keep:]
        if self.on_compact is not None:
            try:
                self.on_compact(new_messages, covered_upto_idx)
            except Exception:  # noqa: BLE001 - persistence must not break the run
                logging.getLogger("pi.agent").exception("on_compact callback failed")
        self.trajectory.record(
            Compaction(dropped=dropped, chars_before=size, chars_after=after)
        )
        yield CompactionEvent(
            dropped=dropped,
            chars_before=size,
            chars_after=after,
        )

    async def _run_tool(self, call: ToolCallBlock) -> _ToolOutcome:
        tool = self.tools.get(call.name)
        if tool is None:
            block = ToolResultBlock(
                tool_use_id=call.id,
                content=f"Error: unknown tool {call.name!r}",
                is_error=True,
            )
            return _ToolOutcome(block=block, name=call.name)

        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            block = ToolResultBlock(
                tool_use_id=call.id,
                content=f"Error: invalid tool arguments: {exc}",
                is_error=True,
            )
            return _ToolOutcome(block=block, name=call.name)

        decision = policy_check(self.policy, call.name, args, self.ctx.cwd)
        if not decision.allowed:
            content = f"Error: denied by security policy: {decision.reason}"
            if self.audit is not None:
                self.audit.tool_call(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    tool=call.name,
                    args=args,
                    decision_allowed=False,
                    decision_reason=decision.reason,
                    ok=None,
                )
            block = ToolResultBlock(
                tool_use_id=call.id,
                content=content,
                is_error=True,
            )
            return _ToolOutcome(block=block, name=call.name, arguments=args, denied=True)

        with self.tracer.track("tool.call", {"tool": call.name, "session": self.session_id}) as span:
            try:
                result = await tool.execute(args, self.ctx)
            except Exception as exc:  # noqa: BLE001
                span.set_attribute("ok", False)
                block = ToolResultBlock(
                    tool_use_id=call.id,
                    content=f"Error: tool {call.name} crashed: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
                self._audit(call.name, args, ok=False, preview=block.content)
                return _ToolOutcome(block=block, name=call.name, arguments=args)

            span.set_attribute("ok", not result.is_error)
            block = ToolResultBlock(
                tool_use_id=call.id,
                content=result.content,
                is_error=result.is_error,
            )
            self._audit(call.name, args, ok=not result.is_error, preview=result.content)
            return _ToolOutcome(block=block, name=call.name, usage=result.usage, arguments=args)

    def _audit(self, tool: str, args: dict, ok: bool, preview: str) -> None:
        if self.audit is None:
            return
        if self.policy is not None and self.policy.redact:
            args = {k: (redact_text(str(v)) if isinstance(v, str) else v) for k, v in args.items()}
        self.audit.tool_call(
            session_id=self.session_id,
            user_id=self.user_id,
            tool=tool,
            args=args,
            decision_allowed=True,
            ok=ok,
            result_preview=preview[:400],
        )
