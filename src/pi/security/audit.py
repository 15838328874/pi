"""Audit log: append-only records of tool calls, policy decisions and auth attempts.

MySQL-first: every record lands in the audit_events table (the admin endpoint
queries it), and the JSONL file is a mirror - kept because it is readable with
plain tools, survives a database outage, and existing tests read it directly.
Each record: timestamp, session id, user id, tool, arguments (possibly
redacted), decision, outcome. Written to ~/.pi-py/audit.jsonl by default.

`auth` records also carry the client IP and user agent. That is personal data
under PIPL/GDPR, so it changes what this file is: give it a retention period
rather than keeping it forever.

The MySQL side is a bounded queue drained by one background task: audit writes
happen on request paths, and a database round trip there would price every
audited request. When the queue overflows or the insert fails, the JSONL mirror
is the record of last resort - loud log, no exception into the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_PATH = Path.home() / ".pi-py" / "audit.jsonl"

#: Queue bound: at overflow the newest records are dropped from MySQL (JSONL
#: keeps them). 10k records is minutes of heavy traffic, not a DoS lever.
QUEUE_MAX = 10_000

#: Batch bound for the drainer: how many records one INSERT round trip may carry.
BATCH_MAX = 100

log = logging.getLogger("pi.audit")


def _daily_path(base: Path, day: str) -> Path:
    """audit.jsonl -> audit-2026-09-01.jsonl (daily rotation)."""
    return base.with_name(f"{base.stem}-{day}{base.suffix}")


class AuditLogger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_AUDIT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._day = ""
        self._db_queue: asyncio.Queue | None = None
        self._drain_task: asyncio.Task | None = None
        self._db_repo: Any = None

    def attach_db(self, db: Any) -> None:
        """Start mirroring every record into audit_events. Lifespan-only: needs
        a running loop, and the drainer is cancelled by close()."""
        from pi.server.db import AuditEventRepo

        self._db_repo = AuditEventRepo(db)
        self._db_queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._drain_task = asyncio.create_task(
            self._drain(self._db_repo, self._db_queue)
        )

    async def close(self) -> None:
        """Stop the drainer and flush what is still queued - a shutdown must not
        lose the very records that say why it happened."""
        task, queue, repo = self._drain_task, self._db_queue, self._db_repo
        self._drain_task = None
        self._db_queue = None
        self._db_repo = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        leftover: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            leftover.append(queue.get_nowait())
        if leftover:
            try:
                await repo.append_many(leftover)
            except Exception:  # noqa: BLE001 - the mirror keeps the record
                log.exception(
                    "audit_events final flush failed for %d record(s)", len(leftover)
                )

    async def _drain(self, repo: Any, queue: asyncio.Queue) -> None:
        while True:
            batch = [await queue.get()]
            while len(batch) < BATCH_MAX and not queue.empty():
                batch.append(queue.get_nowait())
            try:
                await repo.append_many(batch)
            except Exception:  # noqa: BLE001 - the mirror keeps the record
                log.exception("audit_events insert failed for %d record(s)", len(batch))

    def _write(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            target = _daily_path(self.path, day)
            if day != self._day:  # lazy daily rotation
                self._day = day
                target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self._db_queue is not None:
            try:
                self._db_queue.put_nowait(record)
            except asyncio.QueueFull:
                log.warning(
                    "audit queue full (%d); record kept in the JSONL mirror only",
                    QUEUE_MAX,
                )

    def tool_call(
        self,
        *,
        session_id: str,
        user_id: str,
        tool: str,
        args: dict[str, Any],
        decision_allowed: bool,
        decision_reason: str = "",
        ok: bool | None = None,
        result_preview: str = "",
    ) -> None:
        self._write(
            {
                "event": "tool_call",
                "session": session_id,
                "user": user_id,
                "tool": tool,
                "args": args,
                "allowed": decision_allowed,
                "reason": decision_reason,
                "ok": ok,
                "result_preview": result_preview[:400],
            }
        )

    def compaction(self, *, session_id: str, user_id: str, dropped: int, before: int, after: int) -> None:
        self._write(
            {
                "event": "compaction",
                "session": session_id,
                "user": user_id,
                "dropped": dropped,
                "chars_before": before,
                "chars_after": after,
            }
        )

    def memory(
        self,
        *,
        action: str,
        user_id: str,
        session_id: str = "",
        facts: int = 0,
        reason: str = "",
        text_preview: str = "",
    ) -> None:
        """Long-term memory writes and deletes, queryable via GET /v1/admin/audit.

        Every field is clamped for the same reason auth() clamps: fact text is model
        output derived from a transcript the user controls, so an unbounded write
        here would let one run fill the audit volume.
        """
        self._write(
            {
                "event": "memory",
                "action": action[:16],
                "user": user_id[:64],
                "session": session_id[:16],
                "facts": facts,
                "reason": reason[:32],
                "text_preview": text_preview[:400],
            }
        )

    def file(
        self,
        *,
        action: str,
        user_id: str,
        session_id: str = "",
        name: str = "",
        size: int = 0,
    ) -> None:
        """Session file upload/download. The download path is unauthenticated
        (the model gateway must fetch the URL), so this is the only record of
        who exposed what. Same clamping rationale as auth()."""
        self._write(
            {
                "event": "file",
                "action": action[:16],
                "user": user_id[:64],
                "session": session_id[:64],
                "name": name[:200],
                "size": size,
            }
        )

    def auth(
        self,
        *,
        action: str,
        username: str,
        ip: str,
        ok: bool,
        user_agent: str = "",
        reason: str = "",
    ) -> None:
        """Register/login attempts; ip is the real client only if PI_FORWARDED_ALLOW_IPS covers the proxy.

        Every field is truncated: the failed-login path is attacker-controlled and
        LoginIn.username has no length bound, so an unbounded write here would
        let one request fill the audit volume.
        """
        self._write(
            {
                "event": "auth",
                "action": action[:16],
                "username": username[:64],
                "ip": ip[:45],  # longest possible IPv6 literal
                "ua": user_agent[:200],
                "ok": ok,
                "reason": reason[:32],
            }
        )
