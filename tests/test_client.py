"""SDK tests: pi.client against the real app via ASGITransport (no socket).

Lifespan events don't run under ASGITransport, so each test boots db.init()
inside the SAME event loop as the requests (cross-loop pooling breaks async
engines) - everything else is the real request path.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport

from conftest import TEST_DB_URL, TEST_REDIS_URL
from pi.client import PiClient, PiError
from pi.server.app import create_app
from pi.server.config import ServerSettings


@pytest.fixture()
def app(monkeypatch, tmp_path: Path):
    # 复用 conftest 的 TEST_DB_URL / TEST_REDIS_URL，别在这里再写一份字面量：
    # 之前两处各写各的端口，3306/6379 被别的服务占用时（与 CubeSandbox 同机
    # 部署就是这种情况）本文件会连到**错的库**——表现为 redis AuthenticationError，
    # 而 conftest 那侧却正常。单一口径 + 支持 PI_TEST_* 覆盖。
    monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PI_REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("PI_REDIS_NS", "sdk-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_TRAJECTORY_PATH", str(tmp_path / "traj.jsonl"))
    settings = ServerSettings.from_env()
    return create_app(settings)


def _run(app, coro_factory):
    """One event loop per test: boot (db.init) + full SDK flow."""

    async def wrapper():
        await app.state.db.init()
        async with PiClient("http://test", transport=ASGITransport(app=app)) as client:
            return await coro_factory(client)

    return asyncio.run(wrapper())


class TestSdk:
    def test_full_flow(self, app):
        async def main(pi: PiClient):
            await pi.register("alice", "password123")
            token = await pi.login("alice", "password123")
            assert token

            s = await pi.create_session("sdk demo")
            assert s["id"]
            assert [x["id"] for x in await pi.list_sessions()] == [s["id"]]

            result = await pi.ask(s["id"], "hello sdk")
            assert result["text"], "fake model replies with text"
            assert result["frames"][0]["event"] == "start"
            assert any(f["event"] == "text_delta" for f in result["frames"])
            assert result["usage"]["turns"] >= 1

            msgs = await pi.messages(s["id"])
            assert len(msgs) >= 2

            traj = await pi.latest_trajectory(s["id"])
            assert traj["run_id"]
            replay = await pi.run_trajectory(traj["run_id"])
            assert replay["run_id"] == traj["run_id"]

            u = await pi.usage()
            assert u["used_tokens"] >= 0

            await pi.delete_session(s["id"])
            assert await pi.list_sessions() == []

        _run(app, main)

    def test_errors_raise_with_detail(self, app):
        async def main(pi: PiClient):
            await pi.register("bob", "password123")
            await pi.login("bob", "password123")
            try:
                await pi.messages("deadbeef")
                raise AssertionError("should have raised")
            except PiError as e:
                assert e.status == 404
            try:
                await pi.login("bob", "wrong-password")
                raise AssertionError("should have raised")
            except PiError as e:
                assert e.status == 401

        _run(app, main)

    def test_cross_user_isolation(self, app):
        async def main(pi: PiClient):
            await pi.register("alice", "password123")
            await pi.register("mallory", "password123")
            await pi.login("alice", "password123")
            s = await pi.create_session("mine")
            await pi.ask(s["id"], "hi")
            traj = await pi.latest_trajectory(s["id"])
            await pi.login("mallory", "password123")
            try:
                await pi.run_trajectory(traj["run_id"])
                raise AssertionError("should have raised")
            except PiError as e:
                assert e.status == 404

        _run(app, main)
