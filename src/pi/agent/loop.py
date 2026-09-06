"""The agent loop (pi-agent-core's agent-loop, in asyncio).

Flow for one user turn:
1. append user message
2. stream one assistant turn from the provider
3. if the assistant requested tool calls -> execute them in order, append tool
   results, goto 2. A tool marked Tool.terminal ends the loop as soon as it
   succeeds: the calls after it are answered with a synthesized error result and
   never execute.
4. else -> done, emit TurnEndEvent
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.compaction import compact, estimate_size
from pi.agent.events import (
    AgentEvent,
    CompactionEvent,
    ErrorEvent,
    LlmCallEvent,
    PlanEvent,
    RetrievalEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from pi.llm.base import LLMProvider, StreamEnd, TextDelta, ToolCallDelta
from pi.models import (
    FileBlock,
    Message,
    Plan,
    Role,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
    Usage,
)
from pi.observability.tracing import NoOpTracer, Tracer
from pi.security.audit import AuditLogger
from pi.security.policy import Policy, check as policy_check
from pi.security.redact import redact_messages, redact_text
from pi.tools.base import Tool, ToolContext

MessageCallback = Callable[[Message], None]

#: prompt -> (text to inject, the usage it cost, what the retrieval did). The
#: server layer supplies this from MemoryService.retrieve_traced. Expressed as a
#: callable rather than importing pi.memory so the loop stays unaware of where
#: facts live; the third element is a dict of primitives for the same reason, and
#: the loop forwards it into a RetrievalEvent without interpreting it.
RetrieveContext = Callable[[str], Awaitable[tuple[str, Usage, dict[str, Any]]]]

PREVIEW_LEN = 200

# Fed back for the calls a terminal tool cut short; {tool} is that tool's name, so
# the loop still hardcodes none. Deliberately short enough to survive the
# PREVIEW_LEN cut whole, and explicit that nothing happened: next turn the model
# reads this in its own history and has to be able to tell "never ran" from "maybe
# ran halfway and failed".
SKIPPED_RESULT = (
    "Error: skipped - {tool} ended this turn. This tool call did not execute "
    "and had no side effects."
)


def _args_or_empty(call: ToolCallBlock) -> dict[str, Any]:
    """Best-effort arguments, for auditing a call that never reached _run_tool."""
    try:
        args = json.loads(call.arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


@dataclass
class _ToolOutcome:
    block: ToolResultBlock
    name: str
    terminal: bool = False
    payload: Any = None
    server_executed: bool = False


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
        on_compact: "Callable[[list[Message]], None] | None" = None,
        retrieve_context: RetrieveContext | None = None,
        server_tools: list[str] | None = None,
    ):
        self.provider = provider
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.system_prompt = system_prompt
        self.messages: list[Message] = messages if messages is not None else []
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
        self.retrieve_context = retrieve_context
        self.server_tools = set(server_tools or [])

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in self.tools.values()
        ]

    def _append(self, msg: Message) -> None:
        self.messages.append(msg)
        if self.on_message is not None:
            self.on_message(msg)

    async def run(
        self, user_text: str, files: list[FileBlock] | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop until the assistant stops requesting tools.

        `files` are attachments uploaded for this turn: they ride in the user
        message itself, so history replay and persistence keep them without a
        parallel channel that could drift out of sync.
        """
        blocks: list[TextBlock | FileBlock] = [TextBlock(text=user_text)]
        blocks.extend(files or [])
        self._append(Message(role=Role.user, blocks=blocks))

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

        total = Usage()
        turns = 0

        # Retrieved once per turn, not once per tool round-trip: the facts cannot
        # change while this loop runs, and re-querying would pay an embedding call
        # plus a vector search for every single tool call. Held in a local and spliced
        # into the outbound copy below instead of into self.messages, so memory is
        # never persisted, never folded into a compaction summary and never counted by
        # estimate_size.
        memory_msg: Message | None = None
        if self.retrieve_context is not None:
            retrieved_at = time.perf_counter()
            text = ""
            stats: dict[str, Any] = {}
            try:
                text, usage, stats = await self.retrieve_context(user_text)
            except Exception as exc:  # noqa: BLE001 - retrieval must not break the run
                logging.getLogger("pi.agent").exception("memory retrieval failed")
                # MemoryService guards every stage itself, so getting here means the
                # callable was not MemoryService - record it rather than lose it.
                stats = {
                    "ok": False,
                    "outcome": "raised",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                total = total.add(usage)
                if text:
                    memory_msg = Message(role=Role.user, blocks=[TextBlock(text=text)])
            # Emitted on every path, success included: the injected text never
            # reaches the transcript, so this event is the only record that the
            # model saw a memory at all - and of which candidates it did not see.
            yield RetrievalEvent(
                ok=bool(stats.get("ok", False)),
                outcome=str(stats.get("outcome", "")),
                error=str(stats.get("error", "")),
                kept=int(stats.get("kept", 0)),
                injected_chars=len(text),
                duration_ms=round((time.perf_counter() - retrieved_at) * 1000, 1),
                stats=stats,
            )

        try:
            while True:
                turns += 1
                if turns > self.max_turns:
                    yield ErrorEvent(f"exceeded max_turns={self.max_turns}, aborting")
                    break

                text_parts: list[str] = []
                by_id: dict[str, dict[str, str]] = {}
                stop_reason = "end_turn"

                outbound = self.messages
                if self.policy is not None and self.policy.redact:
                    outbound = redact_messages(self.messages)
                if memory_msg is not None:
                    # Index 0 is the only safe position: providers reject a request
                    # whose assistant tool_call has no adjacent tool_result, so this
                    # message must never land between a call and its answer. A new
                    # list, never insert(0, ...) - without redaction `outbound` *is*
                    # self.messages, and mutating it would persist the memory.
                    outbound = [memory_msg, *outbound]

                model = str(getattr(self.provider, "model", ""))
                call_started = time.perf_counter()
                turn_usage = Usage()

                def llm_call_event(ok: bool, error: str = "") -> LlmCallEvent:
                    return LlmCallEvent(
                        turn=turns,
                        model=model,
                        ok=ok,
                        error=error,
                        stop_reason=stop_reason,
                        input_tokens=turn_usage.input_tokens,
                        output_tokens=turn_usage.output_tokens,
                        duration_ms=round((time.perf_counter() - call_started) * 1000, 1),
                    )

                try:
                    with self.tracer.track(
                        "llm.call", {"model": model, "turn": turns}
                    ) as call:
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
                                turn_usage = ev.usage
                                total = total.add(ev.usage)
                        call.set_attribute("stop_reason", stop_reason)
                        call.set_attribute("input_tokens", turn_usage.input_tokens)
                        call.set_attribute("output_tokens", turn_usage.output_tokens)
                except Exception as exc:  # noqa: BLE001 - the outer handler reports it
                    # Yielded before the re-raise: the ErrorEvent the outer handler
                    # emits says a run broke, this says which of its round-trips
                    # broke. Letting the raise skip the yield would leave the trace
                    # describing every turn except the one that matters.
                    yield llm_call_event(False, f"{type(exc).__name__}: {exc}")
                    raise
                yield llm_call_event(True)

                calls = [
                    ToolCallBlock(id=cid, name=slot["name"], arguments=slot["arguments"])
                    for cid, slot in by_id.items()
                ]

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
                ended_by: _ToolOutcome | None = None
                for call in calls:
                    if ended_by is None:
                        if call.name in self.server_tools:
                            outcome = self._server_tool(call)
                        else:
                            outcome = await self._run_tool(call)
                        # A terminal tool ends the turn only when it succeeded: a
                        # rejected or malformed submit_plan feeds its error back and
                        # the turn keeps going, so the model can fix its arguments
                        # instead of leaving the user with neither plan nor answer.
                        if outcome.terminal and not outcome.block.is_error:
                            ended_by = outcome
                    else:
                        # Never routed through _run_tool - the whole point is that
                        # tool.execute is not reached. Skipped without a policy
                        # check too: policy gates execution, and a call that never
                        # executes has nothing to gate.
                        reason = f"skipped: {ended_by.name} ended the turn"
                        outcome = _ToolOutcome(
                            block=ToolResultBlock(
                                tool_use_id=call.id,
                                content=SKIPPED_RESULT.format(tool=ended_by.name),
                                is_error=True,
                            ),
                            name=call.name,
                        )
                        self._audit(
                            call.name,
                            _args_or_empty(call),
                            ok=None,
                            preview=outcome.block.content,
                            allowed=False,
                            reason=reason,
                        )
                    outcomes.append(outcome)
                    preview = outcome.block.content[:PREVIEW_LEN].replace("\n", " ")
                    if not preview and outcome.server_executed:
                        # the wire result is empty by design (the gateway executes
                        # the tool on receipt); the UI deserves to know that.
                        preview = "(executed by the model gateway)"
                    yield ToolCallEndEvent(
                        id=call.id,
                        name=outcome.name,
                        ok=not outcome.block.is_error,
                        result=preview,
                    )
                # One result block per call, in call order. Providers reject a
                # request whose assistant tool_calls are not all answered, and they
                # reject it on the *next* turn - so the synthesized skips above are
                # what keeps this history loadable.
                self._append(
                    Message(role=Role.user, blocks=[o.block for o in outcomes])
                )
                if ended_by is not None:
                    if isinstance(ended_by.payload, Plan):
                        yield PlanEvent(plan=ended_by.payload)
                    break
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            yield ErrorEvent(f"{type(exc).__name__}: {exc}")

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
        self.messages = new_messages
        if self.on_compact is not None:
            try:
                self.on_compact(self.messages)
            except Exception:  # noqa: BLE001 - persistence must not break the run
                logging.getLogger("pi.agent").exception("on_compact callback failed")
        yield CompactionEvent(
            dropped=dropped,
            chars_before=size,
            chars_after=after,
        )

    def _server_tool(self, call: ToolCallBlock) -> _ToolOutcome:
        """Answer a gateway-executed tool call with an EMPTY result.

        The OpenAI-compatible endpoint runs these tools itself once it receives
        the empty tool result, so there is nothing local to execute, gate or
        sandbox - which is why this never routes through _run_tool. The call is
        still audited: it names a capability and carries model-chosen arguments,
        and the audit log is the only place the round-trip is visible.
        """
        self._audit(call.name, _args_or_empty(call), ok=None, preview="(executed by the model gateway)")
        return _ToolOutcome(
            block=ToolResultBlock(tool_use_id=call.id, content="", is_error=False),
            name=call.name,
            server_executed=True,
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
            return _ToolOutcome(block=block, name=call.name)

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
                return _ToolOutcome(block=block, name=call.name)

            span.set_attribute("ok", not result.is_error)
            block = ToolResultBlock(
                tool_use_id=call.id,
                content=result.content,
                is_error=result.is_error,
            )
            self._audit(call.name, args, ok=not result.is_error, preview=result.content)
            # terminal/payload only travel out of this path: an unknown tool, bad
            # arguments, a policy denial or a crash all return early with the
            # dataclass defaults, so a failed terminal tool cannot end the turn.
            return _ToolOutcome(
                block=block,
                name=call.name,
                terminal=tool.terminal,
                payload=result.payload,
            )

    def _audit(
        self,
        tool: str,
        args: dict,
        ok: bool | None,
        preview: str,
        allowed: bool = True,
        reason: str = "",
    ) -> None:
        if self.audit is None:
            return
        if self.policy is not None and self.policy.redact:
            args = {k: (redact_text(str(v)) if isinstance(v, str) else v) for k, v in args.items()}
        self.audit.tool_call(
            session_id=self.session_id,
            user_id=self.user_id,
            tool=tool,
            args=args,
            decision_allowed=allowed,
            decision_reason=reason,
            ok=ok,
            result_preview=preview[:400],
        )
