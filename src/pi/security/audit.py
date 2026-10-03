"""Audit log: append-only JSONL records of tool calls, policy decisions and auth attempts.

Each record: timestamp, session id, user id, tool, arguments (possibly
redacted), decision, outcome. Written to ~/.pi-py/audit.jsonl by default.

`auth` records also carry the client IP and user agent. That is personal data
under PIPL/GDPR, so it changes what this file is: give it a retention period
rather than keeping it forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

DEFAULT_AUDIT_PATH = Path.home() / ".pi-py" / "audit.jsonl"


def _daily_path(base: Path, day: str) -> Path:
    """audit.jsonl -> audit-2026-09-01.jsonl (daily rotation)."""
    return base.with_name(f"{base.stem}-{day}{base.suffix}")


class AuditLogger:
    """jsonl = 合规底稿（追加式）；on_record = 结构化查询镜像（DB），双写。

    on_record is an async best-effort hook: the jsonl copy must never depend on
    the DB being reachable, and a sync caller (no running loop) simply skips it.
    """

    def __init__(
        self,
        path: Path | None = None,
        on_record: "Callable[[dict[str, Any]], Awaitable[None]] | None" = None,
        retention_days: int = 0,
    ):
        self.path = Path(path) if path else DEFAULT_AUDIT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._day = ""
        self.on_record = on_record
        # Rotated daily files older than this are pruned on rotation.
        # 0 = keep forever (historical behaviour). The DB mirror (on_record,
        # audit_events table) is NOT pruned by this - manage its retention in the
        # database layer. WORM / S3 Object Lock is out of scope here: pruning is a
        # deliberate, operator-configured data lifecycle, not tamper-proofing.
        self.retention_days = retention_days

    def _write(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            target = _daily_path(self.path, day)
            if day != self._day:  # lazy daily rotation
                self._day = day
                target.parent.mkdir(parents=True, exist_ok=True)
                self._prune()
            with target.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.on_record is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # sync caller: jsonl copy already written
            loop.create_task(self._fire(record))

    async def _fire(self, record: dict[str, Any]) -> None:
        try:
            await self.on_record(record)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - mirror failure never breaks the jsonl copy
            logging.getLogger("pi.security.audit").exception("audit db mirror failed")

    def _prune(self) -> None:
        """Delete rotated daily files older than retention_days (0 = keep forever).

        Runs inside the write lock, on rotation only, so it never races a writer.
        """
        if self.retention_days <= 0:
            return
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=self.retention_days)
        stem, suffix = self.path.stem, self.path.suffix
        for p in self.path.parent.glob(f"{stem}-*{suffix}"):
            day = p.name[len(stem) + 1 : -len(suffix)] if suffix else p.name[len(stem) + 1 :]
            try:
                file_day = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                continue  # not a dated rotation file (or foreign) - leave it alone
            if file_day < cutoff:
                try:
                    p.unlink(missing_ok=True)
                except OSError:  # noqa: BLE001 - lifecycle cleanup must not break writes
                    logging.getLogger("pi.security.audit").warning(
                        "audit prune failed for %s", p
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
