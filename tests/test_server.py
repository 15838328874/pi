"""Multi-user server tests: auth, isolation, SSE runs (FakeProvider model)."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import TEST_DB_URL
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from pi.server.app import create_app
from pi.server.config import ServerSettings
from pi.llm.base import LLMProvider, StreamEnd, TextDelta, ToolCallDelta, Usage


class _BoomAfterToolProvider(LLMProvider):
    """First call emits one tool call (write), second call crashes: the completed
    round's messages must already be in the DB when the crash lands."""

    name = "fake"

    def __init__(self) -> None:
        self.model = "demo"
        self._calls = 0

    async def stream(self, system, messages, tools):
        self._calls += 1
        if self._calls == 1:
            yield ToolCallDelta(
                id="c1", name="write", arguments='{"path":"a.txt","content":"hi"}'
            )
            yield StreamEnd("tool_use", Usage(input_tokens=1, output_tokens=1))
        else:
            raise RuntimeError("boom")


class _BoomMidStreamProvider(LLMProvider):
    """Yields one text delta, then raises: crash right after write-ahead flushed."""

    name = "fake"

    def __init__(self) -> None:
        self.model = "demo"

    async def stream(self, system, messages, tools):
        yield TextDelta(text="hi")
        raise RuntimeError("boom")


class _BoomBeforeStreamProvider(LLMProvider):
    """Raises before yielding anything: the user message must still be persisted."""

    name = "fake"

    def __init__(self) -> None:
        self.model = "demo"

    async def stream(self, system, messages, tools):
        raise RuntimeError("boom")


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


def _register(client: TestClient, username: str, password: str):
    return client.post("/v1/auth/register", json={"username": username, "password": password})


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


class TestHealth:
    def test_healthz(self, server):
        assert server.get("/healthz").json() == {"status": "ok"}

    def test_readyz(self, server):
        assert server.get("/readyz").status_code == 200


class TestAuth:
    def test_first_registrant_is_not_admin(self, server):
        """Admin comes from a direct write to the users table, never from a route."""
        r = _register(server, "root", "password123")
        assert r.status_code == 200
        assert r.json()["is_admin"] is False
        token = _login(server, "root", "password123")
        r = server.get("/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_registration_is_open(self, server):
        for name in ("alice", "bob", "carol"):
            r = _register(server, name, "password123")
            assert r.status_code == 200, r.text
            assert r.json()["is_admin"] is False

    def test_duplicate_username(self, server):
        _register(server, "alice", "password123")
        assert _register(server, "alice", "password123").status_code == 409

    def test_concurrent_duplicate_registration_one_wins(self, server):
        """The pre-check misses a simultaneous signup; the UNIQUE constraint catches it."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: _register(server, "alice", "password123"), range(2))
            )
        assert sorted(r.status_code for r in results) == [200, 409]

    def test_login_wrong_password(self, server):
        _register(server, "admin", "password123")
        r = server.post("/v1/auth/login", json={"username": "admin", "password": "wrongpass"})
        assert r.status_code == 401

    def test_me_requires_token(self, server):
        assert server.get("/v1/me").status_code == 401
        _register(server, "admin", "password123")
        token = _login(server, "admin", "password123")
        r = server.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.json() == {"username": "admin"}

    def test_bad_token_rejected(self, server):
        r = server.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


class TestSessions:
    def test_create_and_list(self, server):
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        r = server.post("/v1/sessions", json={"title": "work", "model": "fake/demo"}, headers=h)
        assert r.status_code == 200
        sid = r.json()["id"]
        r = server.get("/v1/sessions", headers=h)
        assert len(r.json()["sessions"]) == 1
        assert r.json()["sessions"][0]["id"] == sid

    def test_isolation_user_cannot_see_others(self, server):
        _register(server, "alice", "password123")
        _register(server, "bob", "password123")
        alice = _login(server, "alice", "password123")
        bob = _login(server, "bob", "password123")
        r = server.post(
            "/v1/sessions", json={"title": "secret"}, headers={"Authorization": f"Bearer {alice}"}
        )
        sid = r.json()["id"]
        assert (
            server.get(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {bob}"}).status_code
            == 404
        )
        assert (
            server.get(
                f"/v1/sessions/{sid}/messages", headers={"Authorization": f"Bearer {bob}"}
            ).status_code
            == 404
        )
        assert server.get(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {alice}"}).status_code == 200

    def test_delete_session_removes_it_and_messages(self, server):
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"title": "temp"}, headers=h).json()["id"]
        with server.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h
        ) as resp:
            list(resp.iter_lines())
        assert server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]

        r = server.delete(f"/v1/sessions/{sid}", headers=h)
        assert r.status_code == 200
        assert r.json() == {"deleted": sid}
        assert server.get("/v1/sessions", headers=h).json()["sessions"] == []
        assert server.get(f"/v1/sessions/{sid}", headers=h).status_code == 404
        assert server.get(f"/v1/sessions/{sid}/messages", headers=h).status_code == 404

    def test_delete_cross_user_404_and_keeps_session(self, server):
        _register(server, "alice", "password123")
        _register(server, "bob", "password123")
        alice = _login(server, "alice", "password123")
        bob = _login(server, "bob", "password123")
        sid = server.post(
            "/v1/sessions", json={"title": "mine"}, headers={"Authorization": f"Bearer {alice}"}
        ).json()["id"]
        assert (
            server.delete(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {bob}"}).status_code
            == 404
        )
        assert server.get(f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {alice}"}).status_code == 200

    def test_delete_unknown_404(self, server):
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        r = server.delete("/v1/sessions/deadbeef", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404


class TestRuns:
    def test_run_streams_sse_and_persists(self, server):
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]

        events = []
        with server.stream("POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    events.append(line.removeprefix("event: "))

        assert events[0] == "start"
        assert "text_delta" in events
        assert events[-1] == "done"

        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert len(msgs) >= 2  # user + assistant
        assert msgs[0]["role"] == "user"

    def test_write_ahead_user_message_survives_mid_stream_crash(self, server, monkeypatch):
        """run-durability step 1: the user prompt must be in the DB even when the
        run crashes mid-stream (the core guarantee of write-ahead)."""
        import pi.server.runner as runner_mod

        monkeypatch.setattr(
            runner_mod,
            "resolve_chain",
            lambda model, on_fallback=None: _BoomMidStreamProvider(),
        )
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]

        events = []
        with server.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=h
        ) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    events.append(line.removeprefix("event: "))

        assert "error" in events
        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert [m["role"] for m in msgs] == ["user"]

    def test_write_ahead_survives_crash_before_any_output(self, server, monkeypatch):
        """Even when nothing ever streams, the user prompt is still persisted."""
        import pi.server.runner as runner_mod

        monkeypatch.setattr(
            runner_mod,
            "resolve_chain",
            lambda model, on_fallback=None: _BoomBeforeStreamProvider(),
        )
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]

        with server.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=h
        ) as resp:
            list(resp.iter_lines())

        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert [m["role"] for m in msgs] == ["user"]

    def test_idx_continuous_after_failed_run(self, server, monkeypatch):
        """A crashed run's write-ahead must not leave a gap or duplicate idx for
        the next run (idx 0 = crashed prompt, then 1, 2 for the healthy run)."""
        import asyncio

        import aiomysql
        import pi.server.runner as runner_mod
        from sqlalchemy.engine import make_url

        calls = {"n": 0}

        def factory(model, on_fallback=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _BoomMidStreamProvider()
            from pi.llm.fake import FakeProvider

            return FakeProvider(model="demo")

        monkeypatch.setattr(runner_mod, "resolve_chain", factory)
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]

        for prompt in ("boom me", "healthy"):
            with server.stream(
                "POST", f"/v1/sessions/{sid}/runs", json={"prompt": prompt}, headers=h
            ) as resp:
                list(resp.iter_lines())

        async def fetch_rows():
            u = make_url(TEST_DB_URL)  # 端口单一来源，随 .env.local 走
            conn = await aiomysql.connect(
                host=u.host, port=u.port, user=u.username, password=u.password, db=u.database
            )
            try:
                cur = await conn.cursor()
                await cur.execute(
                    "SELECT idx, role FROM messages WHERE session_id=%s ORDER BY idx", (sid,)
                )
                return await cur.fetchall()
            finally:
                conn.close()

        rows = asyncio.run(fetch_rows())
        assert [(r[0], r[1]) for r in rows] == [
            (0, "user"),   # crashed run: write-ahead prompt
            (1, "user"),   # healthy run: write-ahead prompt
            (2, "assistant"),
        ]

    def test_completed_round_messages_survive_crash(self, server, monkeypatch):
        """run-durability step 2: messages finalized at a tool round boundary are
        persisted before the round executes - a later crash loses at most the
        in-flight round, never the earlier conversation."""
        import pi.server.runner as runner_mod

        monkeypatch.setattr(
            runner_mod,
            "resolve_chain",
            lambda model, on_fallback=None: _BoomAfterToolProvider(),
        )
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]

        events = []
        with server.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "write it"}, headers=h
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    events.append(line.removeprefix("event: "))

        assert "toolcall_start" in events
        assert "error" in events
        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        # prompt / assistant tool-call / tool result - the completed round survives
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        blocks = msgs[2]["blocks"]
        assert blocks and blocks[0]["type"] == "tool_result"

    def test_run_unknown_session_404(self, server):
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        r = server.post(
            "/v1/sessions/deadbeef/runs", json={"prompt": "hi"}, headers=h
        )
        assert r.status_code == 404

    def test_run_requires_auth(self, server):
        r = server.post("/v1/sessions/x/runs", json={"prompt": "hi"})
        assert r.status_code == 401

    def test_multi_turn_remembers_history(self, server):
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
        for _ in range(2):
            with server.stream(
                "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=h
            ) as resp:
                list(resp.iter_lines())
        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert len(msgs) == 4  # 2x (user + assistant)
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_messages_blocks_contract_is_an_array(self, server):
        """The API contract: "blocks" is the block ARRAY, not the stored Message
        object ({"role":..,"blocks":[..]}). The chat UI crashed on the object
        shape after every run - live text flashed then vanished on re-render."""
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
        with server.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=h
        ) as resp:
            list(resp.iter_lines())
        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert msgs, "run should persist messages"
        for m in msgs:
            assert isinstance(m["blocks"], list), m
            for b in m["blocks"]:
                assert isinstance(b, dict) and "type" in b, b


class TestRateLimit:
    def test_rate_limit_kicks_in(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_RATE_LIMIT_RUNS_PER_MIN", "2")
        settings = ServerSettings.from_env()
        app = create_app(settings)
        with TestClient(app) as client:
            _register(client, "alice", "password123")
            token = _login(client, "alice", "password123")
            h = {"Authorization": f"Bearer {token}"}
            sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
            statuses = []
            for _ in range(3):
                r = client.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h)
                statuses.append(r.status_code)
            assert statuses[:2] == [200, 200]
            assert statuses[2] == 429


def _auth_audit(tmp_path: Path) -> list[dict]:
    """Auth records from today's rotated file (audit.jsonl -> audit-<day>.jsonl)."""
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / f"audit-{day}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("event") == "auth":
            out.append(rec)
    return out


class TestAuthAudit:
    """Register and login are unauthenticated, so the audit log is the only
    evidence of who opened an account or tried to get into one."""

    def test_every_outcome_is_recorded(self, server, tmp_path: Path):
        assert _register(server, "audited", "password123").status_code == 200
        assert _register(server, "audited", "password123").status_code == 409
        _login(server, "audited", "password123")
        bad = server.post("/v1/auth/login",
                          json={"username": "audited", "password": "wrong-password"})
        assert bad.status_code == 401

        recs = _auth_audit(tmp_path)
        assert [(r["action"], r["ok"], r["reason"]) for r in recs] == [
            ("register", True, ""),
            ("register", False, "duplicate"),
            ("login", True, ""),
            ("login", False, "invalid_credentials"),
        ]
        assert all(r["ip"] for r in recs), "client ip must be populated"
        assert all("password" not in json.dumps(r) for r in recs), "never log the secret"

    def test_invalid_username_is_recorded(self, server, tmp_path: Path):
        r = server.post("/v1/auth/register", json={"username": "a b!", "password": "password123"})
        assert r.status_code == 400
        recs = _auth_audit(tmp_path)
        assert recs[-1]["ok"] is False
        assert recs[-1]["reason"] == "invalid_username"


PROXY_IP = "172.18.0.2"  # inside 172.16/12 (Docker bridge), outside 127.0.0.1
REAL_IP = "203.0.113.9"


class TestForwardedFor:
    """PI_FORWARDED_ALLOW_IPS decides whether request.client.host is the real
    client or the reverse proxy. Caddy runs in its own compose container, so
    uvicorn's 127.0.0.1 default silently drops X-Forwarded-For and every user
    gets logged as Caddy - which would also collapse any IP rate limit into a
    single bucket shared by the whole world."""

    def _register_ip(self, tmp_path: Path, monkeypatch, trusted: str, xff: str) -> str:
        monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_POLICY", "")
        monkeypatch.setenv("PI_FORWARDED_ALLOW_IPS", trusted)
        settings = ServerSettings.from_env()
        app = ProxyHeadersMiddleware(
            create_app(settings), trusted_hosts=settings.forwarded_allow_ips
        )
        with TestClient(app, client=(PROXY_IP, 40000)) as client:
            r = client.post("/v1/auth/register",
                            json={"username": "xffuser", "password": "password123"},
                            headers={"X-Forwarded-For": xff})
            assert r.status_code == 200, r.text
        return _auth_audit(tmp_path)[-1]["ip"]

    def test_trusted_proxy_cidr_yields_real_client_ip(self, tmp_path: Path, monkeypatch):
        assert self._register_ip(tmp_path, monkeypatch, "172.16.0.0/12", REAL_IP) == REAL_IP

    def test_default_trust_logs_the_proxy_not_the_client(self, tmp_path: Path, monkeypatch):
        """The bug this setting exists to fix, pinned as a regression test."""
        assert self._register_ip(tmp_path, monkeypatch, "127.0.0.1", REAL_IP) == PROXY_IP

    def test_spoofed_prefix_loses_when_proxy_is_trusted(self, tmp_path: Path, monkeypatch):
        """Each proxy appends, so a hop the client prepended itself must be ignored."""
        got = self._register_ip(tmp_path, monkeypatch, "172.16.0.0/12", f"1.2.3.4, {REAL_IP}")
        assert got == REAL_IP

    def test_wildcard_trust_lets_any_client_spoof(self, tmp_path: Path, monkeypatch):
        """Why the docs forbid PI_FORWARDED_ALLOW_IPS="*": uvicorn then returns
        the leftmost, i.e. attacker-supplied, entry."""
        got = self._register_ip(tmp_path, monkeypatch, "*", f"1.2.3.4, {REAL_IP}")
        assert got == "1.2.3.4"

    def test_rag_ingest_oversize_rejected(self, server):
        """Upload beyond the RAG cap is rejected before any ingest work starts."""
        server.app.state.settings.rag_max_upload_bytes = 100
        _register(server, "alice", "password123")
        token = _login(server, "alice", "password123")
        h = {"Authorization": f"Bearer {token}"}
        r = server.post(
            "/v1/rag/ingest",
            files={"file": ("big.txt", b"x" * 200, "text/plain")},
            headers=h,
        )
        assert r.status_code == 413
        docs = server.get("/v1/rag/docs", headers=h).json()["docs"]
        assert docs == []
