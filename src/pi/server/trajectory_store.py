"""Trajectory persistence: append-only JSONL with daily rotation (mirrors audit.py).

Each line is one run's full trajectory dict plus the top-level ``session_id`` /
``user_id`` (int DB id) that the query side scans for. Raw values, same
sensitivity class as the workspace: the viewer endpoint enforces session
ownership before anything is returned.

Persistence is best-effort: append failures must never fail a run, so callers
catch around ``append_trajectory``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("pi.server.trajectory")

DEFAULT_TRAJECTORY_PATH = Path.home() / ".pi-py" / "trajectories.jsonl"

_write_lock = threading.Lock()


def _daily_path(base: Path, day: str) -> Path:
    """trajectories.jsonl -> trajectories-2026-09-27.jsonl (daily rotation)."""
    return base.with_name(f"{base.stem}-{day}{base.suffix}")


def append_trajectory(base: Path, record: dict[str, Any]) -> None:
    """Append one run record (a JSON line) to today's file."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = _daily_path(base, day)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    # The lock serializes writers in one process; across processes/instances a
    # single short line appended in O_APPEND mode stays atomic on POSIX.
    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def latest_trajectory(base: Path, session_id: str) -> dict[str, Any] | None:
    """Newest stored run of a session, scanning daily files newest-first.

    Linear in the total volume of stored trajectories; fine at single-team
    scale (tens of MB). A DB-backed store (JSONB) would be a drop-in
    replacement behind this function when it stops being fine.
    """
    pattern = f"{base.stem}-*.jsonl"
    files = sorted(base.parent.glob(pattern), reverse=True)  # dates sort lexicographically
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("trajectory file %s unreadable: %s", path, exc)
            continue
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("session_id") == session_id:
                return record
    return None
