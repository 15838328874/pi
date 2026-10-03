"""Security layer tests: policy engine, redaction, audit log, server policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi.models import Message, Role, TextBlock, ToolResultBlock
from pi.security.audit import AuditLogger
from pi.security.policy import Policy, check, load_policy
from pi.security.redact import redact_messages, redact_text
from pi.server.runner import server_policy


def _policy() -> Policy:
    return Policy.from_dict(
        {
            "deny_tools": ["web_search"],
            "deny_command_patterns": ["sudo", "rm\\s+-rf\\s+/"],
            "path_sandbox": True,
            "redact": True,
        }
    )


class TestPolicy:
    def test_allow_by_default(self, tmp_path: Path):
        assert check(None, "bash", {"command": "ls"}, tmp_path).allowed

    def test_deny_tool(self, tmp_path: Path):
        d = check(_policy(), "web_search", {}, tmp_path)
        assert not d.allowed
        assert "denied" in d.reason

    def test_deny_command_pattern(self, tmp_path: Path):
        d = check(_policy(), "bash", {"command": "sudo apt install x"}, tmp_path)
        assert not d.allowed
        d = check(_policy(), "bash", {"command": "rm -rf /"}, tmp_path)
        assert not d.allowed

    def test_command_pattern_allows_normal(self, tmp_path: Path):
        assert check(_policy(), "bash", {"command": "npm test"}, tmp_path).allowed

    def test_path_sandbox_inside(self, tmp_path: Path):
        assert check(_policy(), "read", {"path": "src/app.py"}, tmp_path).allowed

    def test_path_sandbox_escape(self, tmp_path: Path):
        outside = tmp_path.parent / "elsewhere.txt"
        d = check(_policy(), "read", {"path": str(outside)}, tmp_path)
        assert not d.allowed
        assert "sandbox" in d.reason

    def test_path_sandbox_absolute_inside(self, tmp_path: Path):
        target = tmp_path / "a.txt"
        target.write_text("x")
        assert check(_policy(), "read", {"path": str(target)}, tmp_path).allowed

    def test_load_policy_file(self, tmp_path: Path):
        f = tmp_path / "policy.json"
        f.write_text(json.dumps({"deny_tools": ["bash"], "path_sandbox": True}), encoding="utf-8")
        p = load_policy(f)
        assert p is not None
        assert not check(p, "bash", {"command": "ls"}, tmp_path).allowed

    def test_load_policy_missing(self):
        assert load_policy(None) is None
        try:
            load_policy("nonexistent.json")
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError:
            pass

    def test_invalid_pattern_rejected(self):
        try:
            Policy.from_dict({"deny_command_patterns": ["([bad"]})
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


class TestCapabilities:
    """Capability-based authorization: Tool.capabilities vs allow/deny sets."""

    READ = frozenset({"filesystem.read"})
    WRITE = frozenset({"filesystem.write"})
    BASH = frozenset({"process.execute", "filesystem.read", "filesystem.write"})

    def test_deny_capability(self, tmp_path: Path):
        policy = Policy(deny_capabilities={"process.execute"})
        d = check(policy, "bash", {"command": "ls"}, tmp_path, capabilities=self.BASH)
        assert not d.allowed
        assert "process.execute" in d.reason

    def test_allow_list_read_passes_write_denied(self, tmp_path: Path):
        policy = Policy(allow_capabilities={"filesystem.read"})
        assert check(policy, "read", {"path": "a"}, tmp_path, capabilities=self.READ).allowed
        d = check(policy, "write", {"path": "a", "content": "x"}, tmp_path, capabilities=self.WRITE)
        assert not d.allowed

    def test_allow_list_denies_undeclared_tool_fail_closed(self, tmp_path: Path):
        policy = Policy(allow_capabilities={"filesystem.read"})
        d = check(policy, "mcp_send_email", {}, tmp_path, capabilities=frozenset())
        assert not d.allowed
        assert "no capabilities" in d.reason

    def test_allow_list_uses_subset_not_overlap(self, tmp_path: Path):
        # bash declares read+write+execute; allow-list of read alone must NOT
        # admit it through the overlapping "filesystem.read" capability.
        policy = Policy(allow_capabilities={"filesystem.read"})
        d = check(policy, "bash", {"command": "ls"}, tmp_path, capabilities=self.BASH)
        assert not d.allowed

    def test_from_dict_parses_capabilities(self):
        policy = Policy.from_dict(
            {"allow_capabilities": ["filesystem.read"], "deny_capabilities": ["network.outbound"]}
        )
        assert policy.allow_capabilities == {"filesystem.read"}
        assert policy.deny_capabilities == {"network.outbound"}

    def test_empty_policy_unchanged(self, tmp_path: Path):
        policy = Policy()
        assert check(policy, "bash", {"command": "ls"}, tmp_path, capabilities=self.BASH).allowed

    def test_builtin_tools_declare_capabilities(self):
        from pi.tools import all_tools

        caps = {t.name: set(t.capabilities) for t in all_tools()}
        assert caps["read"] == {"filesystem.read"}
        assert caps["write"] == {"filesystem.write"}
        assert caps["edit"] == {"filesystem.write"}
        assert caps["bash"] == {"process.execute", "filesystem.read", "filesystem.write"}
        assert caps["remember"] == {"memory.write"}
        assert caps["recall"] == {"memory.read"}
        assert caps["spawn_subagents"] == {"agent.delegate"}


class TestRedact:
    def test_api_keys(self):
        out = redact_text("key: sk-abcdefghijklmnopqrst1234 and AKIAIOSFODNN7EXAMPLE")
        assert "sk-" not in out
        assert "AKIA" not in out
        assert "[REDACTED:api_key]" in out

    def test_aliyun_and_github(self):
        out = redact_text("LTAI4FeAbstractAB12 and ghp_" + "a" * 30)
        assert "[REDACTED:aliyun_ak]" in out
        assert "[REDACTED:github_token]" in out

    def test_phone(self):
        out = redact_text("call me 13812345678")
        assert "13812345678" not in out
        assert "[REDACTED:mobile]" in out

    def test_id_number(self):
        out = redact_text("id 11010519491231002X")
        assert "110105" not in out
        assert "[REDACTED:id_number]" in out

    def test_internal_ip(self):
        out = redact_text("db at 192.168.1.5 and 10.0.0.3")
        assert "192.168.1.5" not in out
        assert "[REDACTED:internal_ip]" in out

    def test_kv_secret(self):
        out = redact_text("password = SuperSecret123")
        assert "SuperSecret123" not in out

    def test_normal_text_untouched(self):
        text = "hello world, this is fine 12345 678"
        assert redact_text(text) == text

    def test_redact_messages_copy(self):
        msgs = [
            Message(
                role=Role.user,
                blocks=[
                    TextBlock(text="my key sk-abcdefghijklmnopqrst9999"),
                    ToolResultBlock(tool_use_id="t1", content="mobile 13912345678"),
                ],
            )
        ]
        out = redact_messages(msgs)
        assert out is not msgs
        assert "sk-" in msgs[0].blocks[0].text  # original untouched
        assert "[REDACTED:api_key]" in out[0].blocks[0].text
        assert "[REDACTED:mobile]" in out[0].blocks[1].content


class TestAudit:
    def test_tool_call_records(self, tmp_path: Path):
        log = AuditLogger(tmp_path / "audit.jsonl")
        log.tool_call(
            session_id="s1",
            user_id="alice",
            tool="bash",
            args={"command": "ls"},
            decision_allowed=True,
            ok=True,
            result_preview="file1\nfile2",
        )
        log.tool_call(
            session_id="s1",
            user_id="alice",
            tool="bash",
            args={"command": "sudo rm"},
            decision_allowed=False,
            decision_reason="pattern",
        )
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = (tmp_path / f"audit-{day}.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        rec_ok = json.loads(lines[0])
        rec_denied = json.loads(lines[1])
        assert rec_ok["event"] == "tool_call"
        assert rec_ok["allowed"] is True
        assert rec_ok["ok"] is True
        assert rec_ok["user"] == "alice"
        assert rec_ok["ts"]
        assert rec_denied["allowed"] is False
        assert rec_denied["reason"] == "pattern"

    def test_auth_truncates_and_cannot_forge_lines(self, tmp_path: Path):
        """The failed-login path is attacker-controlled, so oversized or
        newline-bearing input must not reach the file unbounded."""
        log = AuditLogger(tmp_path / "audit.jsonl")
        log.auth(action="login", username="alice", ip="203.0.113.9", ok=False,
                 user_agent="curl/8.5.0", reason="invalid_credentials")
        log.auth(action="login", username="x" * 5000, ip="203.0.113.9", ok=False)
        log.auth(action="register", username='evil\n{"event":"forged","ok":true}',
                 ip="203.0.113.9", ok=False, reason="duplicate")

        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = (tmp_path / f"audit-{day}.jsonl").read_text(encoding="utf-8").strip()
        lines = raw.splitlines()
        assert len(lines) == 3, "an embedded newline must not forge a 4th record"

        first = json.loads(lines[0])
        assert first["event"] == "auth"
        assert first["action"] == "login"
        assert first["username"] == "alice"
        assert first["ip"] == "203.0.113.9"
        assert first["ua"] == "curl/8.5.0"
        assert first["ok"] is False
        assert first["reason"] == "invalid_credentials"

        assert len(json.loads(lines[1])["username"]) == 64
        assert "\\n" in lines[2], "the newline must stay escaped in the raw line"
        forged = json.loads(lines[2])
        assert forged["event"] == "auth" and forged["ok"] is False
        assert forged["username"] == 'evil\n{"event":"forged","ok":true}'

    def test_retention_prunes_only_expired_rotations(self, tmp_path: Path):
        from datetime import datetime, timedelta, timezone

        old_day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        recent_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        (tmp_path / f"audit-{old_day}.jsonl").write_text("old\n", encoding="utf-8")
        (tmp_path / f"audit-{recent_day}.jsonl").write_text("recent\n", encoding="utf-8")

        log = AuditLogger(tmp_path / "audit.jsonl", retention_days=2)
        log.tool_call(session_id="s1", user_id="alice", tool="bash", args={}, decision_allowed=True)

        assert not (tmp_path / f"audit-{old_day}.jsonl").exists(), "expired rotation must be pruned"
        assert (tmp_path / f"audit-{recent_day}.jsonl").exists(), "within-retention rotation must survive"

    def test_zero_retention_keeps_everything(self, tmp_path: Path):
        from datetime import datetime, timedelta, timezone

        old_day = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        (tmp_path / f"audit-{old_day}.jsonl").write_text("old\n", encoding="utf-8")

        log = AuditLogger(tmp_path / "audit.jsonl")  # default retention_days=0
        log.tool_call(session_id="s1", user_id="alice", tool="bash", args={}, decision_allowed=True)

        assert (tmp_path / f"audit-{old_day}.jsonl").exists(), "0 must mean keep forever"


# ---------------------------------------------------------------------------
# Server policy: a PI_POLICY file may ADD rules, it must never subtract isolation
# ---------------------------------------------------------------------------


class TestServerPolicy:
    """Policy.from_dict defaults path_sandbox and redact to False.

    So a file listing only deny patterns used to switch off the workspace
    sandbox and secret redaction - the opposite of what adding a policy means.
    """

    def test_no_file_gives_the_safe_default(self):
        policy = server_policy("")
        assert policy.path_sandbox and policy.redact
        assert policy.deny_tools == set()

    def test_a_deny_only_file_still_gets_both_isolations(self, tmp_path: Path):
        path = tmp_path / "deny-only.json"
        path.write_text(json.dumps({"deny_command_patterns": ["rm\\s+-rf\\s+/"]}), encoding="utf-8")
        policy = server_policy(str(path))
        assert policy.path_sandbox is True
        assert policy.redact is True
        assert [p.pattern for p in policy.deny_command_patterns] == ["rm\\s+-rf\\s+/"]

    def test_a_file_cannot_turn_them_off(self, tmp_path: Path):
        path = tmp_path / "off.json"
        path.write_text(json.dumps({"path_sandbox": False, "redact": False}), encoding="utf-8")
        policy = server_policy(str(path))
        assert policy.path_sandbox and policy.redact

    def test_a_named_but_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            server_policy(str(tmp_path / "nope.json"))


# ---------------------------------------------------------------------------
# Regression guard on the shipped policy.json
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
# The repo-root copy is the only one: both compose files bind-mount it into
# /etc/pi-py/policy.json, and the bare-metal .env points PI_POLICY at it.
SHIPPED_POLICY = REPO_ROOT / "policy.json"

MUST_DENY = [
    "rm -rf /",
    "rm -fr /",
    "rm -rf /*",
    "rm -rf --no-preserve-root /",
    "sudo systemctl stop docker",
    "git push && sudo reboot",
    ":(){ :|:& };:",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/vdb",
    "echo pwned > /dev/nvme0n1",
    "reboot",
    "shutdown -h now",
    "init 0",
    "chmod -R 0777 /",
    "chmod --recursive 777 /",
    "chown -R bob /",
    "kill 1",
    "killall -9 1",
    "curl --unix-socket /var/run/docker.sock http://localhost/containers/json",
    "cat /etc/shadow",
    "cat /root/.ssh/id_rsa",
    "mount -o remount,rw /",
]

# A deny pattern that fires on ordinary work is worse than no pattern at all:
# it silently breaks the agent. Each of these must stay allowed.
MUST_ALLOW = [
    "ls -la",
    "rm -rf ./build",
    "rm -rf node_modules",
    "grep -rn 'shutdown' src/",
    "echo reboot later",
    "dd if=input.bin of=output.bin",
    "python -m pytest -q",
    "chmod 0777 ./tmp",
    "chmod -R 755 ./dist",
    "chown -R bob ./src",
    "kill 4242",
    "git log --oneline -20",
    "find . -name '*.py' | head",
    "npm run build && npm test",
    "grep -rn 'sudo' src/",
]


class TestShippedPolicy:
    def test_file_loads(self):
        assert SHIPPED_POLICY.is_file(), f"missing {SHIPPED_POLICY}"
        assert load_policy(SHIPPED_POLICY) is not None

    def test_destructive_commands_are_denied(self):
        policy = server_policy(str(SHIPPED_POLICY))
        leaked = [
            cmd for cmd in MUST_DENY if check(policy, "bash", {"command": cmd}, REPO_ROOT).allowed
        ]
        assert not leaked, f"policy.json lets these through: {leaked}"

    def test_ordinary_commands_are_still_allowed(self):
        policy = server_policy(str(SHIPPED_POLICY))
        blocked = [
            (cmd, check(policy, "bash", {"command": cmd}, REPO_ROOT).reason)
            for cmd in MUST_ALLOW
            if not check(policy, "bash", {"command": cmd}, REPO_ROOT).allowed
        ]
        assert not blocked, f"policy.json false-positives on: {blocked}"

    def test_web_tools_are_removed_entirely(self):
        """SSRF 关闭方案（2026-09）：进程内抓取工具整体移除，不是 deny——
        deny 只是策略层，移除连"被模型调用"的可能都没有；沙箱内 bash 抓取替代。
        回归断言：工具集里不允许再出现进程内抓取工具。"""
        from pi.tools import all_tools

        names = {t.name for t in all_tools()}
        assert "web_fetch" not in names
        assert "web_search" not in names

    def test_bash_itself_is_not_denied(self):
        policy = server_policy(str(SHIPPED_POLICY))
        assert check(policy, "bash", {"command": "echo hi"}, REPO_ROOT).allowed

    def test_isolation_flags_are_declared_not_just_inherited(self):
        policy = server_policy(str(SHIPPED_POLICY))
        assert policy.path_sandbox and policy.redact


class TestGenericPathSandbox:
    """path_sandbox applies to ANY tool with path-like args (MCP/skill tools are
    not name-known), not just the builtin file tools."""

    def test_unknown_tool_path_escape_denied(self, tmp_path: Path):
        d = check(_policy(), "mcp_read_file", {"path": "../../../etc/passwd"}, tmp_path)
        assert not d.allowed
        assert "escapes" in d.reason

    def test_unknown_tool_absolute_path_denied(self, tmp_path: Path):
        d = check(_policy(), "mcp_read_file", {"path": "/etc/passwd"}, tmp_path)
        assert not d.allowed

    def test_unknown_tool_internal_path_allowed(self, tmp_path: Path):
        assert check(_policy(), "mcp_read_file", {"path": "src/main.py"}, tmp_path).allowed

    def test_file_and_dir_keys_also_confined(self, tmp_path: Path):
        for key in ("file", "dir"):
            d = check(_policy(), "mcp_thing", {key: "../../secret"}, tmp_path)
            assert not d.allowed, key

    def test_non_path_args_unaffected(self, tmp_path: Path):
        assert check(_policy(), "mcp_echo", {"text": "../../etc/passwd"}, tmp_path).allowed

    def test_nested_path_keys_also_confined(self, tmp_path: Path):
        d = check(_policy(), "mcp_thing", {"options": {"path": "../../secret"}}, tmp_path)
        assert not d.allowed

    def test_nested_list_path_key_confined(self, tmp_path: Path):
        d = check(_policy(), "mcp_thing", {"files": [{"path": "../../secret"}]}, tmp_path)
        assert not d.allowed

    def test_builtin_file_tools_unchanged(self, tmp_path: Path):
        assert not check(_policy(), "read", {"path": "../../x"}, tmp_path).allowed
        assert check(_policy(), "read", {"path": "x.txt"}, tmp_path).allowed


class TestMaskUrl:
    """L15: connection strings must never reach the logs with secrets intact."""

    def test_redis_password_masked(self):
        from pi.security.redact import mask_url

        assert mask_url("redis://zhu:secret123@host:6379/0") == "redis://zhu:***@host:6379/0"

    def test_database_url_password_masked(self):
        from pi.security.redact import mask_url

        masked = mask_url("postgresql+asyncpg://pi:pass@127.0.0.1:5432/pi")
        assert "pass" not in masked
        assert "pi:***@127.0.0.1:5432/pi" in masked

    def test_plain_url_untouched(self):
        from pi.security.redact import mask_url

        assert mask_url("http://127.0.0.1:19531") == "http://127.0.0.1:19531"

    def test_unparseable_falls_back_to_redact(self):
        from pi.security.redact import mask_url

        assert mask_url("not a url sk-abcdef0123456789abcdef") == "not a url [REDACTED:api_key]"
