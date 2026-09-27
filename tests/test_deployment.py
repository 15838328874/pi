"""Deployment hardening tests: cache backends, sandbox runners, migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi.server.cache import MemoryBackend, get_backend
from pi.server.ratelimit import RateLimiter
from pi.tools.sandbox import DockerRunner, LocalRunner, get_runner


class TestMemoryBackend:
    def test_window_counter(self):
        async def main():
            b = MemoryBackend()
            counts = [await b.incr_window("u1", 60) for _ in range(3)]
            return counts, await b.ping()

        counts, ping = asyncio.run(main())
        assert counts == [1, 2, 3]
        assert ping is True

    def test_lock_acquire_release(self):
        async def main():
            b = MemoryBackend()
            first = await b.acquire_lock("s1", 60)
            second = await b.acquire_lock("s1", 60)
            await b.release_lock("s1")
            third = await b.acquire_lock("s1", 60)
            return first, second, third

        assert asyncio.run(main()) == (True, False, True)


class TestRateLimiterBackends:
    def test_memory_backend_limit(self):
        async def main():
            rl = RateLimiter(2, backend=MemoryBackend())
            return [await rl.allow("alice") for _ in range(3)]

        assert asyncio.run(main()) == [True, True, False]

    def test_default_backend_is_memory(self):
        rl = RateLimiter(5)
        assert isinstance(rl.backend, MemoryBackend)

    def test_redis_url_empty_is_memory(self):
        assert isinstance(get_backend(""), MemoryBackend)
        assert isinstance(get_backend(None), MemoryBackend)


class TestRedisBackend:
    def test_redis_backend_when_configured(self):
        """Constructs RedisBackend without connecting (lazy)."""
        backend = get_backend("redis://localhost:6399/9")
        assert not isinstance(backend, MemoryBackend)

    def test_redis_limiter_counter(self):
        """Live Redis test - skipped when no redis is running on the test port."""
        pytest.importorskip("redis")
        backend = get_backend("redis://localhost:6379/9", namespace="test-rl")

        async def main():
            if not await backend.ping():
                return None
            await backend._redis.delete("test-rl:rl:tst")
            counts = [await backend.incr_window("tst", 60) for _ in range(2)]
            await backend._redis.delete("test-rl:rl:tst")
            await backend.close()
            return counts

        counts = asyncio.run(main())
        if counts is None:
            pytest.skip("redis not running locally")
        assert counts == [1, 2]


class TestSandbox:
    def test_local_runner_echo(self, tmp_path: Path):
        async def main():
            r = LocalRunner()
            return await r.run("echo sandbox-ok", tmp_path, 10)

        result = asyncio.run(main())
        assert "sandbox-ok" in result.output
        assert result.exit_code == 0

    def test_local_runner_timeout(self, tmp_path: Path):
        async def main():
            r = LocalRunner()
            return await r.run("sleep 10", tmp_path, 1)

        result = asyncio.run(main())
        assert result.timed_out

    def test_docker_runner_requires_docker_cli(self, monkeypatch):
        import pi.tools.sandbox as sb

        monkeypatch.setattr(sb.shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="docker"):
            DockerRunner()

    def test_get_runner_default_local(self):
        assert isinstance(get_runner(""), LocalRunner)
        assert isinstance(get_runner("local"), LocalRunner)

    def test_bash_tool_uses_context_runner(self, tmp_path: Path):
        from pi.tools.base import ToolContext
        from pi.tools.bash import BashTool

        class SpyRunner:
            def __init__(self):
                self.commands = []

            async def run(self, command, cwd, timeout):
                self.commands.append(command)
                from pi.tools.sandbox import CommandResult

                return CommandResult(output="spy-output", exit_code=0)

        async def main():
            spy = SpyRunner()
            ctx = ToolContext(cwd=tmp_path, runner=spy)
            result = await BashTool().execute({"command": "ls -la"}, ctx)
            return spy, result

        spy, result = asyncio.run(main())
        assert spy.commands == ["ls -la"]
        assert "spy-output" in result.content
        assert "(exit code: 0)" in result.content
