"""Launch-readiness tests: logout/revocation, admin endpoints, log rotation."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi.security.audit import AuditLogger
from pi.server.app import create_app
from pi.server.config import ServerSettings
from pi.server.db import UserRepo


def _env(tmp_path: Path, monkeypatch, **extra) -> ServerSettings:
    monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'lr.db').as_posix()}")
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_RATE_LIMIT_RUNS_PER_MIN", "100")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return ServerSettings.from_env()


def _grant_admin(client: TestClient, username: str) -> None:
    """Promote the way an operator does: a direct write to the users table.

    Runs on the app's loop via the TestClient portal - the engine belongs to it.
    """
    repo = UserRepo(client.app.state.db)

    async def _promote() -> None:
        user = await repo.by_username(username)
        await repo.set_admin(user.id, True)

    client.portal.call(_promote)


def _setup(client: TestClient, username="alice", password="password123", admin=False):
    client.post("/v1/auth/register", json={"username": username, "password": password})
    if admin:
        _grant_admin(client, username)
    token = client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestLogout:
    def test_logout_revokes_token(self, tmp_path: Path, monkeypatch):
        with TestClient(create_app(_env(tmp_path, monkeypatch))) as client:
            h = _setup(client)
            assert client.get("/v1/me", headers=h).status_code == 200
            r = client.post("/v1/auth/logout", headers=h)
            assert r.status_code == 200 and r.json()["revoked"] is True
            # same token is now rejected everywhere
            assert client.get("/v1/me", headers=h).status_code == 401
            assert client.get("/v1/sessions", headers=h).status_code == 401
            # a fresh login still works
            h2 = _setup(client)
            assert client.get("/v1/me", headers=h2).status_code == 200


class TestAdminEndpoints:
    def test_non_admin_forbidden(self, tmp_path: Path, monkeypatch):
        with TestClient(create_app(_env(tmp_path, monkeypatch))) as client:
            hb = _setup(client, username="bob")
            assert client.get("/v1/admin/users", headers=hb).status_code == 403
            assert client.patch("/v1/admin/users/bob", json={"quota_tokens": 5}, headers=hb).status_code == 403

    def test_list_update_and_disable(self, tmp_path: Path, monkeypatch):
        with TestClient(create_app(_env(tmp_path, monkeypatch))) as client:
            admin = _setup(client, username="root", admin=True)
            hb = _setup(client, username="bob")

            # list
            r = client.get("/v1/admin/users", headers=admin)
            assert r.status_code == 200
            names = {u["username"] for u in r.json()["users"]}
            assert names == {"root", "bob"}

            # quota update
            r = client.patch("/v1/admin/users/bob", json={"quota_tokens": 12345}, headers=admin)
            assert r.json()["changed"] == ["quota_tokens"]
            users = {u["username"]: u for u in client.get("/v1/admin/users", headers=admin).json()["users"]}
            assert users["bob"]["quota_tokens"] == 12345

            # disable: bob's token dies immediately
            r = client.patch("/v1/admin/users/bob", json={"is_active": False}, headers=admin)
            assert "is_active" in r.json()["changed"]
            assert client.get("/v1/me", headers=hb).status_code == 401
            # disabled user cannot log back in
            r = client.post("/v1/auth/login", json={"username": "bob", "password": "password123"})
            assert r.status_code == 401

    def test_admin_cannot_disable_self(self, tmp_path: Path, monkeypatch):
        with TestClient(create_app(_env(tmp_path, monkeypatch))) as client:
            admin = _setup(client, username="root", admin=True)
            r = client.patch("/v1/admin/users/root", json={"is_active": False}, headers=admin)
            assert r.status_code == 400

    def test_revoke_kicks_tokens_but_account_stays(self, tmp_path: Path, monkeypatch):
        with TestClient(create_app(_env(tmp_path, monkeypatch))) as client:
            admin = _setup(client, username="root", admin=True)
            hb = _setup(client, username="bob")
            assert client.get("/v1/me", headers=hb).status_code == 200

            r = client.post("/v1/admin/users/bob/revoke", headers=admin)
            assert r.json()["revoked"] is True
            assert client.get("/v1/me", headers=hb).status_code == 401
            # account still alive: re-login works (after the 1s revocation window;
            # tokens issued in the same second as the revoke are conservatively killed)
            import time as _t
            _t.sleep(1.1)
            fresh = client.post(
                "/v1/auth/login", json={"username": "bob", "password": "password123"}
            ).json()["access_token"]
            assert client.get("/v1/me", headers={"Authorization": f"Bearer {fresh}"}).status_code == 200

    def test_audit_endpoint_filters(self, tmp_path: Path, monkeypatch):
        with TestClient(create_app(_env(tmp_path, monkeypatch))) as client:
            admin = _setup(client, username="root", admin=True)
            _setup(client, username="audited")
            # tool_call records carry "user", auth records carry "username" -
            # the user filter must match both (regression: it only looked at "user",
            # so register/login records were invisible to ?user=).
            log = AuditLogger(tmp_path / "audit.jsonl")
            log.tool_call(
                session_id="s1", user_id="audited", tool="bash", args={},
                decision_allowed=True, ok=True,
            )
            log.tool_call(
                session_id="s2", user_id="someone-else", tool="bash", args={},
                decision_allowed=True, ok=True,
            )

            r = client.get("/v1/admin/audit", params={"user": "audited"}, headers=admin)
            assert r.status_code == 200
            recs = r.json()["records"]
            assert recs, "user filter matched nothing"
            events = {rec["event"] for rec in recs}
            assert {"auth", "tool_call"} <= events
            assert all(rec.get("user", rec.get("username")) == "audited" for rec in recs)

            # the tool filter still works
            r = client.get("/v1/admin/audit", params={"tool": "bash"}, headers=admin)
            assert r.status_code == 200
            assert r.json()["records"], "tool filter matched nothing"
            assert all(rec.get("tool") == "bash" for rec in r.json()["records"])


class TestLogRotation:
    def test_audit_writes_daily_file(self, tmp_path: Path):
        log = AuditLogger(tmp_path / "audit.jsonl")
        log.tool_call(
            session_id="s", user_id="u", tool="bash", args={},
            decision_allowed=True, ok=True,
        )
        from datetime import datetime, timezone

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = tmp_path / f"audit-{day}.jsonl"
        assert daily.is_file()
        rec = json.loads(daily.read_text(encoding="utf-8").strip())
        assert rec["event"] == "tool_call"
        # the plain path is never written anymore
        assert not (tmp_path / "audit.jsonl").exists()
