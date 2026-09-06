"""Security layer tests: policy engine, redaction, audit log, server policy."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from pi.models import Message, Role, TextBlock, ToolResultBlock
from pi.security.audit import AuditLogger
from pi.security.policy import Policy, check, load_policy
from pi.security.redact import redact_messages, redact_text
from pi.server.runner import server_policy
from pi.tools import all_tools


def _policy() -> Policy:
    return Policy.from_dict(
        {
            # a fictional name: this exercises the deny_tools mechanism, it makes no
            # claim about which tools are registered
            "deny_tools": ["danger_tool"],
            "deny_command_patterns": ["sudo", "rm\\s+-rf\\s+/"],
            "path_sandbox": True,
            "redact": True,
        }
    )


class TestPolicy:
    def test_allow_by_default(self, tmp_path: Path):
        assert check(None, "bash", {"command": "ls"}, tmp_path).allowed

    def test_deny_tool(self, tmp_path: Path):
        d = check(_policy(), "danger_tool", {}, tmp_path)
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

    def test_a_failing_insert_never_stops_the_drainer(self, tmp_path: Path):
        """The MySQL sink degrades, it does not die: one failed batch is lost
        from the table (the JSONL mirror keeps it) and the next batch lands."""
        import asyncio

        logger = AuditLogger(tmp_path / "audit.jsonl")
        written: list[str] = []
        calls: list[int] = []

        class FlakyRepo:
            async def append_many(self, records):
                calls.append(len(records))
                if len(calls) == 1:
                    raise RuntimeError("database is down")
                written.extend(r["username"] for r in records)
                return len(records)

        async def main() -> None:
            queue: asyncio.Queue = asyncio.Queue(maxsize=100)
            task = asyncio.create_task(logger._drain(FlakyRepo(), queue))
            logger._db_queue = queue
            logger._drain_task = task
            logger.auth(action="login", username="alice", ip="1.2.3.4", ok=True)
            # Let the first (failing) batch actually run before queuing more,
            # or all records coalesce into the one failed batch.
            for _ in range(200):
                if calls:
                    break
                await asyncio.sleep(0.01)
            logger.auth(action="login", username="bob", ip="1.2.3.4", ok=True)
            logger.auth(action="login", username="carol", ip="1.2.3.4", ok=True)
            for _ in range(200):
                if "carol" in written:
                    break
                await asyncio.sleep(0.01)
            await logger.close()

        asyncio.run(main())

        assert "bob" in written and "carol" in written, "records after the failure land"
        assert "alice" not in written, "the failed batch is dropped from the table"
        assert len(calls) >= 2, "the drainer survived the first failure"

        # The mirror kept every record, including whatever the failed batch lost.
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = (tmp_path / f"audit-{day}.jsonl").read_text(encoding="utf-8").splitlines()
        assert {json.loads(line)["username"] for line in lines} == {"alice", "bob", "carol"}


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

    def test_the_local_tool_surface_has_no_internet_tool(self):
        # web_fetch/web_search were deleted rather than fixed. A local fetcher runs
        # inside the app process, where PI_SANDBOX=docker and --network none do not
        # constrain it at all, so with no address validation it was an open SSRF path
        # to the cloud metadata service. Internet access is the model endpoint's own
        # builtin tools now, fetched provider-side.
        #
        # This pins the invariant that actually replaced the deny_tools entry: no
        # registered tool may perform outbound network I/O. sandbox.py is not in
        # all_tools() and is excluded on purpose - its httpx use talks to the Docker
        # Engine API over PI_DOCKER_HOST, not to the internet.
        for tool in all_tools():
            module = sys.modules[type(tool).__module__]
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert not re.search(
                r"^\s*(?:import|from)\s+(?:httpx|urllib|requests|aiohttp)\b", source, re.M
            ), f"{tool.name} ({module.__name__}) performs outbound network I/O"

    def test_bash_itself_is_not_denied(self):
        policy = server_policy(str(SHIPPED_POLICY))
        assert check(policy, "bash", {"command": "echo hi"}, REPO_ROOT).allowed

    def test_isolation_flags_are_declared_not_just_inherited(self):
        policy = server_policy(str(SHIPPED_POLICY))
        assert policy.path_sandbox and policy.redact
