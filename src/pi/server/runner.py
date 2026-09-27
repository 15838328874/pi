"""Agent execution manager: per-session locks, global concurrency cap, timeouts.

Bridges HTTP requests to AgentLoop. Server mode always enforces a security
policy: explicit PI_POLICY file if given, else path_sandbox + redact defaults.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from pi.agent.events import (
    AgentEvent,
    CompactionEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from pi.agent.loop import AgentLoop
from pi.llm.registry import resolve_chain
from pi.models import Message, Role, TextBlock
from pi.observability.metering import UsageTracker
from pi.observability.metrics import Metrics
from pi.observability.tracing import Tracer
from pi.prompt import SYSTEM_PROMPT
from pi.security.audit import AuditLogger
from pi.security.policy import Policy, load_policy
from pi.server.cache import CacheBackend, MemoryBackend
from pi.server.db import MemoryRepo, MessageRepo, SessionRow
from pi.server.trajectory_store import append_trajectory
from pi.tools.registry import ToolRegistry
from pi.tools.sandbox import get_runner


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
        registry: ToolRegistry | None = None,
        metrics: Metrics | None = None,
        trajectory_path: Path | None = None,
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
        self.registry = registry or ToolRegistry()  # builtin-only when unset
        self.metrics = metrics or Metrics(enabled=False)
        self.trajectory_path = trajectory_path
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def run_turn(
        self,
        *,
        session: SessionRow,
        username: str,
        user_id: int,
        prompt: str,
        model: str,
        message_repo: MessageRepo,
        memory_repo: MemoryRepo | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute one user turn; messages are persisted on completion."""
        # distributed session lock: correct across instances when Redis-backed
        lock_key = f"session:{session.id}"
        if not await self.cache.acquire_lock(lock_key, ttl_seconds=self.timeout + 60):
            yield ErrorEvent(message="another turn is already running for this session")
            return

        try:
            async with self._semaphore:
                buffer: list[Message] = []
                compaction_to_save: dict | None = None

                def on_message(msg: Message) -> None:
                    buffer.append(msg)

                def on_compact(
                    new_messages: list[Message], covered_upto_idx: int | None
                ) -> None:
                    nonlocal compaction_to_save
                    if covered_upto_idx is not None and new_messages:
                        compaction_to_save = {
                            "covered_upto_idx": covered_upto_idx,
                            "summary": new_messages[0].blocks[0].text,
                        }

                # Reuse the persisted episodic summary (P3) instead of re-summarizing.
                latest = await message_repo.latest_compaction(session.id)
                rows = await message_repo.list_for_session(
                    session.id,
                    after_idx=latest.covered_upto_idx if latest is not None else -1,
                )
                history: list[Message] = []
                history_idx: list[int | None] = []
                if latest is not None:
                    history.append(
                        Message(role=Role.user, blocks=[TextBlock(text=latest.summary)])
                    )
                    history_idx.append(None)
                for row in rows:
                    history.append(Message.model_validate_json(row.blocks))
                    history_idx.append(row.idx)

                # Semantic memory (P3): inject relevant cross-session memories so
                # the agent breaks session amnesia without being asked to recall.
                system_prompt = SYSTEM_PROMPT
                if memory_repo is not None:
                    try:
                        relevant = await memory_repo.search(user_id, prompt, k=3)
                    except Exception:  # noqa: BLE001 - memory must never fail a run
                        relevant = []
                    if relevant:
                        lines = "\n".join(f"- {m.text}" for m in relevant)
                        system_prompt = (
                            f"{SYSTEM_PROMPT}\n\n"
                            f"<relevant memories>\n{lines}\n</relevant memories>"
                        )
                # Skills: inject the compact index (names + one-liners, no body)
                # so the model knows what is available and can use_skill for
                # progressive loading. Mirrors the memory injection above.
                skill_index = self.registry.skill_index()
                if skill_index:
                    system_prompt = (
                        f"{system_prompt}\n\n"
                        f"<available skills>\n{skill_index}\n</available skills>"
                    )

                provider = resolve_chain(
                    model,
                    # "silent degradations must make noise": a fallback hop is
                    # exactly that, and the counter is the only thing that
                    # shows it on a dashboard (callback failures are swallowed
                    # by FallbackProvider itself).
                    on_fallback=lambda frm, to, reason: self.metrics.fallback(
                        from_model=frm, to_model=to
                    ),
                )
                agent = AgentLoop(
                    provider=provider,
                    tools=await self.registry.tools(),
                    system_prompt=system_prompt,
                    messages=history,
                    message_idx=history_idx,
                    cwd=Path(session.cwd),
                    on_message=on_message,
                    on_compact=on_compact,
                    policy=self.policy,
                    audit=self.audit,
                    session_id=session.id,
                    user_id=username,
                    tracer=self.tracer,
                )
                if memory_repo is not None:
                    agent.ctx.memory = memory_repo
                    agent.ctx.user_db_id = user_id
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
                run_status = "ok"
                run_started = time.perf_counter()

                async def _stream():
                    async with asyncio.timeout(self.timeout):
                        async for ev in agent.run(prompt):
                            yield ev

                try:
                    async with self.metrics.in_flight():
                        async for ev in _stream():
                            if isinstance(ev, TurnEndEvent):
                                final_usage = ev.usage
                                final_turns = ev.turns
                            elif isinstance(ev, ErrorEvent):
                                # a timeout that lands inside the loop's own try
                                # surfaces here as an ErrorEvent with the
                                # exception name in the message
                                run_status = (
                                    "timeout" if "TimeoutError" in ev.message else "error"
                                )
                            yield ev
                except TimeoutError:
                    run_status = "timeout"
                    yield ErrorEvent(message=f"run timed out after {self.timeout}s")
                except Exception as exc:  # noqa: BLE001
                    run_status = "error"
                    yield ErrorEvent(message=f"{type(exc).__name__}: {exc}")
                finally:
                    run_duration = time.perf_counter() - run_started
                    # metrics are a projection, never a dependency: any failure
                    # here is logged and swallowed, like metering below
                    try:
                        self.metrics.run_finished(
                            status=run_status,
                            model=model,
                            duration_s=run_duration,
                            turns=final_turns,
                            tokens_in=final_usage.input_tokens if final_usage else 0,
                            tokens_out=final_usage.output_tokens if final_usage else 0,
                        )
                        traj = getattr(agent, "trajectory", None)
                        if traj is not None:
                            for e in traj.to_dict()["events"]:
                                if e.get("type") == "ToolCall":
                                    # spans miss denied/unknown/invalid-args calls;
                                    # the trajectory has every attempted execution
                                    self.metrics.tool_call(
                                        tool=str(e.get("name", "?")),
                                        ok=not bool(e.get("is_error")),
                                        duration_s=float(e.get("latency_ms", 0)) / 1000.0,
                                    )
                    except Exception:  # noqa: BLE001 - metrics must never fail a run
                        logging.getLogger("pi.server").exception("metrics projection failed")

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

                if compaction_to_save is not None:
                    try:
                        await message_repo.save_compaction(
                            session.id,
                            covered_upto_idx=compaction_to_save["covered_upto_idx"],
                            summary=compaction_to_save["summary"],
                            model=model,
                        )
                    except Exception:  # noqa: BLE001 - persistence must never fail a run
                        logging.getLogger("pi.server").exception("compaction save failed")

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

                # Trajectory persistence (TRAJECTORY_VIEW_DESIGN P0): the run is
                # already over, so a failed write is logged, never re-raised.
                # Raw values by design - the viewer endpoint enforces ownership.
                if self.trajectory_path is not None:
                    traj = getattr(agent, "trajectory", None)
                    if traj is not None:
                        try:
                            record = traj.to_dict()
                            record["session_id"] = session.id
                            record["user_id"] = user_id
                            append_trajectory(self.trajectory_path, record)
                        except Exception:  # noqa: BLE001 - persistence must never fail a run
                            logging.getLogger("pi.server").exception("trajectory save failed")
        finally:
            await self.cache.release_lock(lock_key)


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
