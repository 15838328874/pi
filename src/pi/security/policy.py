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
    # Capability-based authorization (see tools.base.Tool.capabilities).
    # - deny_capabilities: any tool whose capabilities intersect this set is denied.
    # - allow_capabilities: when non-empty, a tool is allowed only if it declares
    #   capabilities AND all of them are within this set (subset, not intersection,
    #   so a powerful tool like bash is not admitted through one overlapping cap).
    #   A tool with no declared capabilities (MCP/skill) is denied: fail-closed.
    allow_capabilities: set[str] = field(default_factory=set)
    deny_capabilities: set[str] = field(default_factory=set)

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
            allow_capabilities={str(c) for c in data.get("allow_capabilities", [])},
            deny_capabilities={str(c) for c in data.get("deny_capabilities", [])},
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


def check(
    policy: Policy | None,
    tool_name: str,
    args: dict[str, Any],
    cwd: Path,
    capabilities: frozenset[str] = frozenset(),
) -> PolicyDecision:
    """Evaluate one tool call against the policy. Returns the decision."""
    if policy is None:
        return PolicyDecision(allowed=True)

    if tool_name in policy.deny_tools:
        return PolicyDecision(allowed=False, reason=f"tool {tool_name!r} is denied by policy")

    if capabilities & policy.deny_capabilities:
        denied = sorted(capabilities & policy.deny_capabilities)
        return PolicyDecision(
            allowed=False,
            reason=f"tool {tool_name!r} requires denied capability: {', '.join(denied)}",
        )

    if policy.allow_capabilities and not (
        capabilities and capabilities <= policy.allow_capabilities
    ):
        if not capabilities:
            reason = (
                f"tool {tool_name!r} declares no capabilities; denied by "
                f"allow-list policy"
            )
        else:
            reason = (
                f"tool {tool_name!r} capabilities {sorted(capabilities)} exceed "
                f"the allowed set {sorted(policy.allow_capabilities)}"
            )
        return PolicyDecision(allowed=False, reason=reason)

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
                # Actionable, not just "denied": a model that only sees
                # "escapes the sandbox" retries the same absolute path in
                # variants and burns turns into the timeout. Telling it the
                # allowed base and the fix lets it recover in one step.
                #
                # Common confusion: the sandbox VM's internal /workspace is a
                # *different* filesystem from the host workspace. A path under
                # /workspace belongs to the VM, so the recovery is to read it
                # with a bash command *inside* the sandbox, not the host tools.
                if raw_path == "/workspace" or raw_path.startswith("/workspace/"):
                    reason = (
                        f"{raw_path!r} is a path inside the sandbox VM, not the "
                        f"host workspace (root: {cwd}). To read or edit it, run "
                        f"a bash command inside the sandbox (e.g. `cat "
                        f"{raw_path}`) instead of the host read/write tools."
                    )
                else:
                    reason = (
                        f"path {raw_path!r} escapes the workspace sandbox "
                        f"(workspace root: {cwd}); use a relative path inside "
                        f"the workspace instead"
                    )
                return PolicyDecision(allowed=False, reason=reason)

    return PolicyDecision(allowed=True)


def _extract_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    """All path-like argument values a tool should be confined to the workspace.

    Known file tools contribute their dedicated "path" arg; unknown tools
    (MCP / skill) contribute every string value under a path-like key
    (path/file/dir), including inside nested dicts/lists.

    This is best-effort and key-name based, NOT a security boundary for
    MCP/skill tools: an arg under any other name ("target", "url", "src") is not
    confined. The real controls for those tools are the capability allow-list
    (allow_capabilities, fail-closed for undeclared tools) and the operator's own
    MCP server root - see tools/mcp.py.
    """
    if tool_name in FILE_PATH_TOOLS or tool_name in SEARCH_PATH_TOOLS:
        value = args.get(FILE_PATH_ARG)
        return [str(value)] if value else []
    out: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PATH_KEYS and isinstance(value, str) and value:
                    out.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(args)
    return out
