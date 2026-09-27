"""Outbound redaction: mask secrets before messages leave the machine.

Only the copy sent to the LLM is redacted; local session history keeps the
original text. Enabled per-policy (Policy.redact).

mask_url() is the log-safe projection of the same concern: connection strings
(redis://user:pass@host, mysql+aiomysql://...) reach the startup logs verbatim
(L15). Outbound redaction guards data leaving the process; mask_url guards
configuration landing on disk via journald.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from pi.models import Message, TextBlock, ToolCallBlock, ToolResultBlock

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # common cloud/LLM API keys
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:api_key]"),
    (re.compile(r"\bLTAI[A-Za-z0-9]{8,}\b"), "[REDACTED:aliyun_ak]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws_ak]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "[REDACTED:github_token]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack_token]"),
    # key=value style secrets
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential)\s*[:=]\s*[^\s'\"]{8,}"
        ),
        r"\1=[REDACTED]",
    ),
    # China ID number (18 digits, last may be X)
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[REDACTED:id_number]"),
    # China mobile number
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED:mobile]"),
    # private-network IPv4 ranges
    (
        re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3})\b"
        ),
        "[REDACTED:internal_ip]",
    ),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_url(raw: str) -> str:
    """Log-safe form of a connection string: password/userinfo masked.

    redis://user:pass@host:6379/0 -> redis://user:***@host:6379/0
    Unparseable strings fall back to redact_text() (key-pattern defense).
    """
    try:
        parts = urlsplit(raw)
    except ValueError:
        return redact_text(raw)
    if parts.username is not None or parts.password is not None:
        user = parts.username or ""
        host = f"{parts.hostname or ''}"
        if parts.port is not None:
            host += f":{parts.port}"
        netloc = f"{user}:***@{host}"
        parts = parts._replace(netloc=netloc)
        return urlunsplit(parts)
    return redact_text(raw)


def redact_messages(messages: list[Message]) -> list[Message]:
    """Return a redacted copy of the message list (originals untouched)."""
    out: list[Message] = []
    for msg in messages:
        blocks = []
        for b in msg.blocks:
            if isinstance(b, TextBlock):
                blocks.append(TextBlock(text=redact_text(b.text)))
            elif isinstance(b, ToolResultBlock):
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=b.tool_use_id,
                        content=redact_text(b.content),
                        is_error=b.is_error,
                    )
                )
            elif isinstance(b, ToolCallBlock):
                blocks.append(b.model_copy())
            else:  # pragma: no cover - future block types
                blocks.append(b)
        out.append(Message(role=msg.role, blocks=blocks))
    return out
