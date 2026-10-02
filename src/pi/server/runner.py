"""Agent execution manager: per-session locks, global concurrency cap, timeouts.

Bridges HTTP requests to AgentLoop. Server mode always enforces a security
policy: explicit PI_POLICY file if given, else path_sandbox + redact defaults.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pi.agent.events import (
    AgentEvent,
    CompactionEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingEvent,
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
from pi.server.archive import (
    archive_workspace,
    enabled as archive_enabled,
    snapshot_files,
)
from pi.server.db import MemoryRepo, MessageRepo, RunRepo, SessionRow
from pi.server.trajectory_store import append_trajectory
from pi.tools.registry import ToolRegistry
from pi.tools.sandbox import get_runner

#: Total budget for sandbox teardown in a turn's finally. close() saves the
#: workspace (tar round-trip) and kills the VM; if it blows this budget the
#: turn stops waiting but the cleanup thread keeps going and still kills the
#: VM, so the cap only bounds how long a turn can be held hostage by a slow
#: tar - never whether the VM dies.
_SANDBOX_CLOSE_TIMEOUT_S = float(os.environ.get("PI_SANDBOX_CLOSE_TIMEOUT_SECONDS", 90))


@dataclass
class _PoolEntry:
    """One idle sandbox in the session-scoped reuse pool."""

    session_id: str
    runner: Any
    last_used: float  # monotonic; LRU eviction + idle-reap key


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
        pool_ttl_s: int = 900,
        pool_ttl_tight_s: int = 300,
        pool_size: int = 4,
        pool_pressure_high: int = 1_500 * 1024 * 1024,
        pool_pressure_low: int = 512 * 1024 * 1024,
        files_repo: Any = None,  # FileRepo (files table), injected to tools
        store: Any = None,  # ObjectStore (presigned URLs), injected to tools
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
        # 会话级沙箱池（懒加载 + 复用 + 自适应空闲回收）
        self.pool_ttl_s = pool_ttl_s
        self.pool_ttl_tight_s = pool_ttl_tight_s
        self.pool_size = pool_size
        self.pool_pressure_high = pool_pressure_high
        self.pool_pressure_low = pool_pressure_low
        self.sandbox_pool: dict[str, _PoolEntry] = {}
        self._pool_lock = asyncio.Lock()
        self._pool_sweep_task: asyncio.Task | None = None
        self._pool_sweep_interval_s = 30.0
        self.files_repo = files_repo
        self.store = store

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
        run_repo: RunRepo | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute one user turn; the user message is persisted up-front (write-ahead),
        the remaining messages on completion."""
        # distributed session lock: correct across instances when Redis-backed
        lock_key = f"session:{session.id}"
        runner = None  # owned by this turn; closed (workspace saved, VM dead) in finally
        baseline = None  # turn-start workspace snapshot; archive diff in finally
        if not await self.cache.acquire_lock(lock_key, ttl_seconds=self.timeout + 60):
            yield ErrorEvent(message="another turn is already running for this session")
            return

        try:
            async with self._semaphore:
                buffer: list[Message] = []
                compaction_to_save: dict | None = None
                # Write-ahead (run-durability step 1): the first message of a
                # non-resume run is always the user prompt (loop.py appends it
                # before streaming anything), so capture it separately and
                # persist it before the first event leaves this function. A hard
                # crash after that point can no longer lose the user's words.
                # Full plan: docs/run-durability-design.md.
                write_ahead: Message | None = None
                write_ahead_flushed = False

                def on_message(msg: Message) -> None:
                    nonlocal write_ahead
                    if write_ahead is None:
                        write_ahead = msg  # first message = user prompt
                    else:
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
                # 文件管线工具依赖（list_files/fetch_file 走工具结果，不碰 system_prompt）
                agent.ctx.files = self.files_repo
                agent.ctx.store = self.store
                if self.sandbox:
                    # 惰性沙箱：回合以本地模式起步，第一次 bash 调用（或显式
                    # 工具）通过 ensure_runner 现场创建/复用 VM。纯聊天回合
                    # 永不触发 —— 零 VM、零装载、零归档。
                    # baseline 也推迟到首次触发时取：复用回合的 host workspace
                    # 是上一回合 save 回来的，此刻快照 = 本回合的真实起点。
                    async def ensure_runner():
                        nonlocal runner, baseline
                        if runner is not None:
                            return
                        runner = await self._acquire_sandbox(session.id, Path(session.cwd))
                        agent.ctx.runner = runner
                        runner.metrics = self.metrics  # sandbox health counters
                        # a sandboxed runner with its own workspace filesystem
                        # (CubeSandbox files API) also hosts the file tools
                        agent.ctx.fs = getattr(runner, "fs", None)
                        if agent.ctx.fs is not None and hasattr(agent.ctx.fs, "set_host_root"):
                            agent.ctx.fs.set_host_root(Path(session.cwd))
                        if baseline is None and archive_enabled():
                            try:
                                baseline = await asyncio.to_thread(
                                    snapshot_files, Path(session.cwd)
                                )
                            except Exception:  # noqa: BLE001 - archiving never breaks a run
                                log.warning("archive baseline failed", exc_info=True)

                    agent.ctx.ensure_runner = ensure_runner
                    # 后台回收扫描器（池复用 + 空闲回收）只启一次
                    if self._pool_sweep_task is None:
                        self._pool_sweep_task = asyncio.create_task(self._pool_sweep())

                final_usage = None
                final_turns = 0
                run_status = "ok"
                run_started = time.perf_counter()

                async def _stream():
                    async with asyncio.timeout(self.timeout):
                        async for ev in agent.run(prompt):
                            yield ev

                async def _flush_write_ahead() -> None:
                    nonlocal write_ahead_flushed
                    if write_ahead is None or write_ahead_flushed:
                        return
                    base_idx = await message_repo.count_for_session(session.id)
                    await message_repo.append_many(
                        session.id,
                        [
                            {
                                "idx": base_idx,
                                "role": write_ahead.role.value,
                                "blocks": write_ahead.model_dump_json(),
                            }
                        ],
                    )
                    write_ahead_flushed = True

                try:
                    async with self.metrics.in_flight():
                        first_event = True
                        async for ev in _stream():
                            if first_event:
                                first_event = False
                                try:
                                    # Persist the user message before anything
                                    # streams to the client; hard-fail (data
                                    # safety first, see §17).
                                    await _flush_write_ahead()
                                except Exception as exc:  # noqa: BLE001
                                    run_status = "error"
                                    yield ErrorEvent(
                                        message=(
                                            "persisting user message failed: "
                                            f"{type(exc).__name__}: {exc}"
                                        )
                                    )
                                    break
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

                # Fallback: if the run never reached its first streamed event
                # (or the early flush failed), persist the user message together
                # with the rest - one batch, contiguous idx.
                if write_ahead is not None and not write_ahead_flushed:
                    buffer.insert(0, write_ahead)
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

                # Trajectory persistence: jsonl = append-only audit-grade copy
                # (daily rotation), runs table = structured query index (by
                # run_id / session_id / user_id). Both fail-soft and independent.
                traj = getattr(agent, "trajectory", None)
                record: dict | None = None
                if traj is not None:
                    record = traj.to_dict()
                    record["session_id"] = session.id
                    record["user_id"] = user_id
                    if self.trajectory_path is not None:
                        try:
                            append_trajectory(self.trajectory_path, record)
                        except Exception:  # noqa: BLE001 - persistence must never fail a run
                            logging.getLogger("pi.server").exception("trajectory save failed")
                    if run_repo is not None:
                        try:
                            await run_repo.save(record)
                        except Exception:  # noqa: BLE001 - jsonl copy still exists
                            logging.getLogger("pi.server").exception("trajectory db save failed")
        finally:
            # 会话级池语义：回合结束不销毁 VM —— workspace 回传宿主（归档与
            # 复用延续都需要）→ 归档 → 归还池。销毁只发生在：idle 超 TTL、
            # 池满淘汰、僵尸重建、服务关闭（均为后台/非回合路径）。
            if runner is not None and self.sandbox:
                # 1) VM 内最新 workspace 回传宿主（快，本地 tar round-trip）
                try:
                    await asyncio.to_thread(runner.save_workspace)
                except Exception:  # noqa: BLE001 - best-effort
                    log.debug("sandbox workspace save failed", exc_info=True)
                # 2) 回合级归档（只在真用过沙箱时；聊天回合跳过分文不取）
                if baseline is not None:
                    try:
                        await asyncio.to_thread(
                            archive_workspace,
                            session_id=session.id,
                            username=username,
                            cwd=Path(session.cwd),
                            baseline=baseline,
                        )
                    except Exception:  # noqa: BLE001 - archiving is best-effort
                        log.warning("workspace archive failed", exc_info=True)
                # 3) 归还池（VM 存活复用；不关不杀）
                await self._release_sandbox(session.id, runner)
                runner = None  # 已归还，不再 close
            await self.cache.release_lock(lock_key)

    # -- 会话级沙箱池 -------------------------------------------------------
    # 懒加载 + 复用 + 自适应空闲回收。池按 session_id 绑定（绝不跨会话共享，
    # 数据隔离第一）；容量由 pool_size 上限 + LRU 淘汰约束；空闲回收 TTL 随
    # 宿主可用内存自适应（宽裕 15min/收紧 5min/极紧立即清空）。

    async def _acquire_sandbox(self, session_id: str, host_cwd: Path) -> Any:
        """回合首次工具调用时取沙箱：同会话池命中（仍存活）→ 复用；否则新建。

        新建的 runner 由调用方装配（ctx.runner/fs/metrics）——与复用路径一致，
        拿到手就是一个可直接下命令的沙箱。僵尸 VM（平台 TTL 已回收）就地销毁
        并静默重建，绝不复用。
        """
        async with self._pool_lock:
            entry = self.sandbox_pool.pop(session_id, None)
            if entry is not None:
                if await entry.runner.is_alive():
                    if self.metrics is not None:
                        self.metrics.sandbox_pool_hit()
                    return entry.runner
                # 僵尸：平台可能已回收长闲置 VM，销毁残留并继续新建
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(entry.runner.close),
                        timeout=_SANDBOX_CLOSE_TIMEOUT_S,
                    )
                except Exception:  # noqa: BLE001 - best-effort
                    log.debug("zombie sandbox close failed", exc_info=True)
        runner = get_runner(
            self.sandbox,
            image=self.sandbox_image,
            allow_network=self.sandbox_network,
        )
        runner.metrics = self.metrics  # sandbox health counters
        return runner

    async def _release_sandbox(self, session_id: str, runner: Any) -> None:
        """回合结束归还：VM 存活复用。池满 → LRU 淘汰最久未用再放入。"""
        async with self._pool_lock:
            while len(self.sandbox_pool) >= self.pool_size and self.pool_size > 0:
                victim = min(self.sandbox_pool.values(), key=lambda e: e.last_used)
                del self.sandbox_pool[victim.session_id]
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(victim.runner.close),
                        timeout=_SANDBOX_CLOSE_TIMEOUT_S,
                    )
                except Exception:  # noqa: BLE001 - eviction is best-effort
                    log.debug("sandbox eviction close failed", exc_info=True)
                if self.metrics is not None:
                    self.metrics.sandbox_pool_evict()
            if len(self.sandbox_pool) < self.pool_size:
                self.sandbox_pool[session_id] = _PoolEntry(
                    session_id, runner, time.monotonic()
                )

    def _effective_pool_ttl(self) -> int:
        """宿主可用内存压力自适应 TTL：宽裕→pool_ttl_s；紧张→pool_ttl_tight_s；
        极紧→0（立即回收全部空闲 VM）。读 /proc/meminfo 无第三方依赖。"""
        avail = 1 << 60
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) * 1024
                        break
        except OSError:
            pass
        if avail < self.pool_pressure_low:
            return 0
        if avail < self.pool_pressure_high:
            return self.pool_ttl_tight_s
        return self.pool_ttl_s

    async def _pool_sweep(self) -> None:
        """后台扫描循环：回收 idle 超 TTL 的 VM；内存极紧时整体清空。"""
        while True:
            await asyncio.sleep(self._pool_sweep_interval_s)
            try:
                await self._sweep_once()
            except Exception:  # noqa: BLE001 - sweeper must never die
                log.debug("sandbox pool sweep failed", exc_info=True)

    async def _sweep_once(self) -> None:
        ttl = self._effective_pool_ttl()
        now = time.monotonic()
        async with self._pool_lock:
            if ttl <= 0:
                for e in list(self.sandbox_pool.values()):
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(e.runner.close),
                            timeout=_SANDBOX_CLOSE_TIMEOUT_S,
                        )
                    except Exception:  # noqa: BLE001 - best-effort
                        log.debug("sandbox pool flush close failed", exc_info=True)
                self.sandbox_pool.clear()
                return
            expired = [
                sid
                for sid, e in self.sandbox_pool.items()
                if now - e.last_used > ttl
            ]
            for sid in expired:
                e = self.sandbox_pool.pop(sid)
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(e.runner.close),
                        timeout=_SANDBOX_CLOSE_TIMEOUT_S,
                    )
                except Exception:  # noqa: BLE001 - best-effort
                    log.debug("sandbox pool reap close failed", exc_info=True)

    async def shutdown_pool(self) -> None:
        """应用关闭：销毁池内全部 VM（孤儿由平台自身 TTL 兜底回收）。"""
        async with self._pool_lock:
            entries = list(self.sandbox_pool.values())
            self.sandbox_pool.clear()
        for e in entries:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(e.runner.close),
                    timeout=_SANDBOX_CLOSE_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 - teardown must not block shutdown
                log.debug("sandbox pool shutdown close failed", exc_info=True)


def event_to_sse(ev: AgentEvent) -> str:
    """Serialize one agent event into a Server-Sent Events frame."""
    if isinstance(ev, TextDeltaEvent):
        data = json.dumps({"text": ev.text}, ensure_ascii=False)
        return f"event: text_delta\ndata: {data}\n\n"
    if isinstance(ev, ThinkingEvent):
        data = json.dumps({"text": ev.text}, ensure_ascii=False)
        return f"event: thinking_delta\ndata: {data}\n\n"
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
