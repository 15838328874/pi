"""pi-py: Python implementation of the pi coding agent harness.

Layers (bottom-up, mirroring earendil-works/pi):
- pi.llm      -> unified streaming LLM API (pi-ai)
- pi.agent    -> asyncio agent loop (pi-agent-core)
- pi.tools    -> bash/read/write/edit/grep/find/ls (pi-coding-agent tools)
- pi.security -> policy gate, outbound redaction, audit log
- pi.server   -> multi-user HTTP service (FastAPI + JWT + SSE, MySQL/Redis)
- pi.cli      -> service entrypoints: serve / migrate
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

_ENV_FILE_CANDIDATES = (
    # 2026-10-01: 曾有三个候选（.pi-py.env / .env / ~/.pi-py/.env）。
    # 项目根的 .pi-py.env 与 .env 同槽位冗余（compose 无 ${} 替换、无冲突），
    # 收敛为两个：项目内 .env，机器级 ~/.pi-py/.env。变更记录见 ARCHITECTURE §12.2。
    Path(".env"),
    Path.home() / ".pi-py" / ".env",
)


def _load_env_file() -> None:
    """Load KEY=VALUE lines into os.environ (existing environment variables win)."""
    for candidate in _ENV_FILE_CANDIDATES:
        try:
            if not candidate.is_file():
                continue
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
        return


_load_env_file()
