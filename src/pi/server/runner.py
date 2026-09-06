"""Agent execution manager: per-session locks, global concurrency cap, timeouts.

Bridges HTTP requests to AgentLoop. Server mode always enforces a security
policy: explicit PI_POLICY file if given, else path_sandbox + redact defaults.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
from pi.agent.loop import AgentLoop, RetrieveContext
from pi.llm.registry import resolve_chain
from pi.memory import MemoryService
from pi.models import FileBlock, Message, Plan, ToolCallBlock, ToolResultBlock, Usage
from pi.observability.metering import UsageTracker
from pi.observability.metrics import Metrics
from pi.observability.tracing import Tracer
from pi.prompt import SYSTEM_PROMPT
from pi.security.audit import AuditLogger
from pi.security.policy import Policy, load_policy
from pi.server.cache import CacheBackend, MemoryBackend
from pi.server.db import AgentRunRepo, MessageRepo, SessionRepo, SessionRow
from pi.tools import all_tools
from pi.tools.sandbox import get_runner

#: Retention passes run at most this often per process, checked on run
#: finalize. Once an hour bounds the extra DELETE to something no user can
#: feel, while still catching a server that was down for days.
RETENTION_INTERVAL_SECONDS = 3600.0

#: Set by the access-log middleware (app.py) per request and read here when a
#: run finalizes, so the trace row carries the same request_id as the JSON log
#: line and the X-Request-Id header. A ContextVar because the run streams
#: inside the request's task; anything wider would cross requests.
request_id_var: ContextVar[str] = ContextVar("pi_request_id", default="")


def trace_flags(
    status: str, turns: int, failed_tools: int, memory_failed: bool = False
) -> list[str]:
    """The anomaly verdict, finalized once per run.

    `status` already covers timeouts and exceptions; the other flags catch the
    quieter shapes: a run that produced nothing (user paid, model answered with
    an empty turn), a tool storm (>=3 failed calls - a loop, a broken
    environment, or a model that cannot stop retrying), and a memory retrieval
    whose stages raised.

    `memory_failed` is deliberately narrow. It flags a retrieval that *broke*,
    never one that found nothing: an empty recall is the correct answer for a
    user with no facts yet, and flagging it would bury the runs where the
    embedder, the index or the reranker was actually down.
    """
    flags: list[str] = []
    if status != "ok":
        flags.append(status)
    if turns <= 0:
        flags.append("empty")
    if failed_tools >= 3:
        flags.append("tool_storm")
    if memory_failed:
        flags.append("memory_failed")
    return flags


#: Events the loop emits for the execution trace alone. Neither is in the SSE
#: contract - no Sse*Data model here, no frame in web/'s KNOWN_EVENTS - so the run
#: endpoint skips them instead of putting `event: unknown` on the wire. They are
#: still recorded: agent_steps is where a retrieval and a per-turn model call
#: become queryable.
TRACE_ONLY_EVENTS = (RetrievalEvent, LlmCallEvent)


class TraceRecorder:
    """Buffers one run's trace in memory; AgentRunRepo.append persists it.

    Lives on the runner side (not in db.py) so the SQL layer stays free of
    agent-event knowledge: observe() is the only place that translates
    AgentEvent -> step rows. TextDelta is deliberately not observed - the
    run's first_idx/last_idx range already points at the full transcript.

    observe() only ever sees the *preview* the SSE contract carries, so
    finalize() re-joins the run's buffered messages to upgrade tool_call steps
    with the full arguments and full results before the one-shot persist.
    """

    def __init__(
        self,
        *,
        run_id: str,
        model: str,
        prompt: str = "",
        enable_search: bool = False,
        builtin_tools: list[str] | None = None,
        metrics: Metrics | None = None,
    ):
        self.run_id = run_id
        self.model = model
        self.prompt = prompt
        self.enable_search = enable_search
        self.builtin_tools = list(builtin_tools or [])
        # A disabled Metrics rather than Optional, for the same reason the loop
        # holds a NoOpTracer: observe() has six branches and a None check in each
        # would say less than the recording itself.
        self.metrics = metrics if metrics is not None else Metrics(enabled=False)
        self.request_id = request_id_var.get()
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.steps: list[dict] = []
        self._tool_starts: dict[str, float] = {}
        self.first_idx: int | None = None
        self.last_idx: int | None = None
        self.status = "ok"
        self.error = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.turns = 0
        self.failed_tools = 0
        self.memory_failed = False

    def observe(self, ev: AgentEvent) -> None:
        if isinstance(ev, ToolCallStartEvent):
            self._tool_starts[ev.id] = time.monotonic()
        elif isinstance(ev, ToolCallEndEvent):
            started = self._tool_starts.pop(ev.id, None)
            duration = (time.monotonic() - started) * 1000 if started else 0.0
            if not ev.ok:
                self.failed_tools += 1
            self.metrics.tool_call(tool=ev.name, ok=ev.ok, duration_s=duration / 1000)
            self.steps.append(
                {
                    "kind": "tool_call",
                    "call_id": ev.id,
                    "name": ev.name,
                    "ok": ev.ok,
                    "args": "",
                    "detail": ev.result,
                    "duration_ms": round(duration, 1),
                }
            )
        elif isinstance(ev, PlanEvent):
            self.steps.append(
                {
                    "kind": "plan",
                    "name": "submit_plan",
                    "ok": True,
                    "args": "",
                    "detail": ev.plan.title,
                    "duration_ms": 0.0,
                }
            )
        elif isinstance(ev, CompactionEvent):
            self.steps.append(
                {
                    "kind": "compaction",
                    "name": "compaction",
                    "ok": True,
                    "args": "",
                    "detail": f"dropped={ev.dropped} chars {ev.chars_before}->{ev.chars_after}",
                    "duration_ms": 0.0,
                }
            )
        elif isinstance(ev, RetrievalEvent):
            # At most one per run: retrieval happens once, before the first model
            # call. args is what went in (the query facts were selected for),
            # detail is the whole account of what came out - the same split
            # tool_call uses. This row is the only record of the memory the model
            # was given, because loop.py never lets the injected text into the
            # transcript, so first_idx..last_idx cannot replay it.
            self.steps.append(
                {
                    "kind": "retrieval",
                    "name": "memory.retrieve",
                    "ok": ev.ok,
                    "args": str(ev.stats.get("query", "")),
                    "detail": json.dumps(ev.stats, ensure_ascii=False, default=str),
                    "duration_ms": ev.duration_ms,
                }
            )
            if not ev.ok:
                self.memory_failed = True
            self.metrics.retrieval(
                outcome=ev.outcome,
                duration_s=ev.duration_ms / 1000,
                kept=ev.kept,
                index_path=str(ev.stats.get("index", "")),
            )
        elif isinstance(ev, LlmCallEvent):
            # One per model round-trip, including the one that raised. The
            # transcript holds what each turn said; this holds what each turn
            # cost and how long it took, which is what separates a slow run from
            # a looping one - the run's totals are identical for both.
            self.steps.append(
                {
                    "kind": "llm_call",
                    "name": ev.model,
                    "ok": ev.ok,
                    "args": "",
                    "detail": json.dumps(
                        {
                            "turn": ev.turn,
                            "model": ev.model,
                            "stop_reason": ev.stop_reason,
                            "input_tokens": ev.input_tokens,
                            "output_tokens": ev.output_tokens,
                            "duration_ms": ev.duration_ms,
                            "error": ev.error,
                        },
                        ensure_ascii=False,
                    ),
                    "duration_ms": ev.duration_ms,
                }
            )
            self.metrics.llm_call(
                model=ev.model, ok=ev.ok, duration_s=ev.duration_ms / 1000
            )
        elif isinstance(ev, ErrorEvent):
            self.status = "timeout" if ev.message.startswith("run timed out") else "error"
            self.error = ev.message
            self.steps.append(
                {
                    "kind": "error",
                    "name": "",
                    "ok": False,
                    "args": "",
                    "detail": ev.message,
                    "duration_ms": 0.0,
                }
            )
        elif isinstance(ev, TurnEndEvent):
            self.input_tokens = ev.usage.input_tokens
            self.output_tokens = ev.usage.output_tokens
            self.turns = ev.turns

    def finalize(self, messages: list[Message], first_idx: int | None, last_idx: int | None) -> None:
        """Upgrade the buffered steps with full fidelity, using the very
        messages this run is about to persist: the block layer holds complete
        tool arguments and complete tool results, where the events only carried
        previews. Called once, right before AgentRunRepo.append."""
        self.first_idx = first_idx
        self.last_idx = last_idx
        args_by_call: dict[str, str] = {}
        result_by_call: dict[str, str] = {}
        for m in messages:
            for b in m.blocks:
                if isinstance(b, ToolCallBlock):
                    args_by_call[b.id] = b.arguments
                elif isinstance(b, ToolResultBlock):
                    result_by_call[b.tool_use_id] = b.content
        for step in self.steps:
            if step["kind"] != "tool_call":
                continue
            call_id = step.get("call_id", "")
            if call_id in args_by_call:
                step["args"] = args_by_call[call_id]
            # The full result replaces the preview when the run actually
            # produced one; a server-executed tool's empty result keeps the
            # "(executed by the model gateway)" marker, which says more.
            full = result_by_call.get(call_id, "")
            if full:
                step["detail"] = full

    def flags(self) -> list[str]:
        return trace_flags(self.status, self.turns, self.failed_tools, self.memory_failed)


def server_policy(policy_path: str) -> Policy:
    """Server-mode policy: a PI_POLICY file adds rules, it cannot subtract isolation.

    Policy.from_dict defaults path_sandbox and redact to False, so a file listing
    only deny patterns would silently switch off the workspace sandbox and secret
    redaction - the opposite of what adding a policy is meant to do. Force both on.
    """
    if not policy_path:
        return Policy(path_sandbox=True, redact=True)
    loaded = load_policy(policy_path)
    if loaded is None:
        return Policy(path_sandbox=True, redact=True)
    return replace(loaded, path_sandbox=True, redact=True)


class RunManager:
    """Serializes turns per session and caps global concurrency."""

    def __init__(
        self,
        *,
        policy: Policy,
        audit: AuditLogger,
        max_concurrent: int,
        timeout_seconds: int,
        usage: UsageTracker | None = None,
        tracer: Tracer | None = None,
        cache: CacheBackend | None = None,
        sandbox: str = "",
        sandbox_image: str = "python:3.12-slim",
        memory: MemoryService | None = None,
        traces: AgentRunRepo | None = None,
        trace_retention_days: int = 30,
        metrics: Metrics | None = None,
    ):
        self.policy = policy
        self.audit = audit
        self.usage = usage
        self.tracer = tracer
        self.timeout = timeout_seconds
        self.cache = cache or MemoryBackend()
        self.sandbox = sandbox
        self.sandbox_image = sandbox_image
        self.sandbox_network = False
        self.memory = memory
        self.traces = traces
        self.trace_retention_days = trace_retention_days
        self.metrics = metrics if metrics is not None else Metrics(enabled=False)
        self._last_retention = 0.0
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _retrieve_for(self, user_id: int) -> RetrieveContext | None:
        """Bind memory retrieval to one user, or None when memory is off.

        The loop takes a plain (query) -> (text, usage, stats) callable and never
        learns that user scoping exists, so the tenant boundary is applied here rather
        than left for the agent layer to remember.

        retrieve_traced, not retrieve: the third element is what turns "the model
        forgot what I told it" into an answerable question, and this closure is the
        only path it has out of pi.memory.
        """
        memory = self.memory
        # Configured, not merely constructed. An install with no vector database
        # would otherwise record a "disabled" retrieval step on every run it ever
        # serves; a configured-but-broken one still gets its step, because that is
        # precisely the run somebody needs explained.
        if memory is None or not memory.configured:
            return None

        async def retrieve(query: str) -> tuple[str, Usage, dict[str, Any]]:
            return await memory.retrieve_traced(user_id, query)

        return retrieve

    async def run_turn(
        self,
        *,
        session: SessionRow,
        username: str,
        user_id: int,
        prompt: str,
        model: str,
        message_repo: MessageRepo,
        session_repo: SessionRepo,
        enable_search: bool = False,
        builtin_tools: list[str] | None = None,
        files: list[FileBlock] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute one user turn; messages are persisted on completion.

        enable_search/builtin_tools switch on the endpoint's model-native
        capabilities for this run; files are this turn's uploads, served from
        public URLs the gateway fetches itself.
        """
        # distributed session lock: correct across instances when Redis-backed
        lock_key = f"session:{session.id}"
        if not await self.cache.acquire_lock(lock_key, ttl_seconds=self.timeout + 60):
            yield ErrorEvent(message="another turn is already running for this session")
            return

        try:
            # in_flight spans the semaphore hold, not the lock wait: a run queued
            # behind another is not executing, and counting it would make the gauge
            # say "saturated" about a queue.
            async with self._semaphore, self.metrics.in_flight():
                buffer: list[Message] = []

                def on_message(msg: Message) -> None:
                    buffer.append(msg)

                history = [
                    Message.model_validate_json(row.blocks)
                    for row in await message_repo.list_for_session(session.id)
                ]

                provider = resolve_chain(
                    model, enable_search=enable_search, builtin_tools=builtin_tools
                )
                agent = AgentLoop(
                    provider=provider,
                    tools=all_tools(),
                    system_prompt=SYSTEM_PROMPT,
                    messages=history,
                    cwd=Path(session.cwd),
                    on_message=on_message,
                    policy=self.policy,
                    audit=self.audit,
                    session_id=session.id,
                    user_id=username,
                    tracer=self.tracer,
                    retrieve_context=self._retrieve_for(user_id),
                    server_tools=builtin_tools,
                )
                if self.sandbox:
                    runner = get_runner(
                        self.sandbox,
                        image=self.sandbox_image,
                        allow_network=self.sandbox_network,
                    )
                    agent.ctx.runner = runner
                    # preheat the warm container while the LLM is thinking
                    # about its first tool call (no-op for non-pooled runners)
                    prewarm = getattr(runner, "prewarm", None)
                    if callable(prewarm):
                        try:
                            prewarm(Path(session.cwd))
                        except Exception:  # noqa: BLE001 - prewarm is best-effort
                            log.debug("sandbox prewarm failed", exc_info=True)

                final_usage = None
                final_turns = 0
                plan: Plan | None = None
                recorder = (
                    TraceRecorder(
                        run_id=uuid.uuid4().hex[:12],
                        model=model,
                        prompt=prompt,
                        enable_search=enable_search,
                        builtin_tools=builtin_tools,
                        metrics=self.metrics,
                    )
                    if self.traces is not None
                    else None
                )

                async def _stream():
                    async with asyncio.timeout(self.timeout):
                        async for ev in agent.run(prompt, files=files):
                            yield ev

                try:
                    async for ev in _stream():
                        if recorder is not None:
                            recorder.observe(ev)
                        if isinstance(ev, TurnEndEvent):
                            final_usage = ev.usage
                            final_turns = ev.turns
                        elif isinstance(ev, PlanEvent):
                            plan = ev.plan
                        yield ev
                except TimeoutError:
                    timeout_ev = ErrorEvent(message=f"run timed out after {self.timeout}s")
                    if recorder is not None:
                        recorder.observe(timeout_ev)
                    yield timeout_ev
                except Exception as exc:  # noqa: BLE001
                    error_ev = ErrorEvent(message=f"{type(exc).__name__}: {exc}")
                    if recorder is not None:
                        recorder.observe(error_ev)
                    yield error_ev

                first_idx: int | None = None
                last_idx: int | None = None
                if buffer:
                    base_idx = await message_repo.count_for_session(session.id)
                    entries = [
                        {
                            "idx": base_idx + i,
                            "role": m.role.value,
                            "blocks": m.model_dump_json(),
                        }
                        for i, m in enumerate(buffer)
                    ]
                    await message_repo.append_many(session.id, entries)
                    first_idx = base_idx
                    last_idx = base_idx + len(entries) - 1

                # After the messages, never during the stream. A run persists
                # all-or-nothing; writing the plan mid-stream would leave a row
                # describing a transcript that never landed, and the plan panel
                # would then point at messages that do not exist. Messages are the
                # record, this column is only its cache - hence the order. Being
                # here also inherits the timeout semantics for free: a run that
                # timed out still flushes both.
                if plan is not None:
                    await session_repo.set_plan(session.id, plan.model_dump_json())

                # Background, never awaited: the user already has their answer, and
                # blocking here would keep holding the session lock and a slot of the
                # global run semaphore for as long as the memory model takes. Only
                # this run's messages go in - feeding the whole history every turn
                # would re-extract, and re-pay for, facts that are already stored.
                if self.memory is not None:
                    self.memory.spawn_extraction(
                        user_id=user_id,
                        username=username,
                        session_id=session.id,
                        messages=buffer,
                        fallback_model=model,
                    )

                # metering: record usage once per completed run
                if self.usage is not None and final_usage is not None:
                    try:
                        await self.usage.record(
                            user_id=user_id,
                            username=username,
                            session_id=session.id,
                            model=model,
                            input_tokens=final_usage.input_tokens,
                            output_tokens=final_usage.output_tokens,
                            turns=final_turns,
                        )
                    except Exception:  # noqa: BLE001 - metering must never fail a run
                        logging.getLogger("pi.server").exception("usage recording failed")

                # Metrics ride on the recorder, because it is the one object that
                # saw every event of this run: the counters cannot disagree with
                # the trace next to them. Computed once and shared with the trace
                # write below, so a run is never two different lengths.
                duration_ms = (
                    round((time.monotonic() - recorder.started_monotonic) * 1000, 1)
                    if recorder is not None
                    else 0.0
                )
                if recorder is not None:
                    self.metrics.run_finished(
                        status=recorder.status,
                        model=model,
                        duration_s=duration_ms / 1000,
                        turns=recorder.turns,
                        tokens_in=recorder.input_tokens,
                        tokens_out=recorder.output_tokens,
                    )

                # The execution trace: same lifecycle as the usage row - one
                # write after the stream, never on it, and a failure must never
                # reach the user (the answer is already theirs; messages and
                # billing are already recorded).
                if recorder is not None and self.traces is not None:
                    try:
                        # Finalized only here, after the idx range exists: the
                        # upgrade needs the very messages that were (or were
                        # going to be) persisted.
                        recorder.finalize(buffer, first_idx, last_idx)
                        await self.traces.append(
                            run_id=recorder.run_id,
                            user_id=user_id,
                            username=username,
                            session_id=session.id,
                            model=model,
                            prompt=recorder.prompt,
                            request_id=recorder.request_id,
                            enable_search=recorder.enable_search,
                            builtin_tools=recorder.builtin_tools,
                            first_idx=recorder.first_idx,
                            last_idx=recorder.last_idx,
                            status=recorder.status,
                            error=recorder.error,
                            input_tokens=recorder.input_tokens,
                            output_tokens=recorder.output_tokens,
                            turns=recorder.turns,
                            failed_tools=recorder.failed_tools,
                            duration_ms=duration_ms,
                            flags=recorder.flags(),
                            started_at=recorder.started_at,
                            steps=recorder.steps,
                        )
                    except Exception:  # noqa: BLE001 - tracing must never fail a run
                        # Counted, because "tracing must never fail a run" also
                        # means nothing else would ever say it had.
                        self.metrics.trace_write_failed()
                        logging.getLogger("pi.server").exception("trace recording failed")
                    await self._maybe_retain_traces()
        finally:
            await self.cache.release_lock(lock_key)

    async def _maybe_retain_traces(self) -> None:
        """Hourly, per process: delete traces older than the retention window.

        Runs on run finalize instead of a dedicated loop so every install gets
        it - traces are recorded whether or not memory is configured, and the
        memory maintenance loop would not exist in a memory-less one.
        """
        if self.traces is None or self.trace_retention_days <= 0:
            return
        now = time.monotonic()
        if now - self._last_retention < RETENTION_INTERVAL_SECONDS:
            return
        self._last_retention = now
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.trace_retention_days)
        ).isoformat(timespec="seconds")
        try:
            runs, steps = await self.traces.delete_older_than(cutoff)
            if runs:
                logging.getLogger("pi.server").info(
                    "trace retention: deleted %d run(s) / %d step(s) older than %s",
                    runs,
                    steps,
                    cutoff,
                )
        except Exception:  # noqa: BLE001 - retention is housekeeping
            logging.getLogger("pi.server").exception("trace retention failed")


def event_to_sse(ev: AgentEvent) -> str:
    """Serialize one agent event into a Server-Sent Events frame."""
    if isinstance(ev, TextDeltaEvent):
        data = json.dumps({"text": ev.text}, ensure_ascii=False)
        return f"event: text_delta\ndata: {data}\n\n"
    if isinstance(ev, ToolCallStartEvent):
        data = json.dumps({"id": ev.id, "name": ev.name})
        return f"event: toolcall_start\ndata: {data}\n\n"
    if isinstance(ev, ToolCallEndEvent):
        data = json.dumps(
            {"id": ev.id, "name": ev.name, "ok": ev.ok, "result": ev.result[:400]},
            ensure_ascii=False,
        )
        return f"event: toolcall_end\ndata: {data}\n\n"
    if isinstance(ev, CompactionEvent):
        data = json.dumps(
            {
                "dropped": ev.dropped,
                "chars_before": ev.chars_before,
                "chars_after": ev.chars_after,
            }
        )
        return f"event: compaction\ndata: {data}\n\n"
    if isinstance(ev, PlanEvent):
        # No length cap: Plan's own field bounds already size this at ~6 KB.
        data = json.dumps(ev.plan.model_dump(), ensure_ascii=False)
        return f"event: plan\ndata: {data}\n\n"
    if isinstance(ev, TurnEndEvent):
        data = json.dumps(
            {
                "turns": ev.turns,
                "usage": {
                    "input_tokens": ev.usage.input_tokens,
                    "output_tokens": ev.usage.output_tokens,
                },
            }
        )
        return f"event: turn_end\ndata: {data}\n\n"
    if isinstance(ev, ErrorEvent):
        data = json.dumps({"message": ev.message}, ensure_ascii=False)
        return f"event: error\ndata: {data}\n\n"
    return f"event: unknown\ndata: {{}}\n\n"
