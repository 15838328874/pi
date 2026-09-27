"""Audit log: append-only JSONL records of tool calls, policy decisions and auth attempts.

Each record: timestamp, session id, user id, tool, arguments (possibly
redacted), decision, outcome. Written to ~/.pi-py/audit.jsonl by default.

`auth` records also carry the client IP and user agent. That is personal data
under PIPL/GDPR, so it changes what this file is: give it a retention period
rather than keeping it forever.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_AUDIT_PATH = Path.home() / ".pi-py" / "audit.jsonl"


def _daily_path(base: Path, day: str) -> Path:
    """audit.jsonl -> audit-2026-09-01.jsonl (daily rotation)."""
    return base.with_name(f"{base.stem}-{day}{base.suffix}")


class AuditLogger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_AUDIT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._day = ""

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
