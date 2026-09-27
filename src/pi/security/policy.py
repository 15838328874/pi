"""Policy engine: tool deny-list, bash command patterns, path sandbox.

A Policy gates every tool execution inside AgentLoop._run_tool.
Default policy allows everything (single-user local mode stays unchanged);
enterprise deployments load a policy file via --policy / PI_POLICY.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE_PATH_TOOLS = {"read", "write", "edit", "ls"}
FILE_PATH_ARG = "path"
SEARCH_PATH_TOOLS = {"grep", "find"}
# Generic path confinement: MCP / skill tools are not name-known, so any tool
# whose args carry one of these keys gets workspace-confined when path_sandbox
# is on. Builtin tools never use these keys outside the two sets above, so
# their behavior is unchanged.
PATH_KEYS = ("path", "file", "dir")

_DEFAULT_DENY_COMMAND_PATTERNS: list[str] = []


@dataclass
class Policy:
    deny_tools: set[str] = field(default_factory=set)
    deny_command_patterns: list[re.Pattern[str]] = field(default_factory=list)
    path_sandbox: bool = False
    redact: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        patterns = []
        for raw in data.get("deny_command_patterns", _DEFAULT_DENY_COMMAND_PATTERNS):
            try:
                patterns.append(re.compile(str(raw), re.IGNORECASE))
            except re.error as exc:
                raise ValueError(f"invalid deny_command_pattern {raw!r}: {exc}") from exc
        return cls(
            deny_tools={str(t) for t in data.get("deny_tools", [])},
            deny_command_patterns=patterns,
            path_sandbox=bool(data.get("path_sandbox", False)),
            redact=bool(data.get("redact", False)),
        )


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str = ""


def load_policy(path: str | Path | None) -> Policy | None:
    """Load a policy from a JSON file. None/missing file -> None (no gating)."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"policy file not found: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("policy file must contain a JSON object")
    return Policy.from_dict(data)


def check(policy: Policy | None, tool_name: str, args: dict[str, Any], cwd: Path) -> PolicyDecision:
    """Evaluate one tool call against the policy. Returns the decision."""
    if policy is None:
        return PolicyDecision(allowed=True)

    if tool_name in policy.deny_tools:
        return PolicyDecision(allowed=False, reason=f"tool {tool_name!r} is denied by policy")

    if tool_name == "bash":
        command = str(args.get("command", ""))
        for pat in policy.deny_command_patterns:
            if pat.search(command):
                return PolicyDecision(
                    allowed=False,
                    reason=f"command matches deny pattern {pat.pattern!r}",
                )

    if policy.path_sandbox:
        for raw_path in _extract_paths(tool_name, args):
            target = Path(raw_path)
            if not target.is_absolute():
                target = cwd / target
            try:
                resolved = target.resolve()
                base = cwd.resolve()
                resolved.relative_to(base)
            except (OSError, ValueError):
                return PolicyDecision(
                    allowed=False,
                    reason=f"path {raw_path!r} escapes the workspace sandbox ({cwd})",
                )

    return PolicyDecision(allowed=True)


def _extract_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    """All path-like argument values a tool should be confined to the workspace.

    Known file tools contribute their dedicated "path" arg; unknown tools
    (MCP / skill) contribute every string value under a path-like key.
    """
    if tool_name in FILE_PATH_TOOLS or tool_name in SEARCH_PATH_TOOLS:
        value = args.get(FILE_PATH_ARG)
        return [str(value)] if value else []
    out: list[str] = []
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            out.append(value)
    return out
