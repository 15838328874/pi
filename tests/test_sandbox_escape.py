"""Adversarial sandbox tests: run REAL docker containers and verify containment.

Unlike tests/test_sandbox_pool.py (a FakeTransport), these actually execute escape
attempts against the shipped DockerRunner. They are the "prove the boundary holds"
counterpart to the unit suite: 521 green unit tests do not mean the model cannot
escape. Skipped when the docker CLI is absent, so environments without docker stay
green; on GitHub Actions ubuntu-latest docker is present and these run.

Image: default python:3.12-slim (pullable in CI). Point PI_TEST_SANDBOX_IMAGE at a
prebuilt image (e.g. 127.0.0.1:5000/pi-sandbox:1.0) to run locally without pulling.

Safety: every case runs under explicit cgroup ceilings (memory/pids/cpus) and
`--rm`, so a failure degrades to a killed container, never host exhaustion.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from pi.tools.sandbox import DockerRunner, SandboxLimits

IMAGE = os.environ.get("PI_TEST_SANDBOX_IMAGE", "python:3.12-slim")

_HAS_DOCKER = shutil.which("docker") is not None

pytestmark = pytest.mark.skipif(not _HAS_DOCKER, reason="docker CLI not available")


def _run(command: str, cwd: Path, *, memory: str = "128m", pids: int = 64, timeout: int = 40) -> object:
    """Run one command inside a fresh cold-path container and return its result."""

    async def go():
        runner = DockerRunner(
            image=IMAGE,
            allow_network=False,
            limits=SandboxLimits(memory=memory, pids=pids, cpus="1.0"),
        )
        return await runner.run(command, cwd, timeout)

    return asyncio.run(go())


def test_smoke_binary_works(tmp_path: Path):
    """The image boots and executes - catches image-pull problems distinctly."""
    res = _run('python3 -c "print(6*7)"', tmp_path)
    assert res.exit_code == 0, res.output
    assert "42" in res.output


def test_network_none_blocks_egress(tmp_path: Path):
    """--network none must make every outbound socket fail."""
    res = _run(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\"",
        tmp_path,
    )
    assert res.exit_code != 0, f"egress should have failed, got: {res.output}"


def test_host_secrets_do_not_leak_into_container(tmp_path: Path):
    """The container must not inherit the app's environment (PI_JWT_SECRET etc.)."""
    res = _run(
        "python3 -c \"import os; print(repr(os.environ.get('PI_JWT_SECRET')))\"",
        tmp_path,
    )
    assert res.exit_code == 0, res.output
    assert "None" in res.output, f"secret leaked into sandbox: {res.output}"


def test_docker_socket_not_mounted(tmp_path: Path):
    """No path from the sandbox to the host docker daemon."""
    res = _run("ls /var/run/docker.sock 2>&1; exit 0", tmp_path)
    assert "No such file" in res.output, f"docker.sock visible: {res.output}"


def test_runs_as_non_root(tmp_path: Path):
    """Containers run as the app's uid, never root."""
    res = _run("id -u", tmp_path)
    assert res.exit_code == 0, res.output
    assert res.output.strip() != "0", "sandbox ran as root"


def test_pids_limit_blocks_process_explosion(tmp_path: Path):
    """A fork bomb must be stopped by --pids-limit, not exhaust the host.

    Uses os.fork (children share pages via CoW, so RSS barely grows) so the
    pids ceiling is the ONLY limit in play - a spawn-and-exec loop would trip
    the 128m memory ceiling first and give a misleading "Killed" (exit 137).
    """
    script = (
        "import os, time\n"
        "count = 0\n"
        "try:\n"
        "    while True:\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(60)\n"
        "            os._exit(0)\n"
        "        count += 1\n"
        "except OSError as e:\n"
        "    print('PIDS_LIMIT_HIT', count, e)\n"
        "    raise SystemExit(0)\n"
        "print('FORKED_ALL', count)\n"
        "raise SystemExit(1)\n"
    )
    res = _run(f'python3 -c "{script}"', tmp_path, pids=64, timeout=60)
    assert res.exit_code == 0, f"process explosion not contained: {res.output}"
    assert "PIDS_LIMIT_HIT" in res.output, res.output


def test_memory_limit_blocks_allocation(tmp_path: Path):
    """512MB allocation under a 128m ceiling must fail (memory + no-swap)."""
    res = _run(
        "python3 -c \"x = bytearray(512*1024*1024); print('ALLOCATED')\"",
        tmp_path,
        memory="128m",
        timeout=60,
    )
    assert res.exit_code != 0, f"512MB under 128m limit should fail, got: {res.output}"
    assert "ALLOCATED" not in res.output


def test_workspace_bind_mount_is_the_only_host_path(tmp_path: Path):
    """The container sees the workspace at /ws and nothing else of the host."""
    marker = tmp_path.parent / "HOST_MARKER_SHOULD_NOT_BE_VISIBLE"
    marker.write_text("secret", encoding="utf-8")
    try:
        # /ws is tmp_path; /ws/.. is the container root, NOT the host parent dir.
        res = _run("cat /ws/../HOST_MARKER_SHOULD_NOT_BE_VISIBLE 2>&1; echo EXIT=$?", tmp_path)
        assert "No such file" in res.output, f"host file outside workspace leaked: {res.output}"
    finally:
        marker.unlink(missing_ok=True)
