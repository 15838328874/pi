"""Warm-pool sandbox tests against a fake docker transport (no real docker).

Covers: per-workspace reuse, preheat, concurrent acquire de-dup, idle
recycling, LRU eviction + soft overshoot, timeout recycling, transparent
rebuild of vanished containers, fail-closed create errors, shutdown cleanup,
get_runner pool/cold selection, container resource limits, and rejection of an
unrecognised PI_SANDBOX value.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

from pi.tools.sandbox import (
    CommandResult,
    ContainerGoneError,
    DockerPool,
    DockerRunner,
    ExecTimeoutError,
    LocalRunner,
    SandboxLimits,
    SandboxUnavailableError,
    _parse_size,
    get_runner,
    validate_sandbox_mode,
)


class FakeTransport:
    """Duck-typed stand-in for CliTransport / ApiTransport."""

    def __init__(self, *, create_delay: float = 0.0, exec_delay: float = 0.0,
                 fail_create: bool = False):
        self.created: list[tuple[str, str, str]] = []  # (cid, bind, workdir)
        self.removed: list[str] = []
        self.execs: list[tuple[str, str]] = []  # (cid, command)
        self._next = 0
        self.create_delay = create_delay
        self.exec_delay = exec_delay
        self.fail_create = fail_create
        self.gone_cids: set[str] = set()  # containers whose exec reports "gone"
        self.timeout_commands: set[str] = set()

    async def create_warm(self, bind: str, workdir: str) -> str:
        if self.fail_create:
            raise SandboxUnavailableError("daemon down")
        await asyncio.sleep(self.create_delay)
        self._next += 1
        cid = f"cid-{self._next}"
        self.created.append((cid, bind, workdir))
        return cid

    async def exec(self, container_id: str, command: str, workdir: str, timeout: int):
        await asyncio.sleep(self.exec_delay)
        if container_id in self.gone_cids:
            raise ContainerGoneError(f"No such container: {container_id}")
        if command in self.timeout_commands:
            raise ExecTimeoutError()
        self.execs.append((container_id, command))
        return (f"out:{command}", 0)

    async def remove(self, container_id: str) -> None:
        self.removed.append(container_id)


def make_pool(transport: FakeTransport, **kwargs) -> DockerPool:
    defaults = dict(
        image="python:3.12-slim",
        allow_network=False,
        transport=transport,
        pool_max=16,
        idle_ttl=600.0,
        sweep_interval=10.0,
    )
    defaults.update(kwargs)
    return DockerPool(**defaults)


class TestWarmPoolReuse:
    def test_calls_in_one_workspace_share_a_container(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            pool = make_pool(t)
            try:
                r1 = await pool.run("echo one", tmp_path, 10)
                r2 = await pool.run("echo two", tmp_path, 10)
                return t, r1, r2
            finally:
                await pool.shutdown()

        t, r1, r2 = asyncio.run(main())
        assert r1.exit_code == 0 and "out:echo one" in r1.output
        assert r2.exit_code == 0 and "out:echo two" in r2.output
        assert len(t.created) == 1  # ONE container, TWO execs
        assert {e[0] for e in t.execs} == {t.created[0][0]}  # same container

    def test_separate_workspaces_get_own_containers(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            pool = make_pool(t)
            try:
                await pool.run("ls", tmp_path / "a", 10)
                await pool.run("ls", tmp_path / "b", 10)
                return t
            finally:
                await pool.shutdown()

        t = asyncio.run(main())
        assert len(t.created) == 2
        binds = {bind for _, bind, _ in t.created}
        assert len(binds) == 2  # each container mounted its own workspace

    def test_concurrent_acquire_creates_once(self, tmp_path: Path):
        async def main():
            t = FakeTransport(create_delay=0.05)
            pool = make_pool(t)
            try:
                results = await asyncio.gather(
                    *(pool.run(f"cmd{i}", tmp_path, 10) for i in range(5))
                )
                return t, results
            finally:
                await pool.shutdown()

        t, results = asyncio.run(main())
        assert len(t.created) == 1
        assert all(r.exit_code == 0 for r in results)
        assert len(t.execs) == 5


class TestPrewarm:
    def test_prewarm_creates_container_before_first_call(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            pool = make_pool(t)
            try:
                pool.prewarm(tmp_path)
                await asyncio.sleep(0.05)
                mid_created = len(t.created)
                mid_execs = len(t.execs)
                r = await pool.run("first", tmp_path, 10)
                return t, mid_created, mid_execs, r
            finally:
                await pool.shutdown()

        t, mid_created, mid_execs, r = asyncio.run(main())
        assert mid_created == 1  # container hot before any bash call
        assert mid_execs == 0  # but nothing executed yet
        assert r.exit_code == 0
        assert len(t.created) == 1  # run reused the prewarmed container

    def test_prewarm_failure_does_not_raise(self, tmp_path: Path):
        async def main():
            t = FakeTransport(fail_create=True)
            pool = make_pool(t)
            try:
                pool.prewarm(tmp_path)  # must not raise
                await asyncio.sleep(0.05)
                r = await pool.run("x", tmp_path, 10)  # fail-closed result
                return r
            finally:
                await pool.shutdown()

        r = asyncio.run(main())
        assert r.exit_code == -1
        assert r.output.startswith("Error:")


class TestRecycle:
    def test_idle_entries_are_swept_and_rebuilt_on_demand(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            pool = make_pool(t, idle_ttl=0.05, sweep_interval=0.02)
            try:
                await pool.run("one", tmp_path, 10)
                first_cid = t.created[0][0]
                await asyncio.sleep(0.3)  # let the sweeper reclaim it
                swept = first_cid in t.removed and not pool._entries
                r2 = await pool.run("two", tmp_path, 10)
                return t, first_cid, swept, r2
            finally:
                await pool.shutdown()

        t, first_cid, swept, r2 = asyncio.run(main())
        assert swept, f"container {first_cid} should be idle-recycled"
        assert r2.exit_code == 0
        assert len(t.created) == 2  # second call rebuilt a fresh container

    def test_timeout_recycles_container(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            t.timeout_commands.add("boom")
            pool = make_pool(t)
            try:
                r = await pool.run("boom", tmp_path, 5)
                r2 = await pool.run("ok", tmp_path, 5)
                return t, r, r2
            finally:
                await pool.shutdown()

        t, r, r2 = asyncio.run(main())
        assert r.timed_out and r.exit_code == -1
        assert t.created[0][0] in t.removed  # dirty container destroyed
        assert r2.exit_code == 0
        assert len(t.created) == 2  # next call got a fresh one

    def test_vanished_container_is_rebuilt_transparently(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            t.gone_cids.add("cid-1")  # first container "dies" under us
            pool = make_pool(t)
            try:
                r = await pool.run("cmd", tmp_path, 10)
                return t, r
            finally:
                await pool.shutdown()

        t, r = asyncio.run(main())
        assert r.exit_code == 0 and "out:cmd" in r.output
        assert len(t.created) == 2  # rebuilt once after ContainerGone
        assert t.created[0][0] in t.removed

    def test_vanished_twice_is_an_error_not_a_hang(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            t.gone_cids.update({"cid-1", "cid-2"})  # rebuild is gone too
            pool = make_pool(t)
            try:
                return await pool.run("cmd", tmp_path, 10)
            finally:
                await pool.shutdown()

        r = asyncio.run(main())
        assert r.exit_code == -1
        assert "vanished" in r.output


class TestScaling:
    def test_lru_eviction_at_capacity(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            pool = make_pool(t, pool_max=2)
            try:
                await pool.run("a", tmp_path / "ws1", 10)
                await pool.run("b", tmp_path / "ws2", 10)
                await pool.run("c", tmp_path / "ws3", 10)
                return t
            finally:
                await pool.shutdown()

        t = asyncio.run(main())
        assert len(t.created) == 3
        assert t.created[0][0] in t.removed  # idlest (ws1) evicted

    def test_all_busy_allows_soft_overshoot(self, tmp_path: Path):
        async def main():
            t = FakeTransport(create_delay=0.1)
            pool = make_pool(t, pool_max=1)
            try:
                r1, r2 = await asyncio.gather(
                    pool.run("a", tmp_path / "ws1", 10),
                    pool.run("b", tmp_path / "ws2", 10),
                )
                return t, r1, r2
            finally:
                await pool.shutdown()

        t, r1, r2 = asyncio.run(main())
        assert r1.exit_code == 0 and r2.exit_code == 0
        assert len(t.created) == 2  # exceeded max rather than blocking


class TestFailClosedAndShutdown:
    def test_create_failure_is_fail_closed(self, tmp_path: Path):
        async def main():
            t = FakeTransport(fail_create=True)
            pool = make_pool(t)
            try:
                return await pool.run("x", tmp_path, 10)
            finally:
                await pool.shutdown()

        r = asyncio.run(main())
        assert r.exit_code == -1
        assert r.output.startswith("Error: sandbox spawn failed:")

    def test_shutdown_removes_every_container(self, tmp_path: Path):
        async def main():
            t = FakeTransport()
            pool = make_pool(t)
            await pool.run("a", tmp_path / "ws1", 10)
            await pool.run("b", tmp_path / "ws2", 10)
            await pool.shutdown()
            after = await pool.run("c", tmp_path / "ws1", 10)
            return t, after

        t, after = asyncio.run(main())
        assert {c for c, _, _ in t.created} == set(t.removed)
        assert after.exit_code == -1  # closed pool refuses work

    def test_local_runner_prewarm_is_noop(self, tmp_path: Path):
        LocalRunner().prewarm(tmp_path)  # must not raise

    def test_docker_runner_prewarm_is_noop(self, tmp_path: Path):
        import pi.tools.sandbox as sb

        original = sb.shutil.which
        sb.shutil.which = lambda _: "/usr/bin/docker"
        try:
            DockerRunner().prewarm(tmp_path)  # must not raise
        finally:
            sb.shutil.which = original


class TestGetRunnerSelection:
    def test_pool_is_default_for_docker_mode(self, monkeypatch, tmp_path: Path):
        import pi.tools.sandbox as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.delenv("PI_DOCKER_HOST", raising=False)
        monkeypatch.delenv("PI_SANDBOX_POOL", raising=False)
        monkeypatch.setattr(sb, "_pool", None)
        r1 = get_runner("docker")
        assert isinstance(r1, DockerPool)
        r2 = get_runner("docker")  # singleton reused
        assert r2 is r1

    def test_pool_disabled_falls_back_to_cold_path(self, monkeypatch):
        import pi.tools.sandbox as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.delenv("PI_DOCKER_HOST", raising=False)
        monkeypatch.setenv("PI_SANDBOX_POOL", "0")
        assert isinstance(get_runner("docker"), DockerRunner)

    def test_docker_mode_fails_closed_without_docker(self, monkeypatch):
        import pi.tools.sandbox as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _: None)
        monkeypatch.delenv("PI_DOCKER_HOST", raising=False)
        monkeypatch.setattr(sb, "_pool", None)
        with pytest.raises(RuntimeError, match="docker"):
            get_runner("docker")

    def test_shutdown_docker_pool_resets_singleton(self, monkeypatch):
        import pi.tools.sandbox as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.delenv("PI_DOCKER_HOST", raising=False)
        monkeypatch.setattr(sb, "_pool", None)
        pool = get_runner("docker")
        asyncio.run(sb.shutdown_docker_pool())
        assert sb._pool is None
        assert pool._closed


# ---------------------------------------------------------------------------
# Resource limits. Registration is open, so docker's own defaults (no memory
# cap, no pids cap, no cpu cap, container running as root) let any account
# exhaust the host with one command. Network isolation and the per-user bind
# mount do not cover that.
# ---------------------------------------------------------------------------


class _FakeProc:
    """Just enough of asyncio.subprocess.Process for create_warm()."""

    def __init__(self, out: bytes = b"cid-fake\n", returncode: int = 0):
        self._out = out
        self.returncode = returncode

    async def communicate(self):
        return (self._out, b"")

    def kill(self) -> None:
        pass


class TestParseSize:
    def test_suffixes(self):
        assert _parse_size("1g") == 1073741824
        assert _parse_size("512m") == 512 * 1024**2
        assert _parse_size("2gb") == 2 * 1024**3
        assert _parse_size("64k") == 64 * 1024
        assert _parse_size("1024") == 1024

    def test_case_and_space_insensitive(self):
        assert _parse_size("1G") == 1073741824
        assert _parse_size("  512 MB  ") == 512 * 1024**2

    def test_empty_is_zero_which_means_omit_the_flag(self):
        assert _parse_size("") == 0

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="invalid size"):
            _parse_size("lots")


class TestSandboxLimits:
    def test_effective_user_tracks_the_app_by_default(self):
        assert SandboxLimits(user="").effective_user == f"{os.getuid()}:{os.getgid()}"

    def test_effective_user_override_wins(self):
        assert SandboxLimits(user="10001:10001").effective_user == "10001:10001"

    def test_rejects_an_unparsable_memory(self):
        with pytest.raises(ValueError, match="invalid size"):
            SandboxLimits(memory="big")

    def test_rejects_an_unparsable_cpu_count(self):
        with pytest.raises(ValueError, match="PI_SANDBOX_CPUS"):
            SandboxLimits(cpus="many")

    def test_from_env_reads_the_four_knobs(self, monkeypatch):
        monkeypatch.setenv("PI_SANDBOX_MEMORY", "512m")
        monkeypatch.setenv("PI_SANDBOX_PIDS", "64")
        monkeypatch.setenv("PI_SANDBOX_CPUS", "0.5")
        monkeypatch.setenv("PI_SANDBOX_USER", "10001:10001")
        limits = SandboxLimits.from_env()
        assert (limits.memory, limits.pids, limits.cpus, limits.user) == (
            "512m", 64, "0.5", "10001:10001",
        )

    def test_cli_flags(self):
        limits = SandboxLimits(memory="512m", pids=64, cpus="0.5", user="10001:10001")
        assert limits.cli_flags() == [
            "--memory", "512m",
            "--memory-swap", "512m",
            "--pids-limit", "64",
            "--cpus", "0.5",
            "--user", "10001:10001",
        ]

    def test_memory_swap_equals_memory_so_swap_stays_off(self):
        # docker's default allows 2x the memory limit via swap
        flags = SandboxLimits(memory="1g").cli_flags()
        assert flags[flags.index("--memory") + 1] == flags[flags.index("--memory-swap") + 1]

    def test_unset_limits_omit_their_flags_but_user_always_resolves(self):
        flags = SandboxLimits(memory="", pids=0, cpus="", user="").cli_flags()
        for absent in ("--memory", "--memory-swap", "--pids-limit", "--cpus"):
            assert absent not in flags
        assert flags[flags.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"

    def test_host_config_uses_bytes_and_nano_cpus(self):
        # the Engine API will not accept the CLI's suffix strings
        cfg = SandboxLimits(memory="1g", pids=256, cpus="1.0").host_config()
        assert cfg == {
            "Memory": 1073741824,
            "MemorySwap": 1073741824,
            "PidsLimit": 256,
            "NanoCpus": 1_000_000_000,
        }

    def test_host_config_omits_unset_limits(self):
        assert SandboxLimits(memory="", pids=0, cpus="").host_config() == {}


class TestTransportsPassTheLimitsThrough:
    def test_cli_transport_splices_flags_before_the_image(self, monkeypatch):
        import pi.tools.sandbox as sb

        captured: list[tuple[str, ...]] = []

        async def fake_exec(*argv, **kwargs):
            captured.append(argv)
            return _FakeProc(b"cid-warm\n")

        monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(sb.asyncio, "create_subprocess_exec", fake_exec)
        transport = sb.CliTransport(
            image="python:3.12-slim", allow_network=False, warm_lifetime=900,
            limits=SandboxLimits(memory="512m", pids=64, cpus="0.5", user="10001:10001"),
        )
        assert asyncio.run(transport.create_warm("/ws/bind", "/ws")) == "cid-warm"

        argv = list(captured[0])
        # docker treats anything after the image name as the command, not a flag
        image_at = argv.index("python:3.12-slim")
        for flag in ("--memory", "--memory-swap", "--pids-limit", "--cpus", "--user", "--network"):
            assert argv.index(flag) < image_at
        assert argv[argv.index("--memory") + 1] == "512m"
        assert argv[argv.index("--pids-limit") + 1] == "64"
        assert argv[argv.index("--cpus") + 1] == "0.5"
        assert argv[argv.index("--user") + 1] == "10001:10001"

    def test_api_transport_puts_user_in_config_and_limits_in_host_config(self, monkeypatch):
        import pi.tools.sandbox as sb

        payloads: list[dict] = []

        class _Resp:
            def __init__(self, status_code: int, data: dict | None = None, text: str = ""):
                self.status_code = status_code
                self._data = data or {}
                self.text = text

            def json(self):
                return self._data

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, path, **kwargs):
                return _Resp(200)  # image already present, so no pull

            async def post(self, path, **kwargs):
                if path == "/containers/create":
                    payloads.append(kwargs["json"])
                    return _Resp(201, {"Id": "cid-api"})
                return _Resp(204)

        monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(AsyncClient=_Client))
        transport = sb.ApiTransport(
            "tcp://127.0.0.1:2375", image="python:3.12-slim", allow_network=False,
            warm_lifetime=900,
            limits=SandboxLimits(memory="1g", pids=256, cpus="1.0", user="10001:10001"),
        )
        assert asyncio.run(transport.create_warm("/ws/bind", "/ws")) == "cid-api"

        payload = payloads[0]
        # Config.User, not HostConfig: docker exec inherits it from the container
        assert payload["User"] == "10001:10001"
        assert payload["HostConfig"]["Memory"] == 1073741824
        assert payload["HostConfig"]["MemorySwap"] == 1073741824
        assert payload["HostConfig"]["PidsLimit"] == 256
        assert payload["HostConfig"]["NanoCpus"] == 1_000_000_000
        assert payload["HostConfig"]["NetworkMode"] == "none"


class TestSandboxModeValidation:
    def test_known_modes_pass(self):
        for mode in ("", "local", "docker"):
            validate_sandbox_mode(mode)  # must not raise

    def test_docker_pool_is_not_a_mode(self):
        # it used to fall through to LocalRunner, silently disabling the sandbox
        with pytest.raises(ValueError, match="docker-pool"):
            validate_sandbox_mode("docker-pool")

    def test_error_names_the_only_sandboxed_value(self):
        with pytest.raises(ValueError) as exc:
            validate_sandbox_mode("pooled")
        assert "PI_SANDBOX" in str(exc.value) and "'docker'" in str(exc.value)

    def test_get_runner_refuses_an_unknown_mode_before_touching_docker(self):
        with pytest.raises(ValueError, match="PI_SANDBOX"):
            get_runner("docker-pool")

    def test_local_modes_still_yield_the_local_runner(self):
        assert isinstance(get_runner(""), LocalRunner)
        assert isinstance(get_runner("local"), LocalRunner)

    def test_pool_is_rebuilt_when_limits_change(self, monkeypatch):
        import pi.tools.sandbox as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.delenv("PI_DOCKER_HOST", raising=False)
        monkeypatch.delenv("PI_SANDBOX_POOL", raising=False)
        monkeypatch.setattr(sb, "_pool", None)
        monkeypatch.setenv("PI_SANDBOX_MEMORY", "512m")
        first = sb._get_pool("python:3.12-slim", False)
        monkeypatch.setenv("PI_SANDBOX_MEMORY", "1g")
        second = sb._get_pool("python:3.12-slim", False)
        assert second is not first
        assert second.limits.memory == "1g"
