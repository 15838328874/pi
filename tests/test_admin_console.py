"""Admin console endpoints: /v1/admin/stats, /v1/admin/usage, audit day param.

Admin is granted by direct DB write (no route, by design); the tests use
TestClient.portal to run the grant inside the app's own event loop.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from conftest import TEST_DB_URL
from fastapi.testclient import TestClient

from pi.server.app import create_app
from pi.server.config import ServerSettings
from pi.server.db import UserRepo


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_POLICY", "")
    settings = ServerSettings.from_env()
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def _register(client: TestClient, username: str) -> None:
    assert client.post("/v1/auth/register", json={"username": username, "password": "password123"}).status_code == 200


def _login(client: TestClient, username: str) -> dict:
    token = client.post("/v1/auth/login", json={"username": username, "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _grant_admin(client: TestClient, username: str) -> None:
    async def grant():
        repo = UserRepo(client.app.state.db)
        user = await repo.by_username(username)
        await repo.set_admin(user.id, True)

    client.portal.call(grant)


def _run_turn(client: TestClient, h: dict) -> None:
    sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
    with client.stream("POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h) as resp:
        assert resp.status_code == 200
        list(resp.iter_lines())


class TestAdminGate:
    def test_stats_requires_admin(self, server):
        _register(server, "alice")
        h = _login(server, "alice")
        for path in ("/v1/admin/stats", "/v1/admin/usage"):
            assert server.get(path, headers=h).status_code == 403

    def test_audit_requires_admin(self, server):
        _register(server, "alice")
        h = _login(server, "alice")
        assert server.get("/v1/admin/audit", headers=h).status_code == 403


class TestAdminStats:
    def test_stats_aggregates(self, server):
        _register(server, "root")
        _register(server, "alice")
        _grant_admin(server, "root")
        h = _login(server, "root")
        _run_turn(server, _login(server, "alice"))

        r = server.get("/v1/admin/stats", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["today"]["runs"] >= 1
        assert body["today"]["input_tokens"] >= 1
        assert body["users"] >= 2
        assert body["sessions"] >= 1


class TestAdminUsage:
    def test_usage_per_user(self, server):
        _register(server, "root")
        _register(server, "alice")
        _register(server, "bob")  # no runs -> absent from the aggregates
        _grant_admin(server, "root")
        _run_turn(server, _login(server, "alice"))

        r = server.get("/v1/admin/usage", headers=_login(server, "root"))
        assert r.status_code == 200
        by_name = {u["username"]: u for u in r.json()["users"]}
        assert "alice" in by_name
        assert by_name["alice"]["runs"] >= 1
        assert by_name["alice"]["input_tokens"] >= 1
        assert "bob" not in by_name


class TestAdminAudit:
    def test_audit_returns_auth_records_and_filters(self, server):
        _register(server, "root")
        _register(server, "alice")
        _grant_admin(server, "root")
        h = _login(server, "root")

        r = server.get("/v1/admin/audit", headers=h)
        assert r.status_code == 200
        assert any(rec.get("event") == "auth" for rec in r.json()["records"])

        r = server.get("/v1/admin/audit?user=alice", headers=h)
        records = r.json()["records"]
        assert records, "alice registered/logged in -> auth records exist"
        assert all(
            rec.get("user") == "alice" or rec.get("username") == "alice" for rec in records
        )

    def test_audit_day_param(self, server):
        _register(server, "root")
        _grant_admin(server, "root")
        h = _login(server, "root")

        assert server.get("/v1/admin/audit?day=2026/09/27", headers=h).status_code == 400
        assert server.get("/v1/admin/audit?day=../..", headers=h).status_code == 400
        # a valid day with no file is an empty list, not an error
        r = server.get("/v1/admin/audit?day=2020-01-01", headers=h)
        assert r.status_code == 200
        assert r.json() == {"records": []}

    def test_admin_page_served(self, server):
        r = server.get("/ui/admin.html")
        assert r.status_code == 200
        assert "管理控制台" in r.text
