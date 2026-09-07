"""Multi-user server tests: auth, isolation, SSE runs (FakeProvider model).

Also pins the HTTP and SSE contract that web/ generates its TypeScript from:
every response body is typed in the OpenAPI document, and the Sse*Data models
still describe what event_to_sse() actually emits.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from conftest import StrictFakeProvider

from pi.agent.events import (
    CompactionEvent,
    ErrorEvent,
    PlanEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from pi.agent.loop import PREVIEW_LEN
from pi.llm.fake import FakeProvider
from pi.memory import FactCandidate, HashEmbedder, InMemoryStore, NoOpStore
from pi.memory.repo import MemoryRowIn
from pi.memory.service import MEMORY_HEADER
from pi.models import Message, Plan, TextBlock, ToolCallBlock, Usage
from pi.server.app import SSE_DATA_MODELS, create_app
from pi.server.config import ServerSettings
from pi.server.db import AuditEventRepo, UserMemoryRepo, UserRepo
from pi.server.runner import event_to_sse


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'srv.db').as_posix()}")
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_POLICY", "")
    monkeypatch.setenv("PI_PUBLIC_BASE_URL", "http://files.test")
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
        r = server.get("/readyz")
        assert r.status_code == 200
        assert r.json()["checks"]["db"] == "ok"

    def test_a_disabled_memory_does_not_make_the_service_unready(self, server):
        """No vector database configured is a normal install, not an outage.

        Gating readiness on memory would pull the whole service out of rotation
        over a feature most requests never touch.
        """
        body = server.get("/readyz").json()
        assert body["status"] == "ready"
        assert body["checks"]["memory"] == "disabled"


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
        assert r.json() == {"username": "admin", "is_admin": False}

    def test_login_reports_the_account_the_token_is_for(self, server):
        """The browser shows this without a second /v1/me round trip, so it has to
        agree with the identity the token actually carries."""
        _register(server, "carol", "password123")
        r = server.post("/v1/auth/login", json={"username": "carol", "password": "password123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["username"] == "carol"
        # is_admin rides along so the client can show/hide the admin console
        # right after login, without a second round trip or a second source.
        assert body["is_admin"] is False
        me = server.get("/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.json() == {"username": body["username"], "is_admin": False}

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


def _tc(id: str, name: str, args: dict) -> ToolCallBlock:
    return ToolCallBlock(id=id, name=name, arguments=json.dumps(args, ensure_ascii=False))


def _frames(resp) -> list[tuple[str, dict]]:
    """SSE response -> [(event name, parsed data)], in arrival order."""
    out: list[tuple[str, dict]] = []
    name = ""
    for line in resp.iter_lines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            out.append((name, json.loads(line.removeprefix("data: "))))
    return out


def _scripted(monkeypatch, scripts: list[list[list]]) -> None:
    """Serve one scripted FakeProvider per run, falling back to a strict unscripted one.

    The server fixture pins PI_MODEL=fake/demo, and the registry builds a fresh
    provider per run, so patching resolve_chain is the only way to drive a scripted
    multi-run scenario through the HTTP stack. Once the scripts run out the fallback
    is StrictFakeProvider rather than FakeProvider: the run after a plan run then
    validates the history that came back out of the database, which is where an
    unpaired batch would turn into a 400 from a real model.
    """

    def fake_resolve(model: str, **kwargs):
        return StrictFakeProvider(responses=scripts.pop(0)) if scripts else StrictFakeProvider()

    monkeypatch.setattr("pi.server.runner.resolve_chain", fake_resolve)


def _session(server, username: str = "alice") -> tuple[dict, str]:
    """Register, log in, create a session. Returns (auth headers, session id)."""
    _register(server, username, "password123")
    token = _login(server, username, "password123")
    h = {"Authorization": f"Bearer {token}"}
    sid = server.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
    return h, sid


class TestPlanRun:
    """submit_plan through the real server path: wire order, persistence, pairing."""

    PLAN = {"title": "重构 sessions 表", "steps": ["加可空 plan 列", "写迁移 0003"]}

    def _run(self, server, sid: str, h: dict, prompt: str = "重构一下") -> list[tuple[str, dict]]:
        with server.stream(
            "POST", f"/v1/sessions/{sid}/runs", json={"prompt": prompt}, headers=h
        ) as resp:
            assert resp.status_code == 200, resp.read().decode()
            return _frames(resp)

    def test_plan_event_streams_and_the_plan_persists(self, server, monkeypatch):
        _scripted(monkeypatch, [[[TextBlock(text="先规划。"), _tc("c1", "submit_plan", self.PLAN)]]])
        h, sid = _session(server)
        frames = self._run(server, sid, h)

        names = [n for n, _ in frames]
        assert names[0] == "start"
        assert names[-1] == "done"
        assert "error" not in names

        plan_frames = [d for n, d in frames if n == "plan"]
        assert len(plan_frames) == 1, "at most one plan event per run"
        assert plan_frames[0] == self.PLAN
        # after the last toolcall_end, before turn_end - SSE_DOC promises this order
        last_end = max(i for i, n in enumerate(names) if n == "toolcall_end")
        assert last_end < names.index("plan") < names.index("turn_end")

        # Both endpoints carry it, which is what makes the card survive a reload.
        assert server.get(f"/v1/sessions/{sid}", headers=h).json()["plan"] == self.PLAN
        listed = server.get("/v1/sessions", headers=h).json()["sessions"]
        assert [s for s in listed if s["id"] == sid][0]["plan"] == self.PLAN

    def test_mid_batch_calls_are_skipped_on_the_wire_and_in_history(
        self, server, monkeypatch, tmp_path: Path
    ):
        _scripted(
            monkeypatch,
            [[
                [
                    _tc("c1", "submit_plan", self.PLAN),
                    _tc("c2", "write", {"path": "leak.txt", "content": "should not exist"}),
                ]
            ]],
        )
        h, sid = _session(server)
        frames = self._run(server, sid, h)

        ends = {d["id"]: d for n, d in frames if n == "toolcall_end"}
        assert ends["c1"]["ok"] is True
        assert ends["c2"]["ok"] is False, "a skipped call must not look like it succeeded"
        assert "skipped" in ends["c2"]["result"]
        assert "did not execute" in ends["c2"]["result"]
        assert not (tmp_path / "ws" / "alice" / "leak.txt").exists()

        msgs = server.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assistant = [m for m in msgs if m["role"] == "assistant"][-1]
        call_ids = [b["id"] for b in assistant["blocks"] if b["type"] == "tool_call"]
        assert call_ids == ["c1", "c2"]
        following = msgs[msgs.index(assistant) + 1]
        assert following["role"] == "user"
        answered = [b["tool_use_id"] for b in following["blocks"] if b["type"] == "tool_result"]
        assert answered == call_ids, "every call in the batch needs a result, in order"

    def test_history_after_a_plan_run_feeds_the_next_run(self, server, monkeypatch):
        """The pairing invariant through the server, where it actually bites.

        A batch left unpaired is fine in the run that produced it; it fails when the
        history is reloaded and sent to a provider that validates it - i.e. on the
        next run, as a 400 from a real model.
        """
        _scripted(
            monkeypatch,
            [[
                [
                    _tc("c1", "submit_plan", self.PLAN),
                    _tc("c2", "bash", {"command": "echo ran"}),
                ]
            ]],
        )
        h, sid = _session(server)
        first = self._run(server, sid, h, "重构一下")
        assert [d for n, d in first if n == "plan"]

        # scripts is now empty, so this run gets an unscripted provider and a real
        # second trip through history loading.
        second = self._run(server, sid, h, "继续执行计划")
        names = [n for n, _ in second]
        assert "error" not in names, [d for n, d in second if n == "error"]
        assert "text_delta" in names
        assert names[-1] == "done"

    def test_a_session_without_a_plan_reports_null(self, server):
        h, sid = _session(server)
        assert server.get(f"/v1/sessions/{sid}", headers=h).json()["plan"] is None
        listed = server.get("/v1/sessions", headers=h).json()["sessions"]
        assert listed[0]["plan"] is None


class TestRateLimit:
    def test_rate_limit_kicks_in(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'rl.db').as_posix()}")
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


@pytest.fixture()
def memory_server(tmp_path: Path, monkeypatch):
    """A server with memory enabled against the in-process store.

    get_store/get_embedder are patched at the app module because the real ones build
    a MilvusStore from PI_MILVUS_URI - which conftest pins empty precisely so the
    suite can never reach a live vector database.
    """
    store = InMemoryStore()
    monkeypatch.setattr("pi.server.app.get_store", lambda *a, **k: store)
    monkeypatch.setattr("pi.server.app.get_embedder", lambda *a, **k: HashEmbedder(dim=64))
    monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'mem.db').as_posix()}")
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_POLICY", "")
    monkeypatch.setenv("PI_MILVUS_URI", "memory://in-process")
    monkeypatch.setenv("PI_EMBEDDING_MODEL", "hash/64")
    # The 400-char default exists to skip "yes"/"done" turns; a scripted run is much
    # shorter, so without this the background extraction would never fire. The
    # threshold itself is covered by test_memory.py.
    monkeypatch.setenv("PI_MEMORY_EXTRACT_MIN_CHARS", "10")
    with TestClient(create_app(ServerSettings.from_env())) as client:
        yield client, store


def _auth(client: TestClient, username: str) -> dict:
    _register(client, username, "password123")
    return {"Authorization": f"Bearer {_login(client, username, 'password123')}"}


def _uid(client: TestClient, username: str) -> int:
    """The account's numeric id - the tenant key the memory store is scoped by."""
    repo = UserRepo(client.app.state.db)

    async def _lookup() -> int:
        user = await repo.by_username(username)
        return user.id

    return client.portal.call(_lookup)


def _seed(client: TestClient, store: InMemoryStore, username: str, text: str) -> int:
    """Store a fact under `username`'s real id and return the fact id.

    The id has to come from the users table: it is the tenant key, and a test that
    guessed it would not be testing the boundary at all. The lookup happens inside
    the same coroutine - _uid() blocks on the portal, and calling it from here would
    block the loop this is already running on.

    Seeding writes the repo first and mirrors to the index, which is the write path
    the service itself uses: MySQL is the truth, and retrieval joins index hits back
    through the repo, so seeding only the store would inject nothing and only the
    repo would test a degraded index instead of the real path.
    """
    users = UserRepo(client.app.state.db)
    memories = UserMemoryRepo(client.app.state.db)

    async def _insert() -> int:
        user = await users.by_username(username)
        vec, _ = await HashEmbedder(dim=64).embed([text])
        (fid,) = await memories.insert_many(
            user.id,
            [MemoryRowIn(text=text, kind="preference", source_session="seed", embedding=list(vec[0]))],
        )
        await memories.mark_synced([fid])
        await store.upsert(user.id, [(fid, vec[0])])
        return fid

    return client.portal.call(_insert)


class TestMemoryWiring:
    """The app.py -> MemoryService plumbing, which nothing else in the suite pins.

    Every value here has a working default, so deleting one of these keyword
    arguments would not fail a single test - it would silently turn a configured
    feature off in production. Capturing the constructor call is the only way to see
    it: MemoryService is a closure local in create_app, reachable from no route and
    no piece of app state.
    """

    def test_the_retrieval_settings_reach_the_service(self, tmp_path: Path, monkeypatch):
        import pi.server.app as app_module

        captured: dict[str, object] = {}
        real = app_module.MemoryService
        sentinel = object()

        def recorder(**kwargs: object):
            captured.update(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(app_module, "MemoryService", recorder)
        monkeypatch.setattr(app_module, "get_store", lambda *a, **k: InMemoryStore())
        monkeypatch.setattr(app_module, "get_embedder", lambda *a, **k: HashEmbedder(dim=64))
        # conftest pins PI_RERANK_URL empty so the suite can never bill a live call;
        # patched here as well, so the test asserts the wiring and not the endpoint.
        monkeypatch.setattr(app_module, "get_reranker", lambda *a, **k: sentinel)
        monkeypatch.setenv(
            "PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'wire.db').as_posix()}"
        )
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_POLICY", "")
        monkeypatch.setenv("PI_MILVUS_URI", "memory://in-process")
        monkeypatch.setenv("PI_EMBEDDING_MODEL", "hash/64")
        monkeypatch.setenv("PI_MEMORY_TOP_K", "3")
        monkeypatch.setenv("PI_MEMORY_RECALL_K", "17")
        monkeypatch.setenv("PI_MEMORY_MIN_SIMILARITY", "0.4")
        monkeypatch.setenv("PI_MEMORY_RERANK_MIN_SCORE", "0.55")

        create_app(ServerSettings.from_env())  # no lifespan, so nothing is set up

        assert captured["reranker"] is sentinel
        assert captured["top_k"] == 3
        assert captured["recall_k"] == 17
        assert captured["min_similarity"] == 0.4
        assert captured["rerank_min_score"] == 0.55

    def test_the_arbiter_settings_reach_the_service(self, tmp_path: Path, monkeypatch):
        import pi.server.app as app_module

        captured: dict[str, object] = {}
        real = app_module.MemoryService

        def recorder(**kwargs: object):
            captured.update(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(app_module, "MemoryService", recorder)
        monkeypatch.setattr(app_module, "get_store", lambda *a, **k: InMemoryStore())
        monkeypatch.setattr(app_module, "get_embedder", lambda *a, **k: HashEmbedder(dim=64))
        monkeypatch.setattr(app_module, "get_reranker", lambda *a, **k: None)
        monkeypatch.setenv(
            "PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'arb.db').as_posix()}"
        )
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_MEMORY_ARBITER_MODEL", "fake/plus")
        monkeypatch.setenv("PI_MEMORY_ARBITER_INTERVAL_SECONDS", "123")
        monkeypatch.setenv("PI_MEMORY_ARBITER_BATCH", "7")

        create_app(ServerSettings.from_env())  # no lifespan, so nothing is set up

        assert captured["arbiter_model"] == "fake/plus"


class TestArbiterSweep:
    """The periodic sweep: dirty-user selection, checkpoint and lock.

    Tested against _memory_arbiter_sweep directly - the loop around it is three
    lines of sleep/try, and driving a real interval from a test would only make
    the suite slow.
    """

    def _db_with_usage(self, tmp_path: Path, rows: list[tuple[int, str, str, int, str]]):
        from pi.server.db import Database, UsageRecord
        from sqlalchemy.ext.asyncio import AsyncSession

        db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'sweep.db').as_posix()}")

        async def seed():
            async with AsyncSession(db.engine) as s:
                for user_id, username, model, turns, created_at in rows:
                    s.add(UsageRecord(user_id=user_id, username=username, model=model,
                                      session_id="s1", turns=turns, created_at=created_at))
                await s.commit()

        asyncio.run(db.init())
        asyncio.run(seed())
        return db

    def test_the_sweep_arbitrates_extraction_users_and_advances_the_checkpoint(
        self, tmp_path: Path
    ):
        from pi.server.app import _memory_arbiter_sweep
        from pi.server.cache import MemoryBackend

        now = datetime.now(timezone.utc)
        iso = lambda dt: dt.isoformat(timespec="seconds")
        # Seeded 2s back: the checkpoint boundary is inclusive (never-miss beats
        # never-duplicate), so a row written in the sweep's own second would be
        # re-swept on the next tick by design. Real extractions always precede
        # the sweep that consumes them; this reproduces that ordering.
        db = self._db_with_usage(tmp_path, [
            (1, "alice", "fake/mem", 0, iso(now - timedelta(seconds=2))),  # extraction: dirty
            (2, "bob", "fake/plus", 0, iso(now - timedelta(seconds=2))),   # arbiter row: excluded
            (3, "carol", "fake/mem", 1, iso(now - timedelta(seconds=2))),  # a run row: excluded
            (4, "dave", "fake/mem", 0, iso(now - timedelta(days=3))),      # before default window
        ])
        arbitrated: list[int] = []

        class _Mem:
            async def arbitrate(self, user_id: int, username: str):
                arbitrated.append(user_id)

        settings = ServerSettings(
            database_url=str(tmp_path / "unused"),
            memory_model="fake/mem",
            memory_arbiter_batch=10,
        )
        cache = MemoryBackend()

        async def main():
            first = await _memory_arbiter_sweep(_Mem(), cache, db.engine, settings)
            second = await _memory_arbiter_sweep(_Mem(), cache, db.engine, settings)
            await db.dispose()
            return first, second

        first, second = asyncio.run(main())
        assert first == 1 and arbitrated == [1]
        assert second == 0, "the checkpoint must hide rows the first sweep consumed"

    def test_the_sweep_is_a_noop_without_the_lock(self, tmp_path: Path):
        from pi.server.app import _ARBITER_LOCK, _memory_arbiter_sweep
        from pi.server.cache import MemoryBackend

        db = self._db_with_usage(tmp_path, [(1, "alice", "fake/mem", 0,
                                             datetime.now(timezone.utc).isoformat(timespec="seconds"))])
        arbitrated: list[int] = []

        class _Mem:
            async def arbitrate(self, user_id: int, username: str):
                arbitrated.append(user_id)

        settings = ServerSettings(database_url=str(tmp_path / "unused"), memory_model="fake/mem")
        cache = MemoryBackend()

        async def main():
            assert await cache.acquire_lock(_ARBITER_LOCK, ttl_seconds=60), "precondition"
            swept = await _memory_arbiter_sweep(_Mem(), cache, db.engine, settings)
            await db.dispose()
            return swept

        assert asyncio.run(main()) == 0
        assert arbitrated == []


class TestMaintenanceSweep:
    """The maintenance half of the loop: index healing, sync retry, decay.

    Tested against _memory_maintenance_sweep directly for the same reason as the
    arbiter tests above - the loop around it is sleep/try, and the sweep's own
    contract (order, lock, idempotence) is what matters.
    """

    class _Mem:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self._queue: list[int] = [2]  # pending rows, drained by the first pass

        async def ensure_index(self) -> None:
            self.calls.append("ensure_index")

        async def sync_pending(self) -> int:
            self.calls.append("sync_pending")
            return self._queue.pop(0) if self._queue else 0

        async def decay(self, days: int) -> int:
            self.calls.append(f"decay:{days}")
            return 1 if len(self.calls) <= 3 else 0

    def test_one_tick_heals_syncs_and_decays_in_order(self, tmp_path: Path):
        from pi.server.app import _memory_maintenance_sweep
        from pi.server.cache import MemoryBackend

        mem = self._Mem()
        settings = ServerSettings(
            database_url=str(tmp_path / "unused"), memory_decay_days=42
        )
        cache = MemoryBackend()

        async def main():
            first = await _memory_maintenance_sweep(mem, cache, settings)
            # The finally-release is load-bearing: without it the tick's own
            # lock would freeze every later tick until the TTL ran out.
            second = await _memory_maintenance_sweep(mem, cache, settings)
            return first, second

        first, second = asyncio.run(main())
        assert mem.calls == ["ensure_index", "sync_pending", "decay:42"] * 2
        assert first == {"synced": 2, "decayed": 1}
        assert second == {"synced": 0, "decayed": 0}, "a second pass finds nothing to do"

    def test_the_sweep_is_a_noop_without_the_lock(self, tmp_path: Path):
        from pi.server.app import _MAINTENANCE_LOCK, _memory_maintenance_sweep
        from pi.server.cache import MemoryBackend

        mem = self._Mem()
        settings = ServerSettings(database_url=str(tmp_path / "unused"))
        cache = MemoryBackend()

        async def main():
            assert await cache.acquire_lock(_MAINTENANCE_LOCK, ttl_seconds=60)
            return await _memory_maintenance_sweep(mem, cache, settings)

        assert asyncio.run(main()) == {}
        assert mem.calls == []

    def test_the_loop_starts_only_when_memory_is_enabled(self, tmp_path: Path, monkeypatch):
        """Maintenance no longer needs the arbiter model: pending-sync retries and
        decay must run in every memory-enabled deployment, including degraded
        mode. Arbitration stays conditional inside the loop."""
        import pi.server.app as app_module

        started: list[str] = []

        async def fake_loop(memory, cache, engine, settings):
            started.append(settings.memory_decay_days and "on" or "off")
            await asyncio.sleep(3600)  # cancelled by lifespan shutdown

        monkeypatch.setattr(app_module, "_memory_maintenance_loop", fake_loop)
        monkeypatch.setattr(
            app_module, "get_store", lambda uri, **k: InMemoryStore() if uri else NoOpStore()
        )
        monkeypatch.setattr(
            app_module,
            "get_embedder",
            lambda model, **k: HashEmbedder(dim=64) if model else None,
        )
        monkeypatch.setattr(app_module, "get_reranker", lambda *a, **k: None)
        monkeypatch.setenv(
            "PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'loop.db').as_posix()}"
        )
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_MILVUS_URI", "memory://in-process")
        monkeypatch.setenv("PI_EMBEDDING_MODEL", "hash/64")
        monkeypatch.setenv("PI_MEMORY_ARBITER_MODEL", "")  # the old start condition

        async def boot(with_memory: bool) -> None:
            if with_memory:
                monkeypatch.setenv("PI_MILVUS_URI", "memory://in-process")
                monkeypatch.setenv("PI_EMBEDDING_MODEL", "hash/64")
            else:
                monkeypatch.delenv("PI_MILVUS_URI")
                monkeypatch.delenv("PI_EMBEDDING_MODEL")
            app = create_app(ServerSettings.from_env())
            async with app.router.lifespan_context(app):
                # Give the freshly created task one scheduler turn: without an
                # await in the body, shutdown's cancel() would land before the
                # coroutine ever started.
                await asyncio.sleep(0)

        asyncio.run(boot(with_memory=True))
        assert started == ["on"]
        asyncio.run(boot(with_memory=False))
        assert started == ["on"], "memory off means no maintenance task"


class TestMemories:
    def test_all_three_routes_require_a_token(self, memory_server):
        client, _ = memory_server
        assert client.get("/v1/memories").status_code == 401
        assert client.delete("/v1/memories").status_code == 401
        assert client.delete("/v1/memories/1").status_code == 401

    def test_list_returns_the_callers_facts_and_the_store_status(self, memory_server):
        client, store = memory_server
        h = _auth(client, "alice")
        _seed(client, store, "alice", "用户偏好 uv")

        body = client.get("/v1/memories", headers=h).json()
        assert body["status"] == "ready"
        assert [f["text"] for f in body["facts"]] == ["用户偏好 uv"]
        fact = body["facts"][0]
        assert fact["kind"] == "preference" and fact["source_session"] == "seed"
        assert fact["created_at"] and isinstance(fact["id"], int)

    def test_one_user_never_sees_another_users_facts(self, memory_server):
        client, store = memory_server
        ha, hb = _auth(client, "alice"), _auth(client, "bob")
        _seed(client, store, "alice", "alice 的事实")
        _seed(client, store, "bob", "bob 的事实")

        assert [f["text"] for f in client.get("/v1/memories", headers=ha).json()["facts"]] == [
            "alice 的事实"
        ]
        assert [f["text"] for f in client.get("/v1/memories", headers=hb).json()["facts"]] == [
            "bob 的事实"
        ]

    def test_delete_removes_one_fact(self, memory_server):
        client, store = memory_server
        h = _auth(client, "alice")
        fid = _seed(client, store, "alice", "用户偏好 uv")

        assert client.delete(f"/v1/memories/{fid}", headers=h).json() == {"deleted": True}
        assert client.get("/v1/memories", headers=h).json()["facts"] == []
        # and it stays gone
        assert client.delete(f"/v1/memories/{fid}", headers=h).status_code == 404

    def test_delete_cannot_reach_another_users_fact(self, memory_server):
        """The path id is not authority on its own.

        The 404 is also identical to the one for a never-existent id, so it leaks
        nothing about which ids belong to whom.
        """
        client, store = memory_server
        ha, hb = _auth(client, "alice"), _auth(client, "bob")
        fid = _seed(client, store, "alice", "alice 的事实")

        assert client.delete(f"/v1/memories/{fid}", headers=hb).status_code == 404
        assert [f["text"] for f in client.get("/v1/memories", headers=ha).json()["facts"]] == [
            "alice 的事实"
        ]

    def test_clear_reports_how_much_went_away(self, memory_server):
        client, store = memory_server
        ha, hb = _auth(client, "alice"), _auth(client, "bob")
        _seed(client, store, "alice", "第一条")
        _seed(client, store, "alice", "第二条")
        _seed(client, store, "bob", "bob 的")

        assert client.delete("/v1/memories", headers=ha).json() == {"deleted": 2}
        assert client.get("/v1/memories", headers=ha).json()["facts"] == []
        # the other tenant is untouched
        assert len(client.get("/v1/memories", headers=hb).json()["facts"]) == 1

    def test_clearing_nothing_reports_zero(self, memory_server):
        client, _ = memory_server
        h = _auth(client, "alice")
        assert client.delete("/v1/memories", headers=h).json() == {"deleted": 0}

    def test_a_deployment_without_memory_lists_empty(self, server):
        """The routes exist either way, so the UI needs no second code path."""
        h = _auth(server, "alice")
        body = server.get("/v1/memories", headers=h).json()
        assert body == {"facts": [], "status": "disabled"}
        assert server.delete("/v1/memories", headers=h).json() == {"deleted": 0}
        # NoOpStore.delete reports False, which surfaces as "not found" rather
        # than as a 500 about a missing vector database.
        assert server.delete("/v1/memories/1", headers=h).status_code == 404


EXTRACTED = '[{"text":"用户的项目用 uv 管理依赖","kind":"convention"}]'


class TestMemoryRun:
    """One real run through the HTTP stack: retrieve before, extract after.

    The unit tests in test_memory.py prove each half works; this is the only place
    the wiring between runner.run_turn, AgentLoop and MemoryService is exercised.
    """

    def test_a_run_retrieves_then_extracts(self, memory_server, monkeypatch):
        client, store = memory_server
        h, sid = _session(client, "alice")
        uid = _uid(client, "alice")
        _seed(client, store, "alice", "用户偏好 uv")

        seen: list[list[Message]] = []

        class Recording(StrictFakeProvider):
            async def stream(self, system, messages, tools):
                seen.append(list(messages))
                async for ev in super().stream(system, messages, tools):
                    yield ev

        monkeypatch.setattr("pi.server.runner.resolve_chain", lambda model, **kwargs: Recording())
        monkeypatch.setattr(
            "pi.memory.service.resolve_chain",
            lambda model: FakeProvider(responses=[[TextBlock(text=EXTRACTED)]]),
        )

        resp = client.post(
            f"/v1/sessions/{sid}/runs", json={"prompt": "uv 怎么装依赖"}, headers=h
        )
        assert resp.status_code == 200
        names = [name for name, _ in _frames(resp)]
        assert "turn_end" in names and "error" not in names

        # Retrieved facts reached the model, at index 0, ahead of the prompt.
        injected = seen[0][0].blocks[0].text
        assert injected.startswith(MEMORY_HEADER) and "用户偏好 uv" in injected
        assert seen[0][1].blocks[0].text == "uv 怎么装依赖"

        # ...and they are not in the transcript that got persisted.
        messages = client.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
        assert MEMORY_HEADER not in json.dumps(messages, ensure_ascii=False)

        # The extraction was queued behind the response, not awaited by it. Poll on
        # the app's loop: that is what gives the background task room to run.
        async def _extracted() -> bool:
            memories = UserMemoryRepo(client.app.state.db)
            for _ in range(200):
                pairs = await memories.get_active(uid)
                if any(f.text == "用户的项目用 uv 管理依赖" for f, _ in pairs):
                    return True
                await asyncio.sleep(0.01)
            return False

        assert client.portal.call(_extracted), "background extraction never stored a fact"

        # Now visible to the user through the API.
        texts = [f["text"] for f in client.get("/v1/memories", headers=h).json()["facts"]]
        assert "用户的项目用 uv 管理依赖" in texts


class TestDeregister:
    """DELETE /v1/me: the erasure path for deregistration.

    Every interaction leaves a trace in MySQL by design, so erasing an account
    means walking all of those tables - and the password re-confirmation is the
    entire security model of the route: a stolen token must not be enough to
    irreversibly destroy an account. The `purged` counts are the receipt.
    """

    def _counts(self, client: TestClient, uid: int, username: str) -> dict[str, int]:
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import AsyncSession

        from pi.server.db import (
            AgentRun,
            AgentStep,
            AuditEvent,
            MessageRow,
            SessionRow,
            UsageRecord,
            UserMemory,
        )

        async def _count() -> dict[str, int]:
            out: dict[str, int] = {}
            async with AsyncSession(client.app.state.db.engine) as s:
                for name, entity in (
                    ("memories", UserMemory),
                    ("sessions", SessionRow),
                    ("usage_records", UsageRecord),
                    ("agent_runs", AgentRun),
                ):
                    out[name] = (
                        await s.execute(
                            select(func.count(entity.id)).where(entity.user_id == uid)
                        )
                    ).scalar_one()
                out["agent_steps"] = (
                    await s.execute(
                        select(func.count(AgentStep.id)).where(
                            AgentStep.run_id.in_(
                                select(AgentRun.id).where(AgentRun.user_id == uid)
                            )
                        )
                    )
                ).scalar_one()
                out["messages"] = (
                    await s.execute(
                        select(func.count(MessageRow.id)).where(
                            MessageRow.session_id.in_(
                                select(SessionRow.id).where(SessionRow.user_id == uid)
                            )
                        )
                    )
                ).scalar_one()
                # audit rows are keyed by username, the way purge_user erases them
                out["audit_events"] = (
                    await s.execute(
                        select(func.count(AuditEvent.id)).where(AuditEvent.actor == username)
                    )
                ).scalar_one()
            return out

        return client.portal.call(_count)

    def _wait_audit_flushed(self, client: TestClient, username: str, at_least: int) -> int:
        """Audit rows reach MySQL through the background drainer, not the
        request path - poll on the app's loop so it gets room to run."""
        from pi.server.db import AuditEventRepo

        async def _wait() -> int:
            repo = AuditEventRepo(client.app.state.db)
            for _ in range(200):
                rows = await repo.list_recent(actor=username, limit=500)
                if len(rows) >= at_least:
                    return len(rows)
                await asyncio.sleep(0.01)
            return len(rows)

        return client.portal.call(_wait)

    def _wait_usage_settled(self, client: TestClient, uid: int, at_least: int) -> int:
        """A run bills itself synchronously, but the memory extraction it spawns
        keeps going afterwards and lands its own spend in usage_records with
        turns=0. Snapshotting `before` while those are still in flight made the
        purge receipt disagree with it in about one run in five.

        Waits for quiescence rather than a fixed row count: how many extraction
        rows arrive depends on what the provider yields, and pinning a number
        would couple this test to that instead of to the erasure contract."""
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import AsyncSession

        from pi.server.db import UsageRecord

        async def _wait() -> int:
            last, stable = -1, 0
            for _ in range(200):
                async with AsyncSession(client.app.state.db.engine) as s:
                    n = (
                        await s.execute(
                            select(func.count(UsageRecord.id)).where(
                                UsageRecord.user_id == uid
                            )
                        )
                    ).scalar_one()
                if n >= at_least and n == last:
                    stable += 1
                    if stable >= 5:
                        return n
                else:
                    stable = 0
                last = n
                await asyncio.sleep(0.01)
            return last

        return client.portal.call(_wait)

    @staticmethod
    def _deregister(client: TestClient, headers: dict, password: str):
        """TestClient.delete() takes no body, and this route lives in the body."""
        return client.request(
            "DELETE", "/v1/me", json={"password": password}, headers=headers
        )

    def test_requires_a_token(self, memory_server):
        client, _ = memory_server
        r = self._deregister(client, {}, "password123")
        assert r.status_code == 401

    def test_a_wrong_password_erases_nothing_and_is_audited(self, memory_server, tmp_path):
        client, store = memory_server
        h = _auth(client, "alice")
        _seed(client, store, "alice", "用户偏好 uv")

        r = self._deregister(client, h, "wrong-password")
        assert r.status_code == 400
        assert r.json()["detail"] == "password confirmation failed"

        # Nothing moved: the account, the fact, and the token are all still alive.
        assert client.get("/v1/me", headers=h).status_code == 200
        assert len(client.get("/v1/memories", headers=h).json()["facts"]) == 1

        rec = _auth_audit(tmp_path)[-1]
        assert (rec["action"], rec["ok"], rec["reason"]) == ("deregister", False, "bad_password")
        assert "wrong-password" not in json.dumps(rec), "never log the secret"

    def test_the_cascade_wipes_every_trace_and_the_token(self, memory_server, tmp_path):
        client, store = memory_server
        ha = _auth(client, "alice")
        hb = _auth(client, "bob")
        _seed(client, store, "alice", "alice 的事实")
        _seed(client, store, "alice", "alice 的另一条")
        _seed(client, store, "bob", "bob 的事实")
        # Real runs, not hand-written rows: messages and usage_records arrive in
        # exactly the shape production erasure has to cope with.
        for _ in range(2):
            sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=ha).json()["id"]
            resp = client.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=ha)
            assert resp.status_code == 200
        # Bob owns a row in the same tables, so "untouched" is a real boundary.
        client.post("/v1/sessions", json={"model": "fake/demo"}, headers=hb)

        ws = client.app.state.settings.workspace_root / "alice"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "notes.txt").write_text("scratch", encoding="utf-8")

        alice_uid, bob_uid = _uid(client, "alice"), _uid(client, "bob")
        # Alice's register+login must already be in audit_events before the
        # purge counts them, or the receipt is flaky by one row.
        assert self._wait_audit_flushed(client, "alice", at_least=2) >= 2
        # Same reason for usage_records: the two runs above each spawned a
        # background extraction that bills itself after the run has answered.
        assert self._wait_usage_settled(client, alice_uid, at_least=2) >= 2
        before = self._counts(client, alice_uid, "alice")
        assert before["usage_records"] >= 1, "a run must leave its billing trace"
        assert before["messages"] >= 4
        assert before["audit_events"] >= 2, "auth must leave its trace in MySQL"

        r = self._deregister(client, ha, "password123")
        assert r.status_code == 200, r.text
        assert r.json() == {
            "username": "alice",
            "deleted": True,
            "purged": {**before, "account": 1},
        }

        # The token died with the account...
        assert client.get("/v1/me", headers=ha).status_code == 401
        # ...the vector mirror went with it, and only hers...
        assert {owner for _, (owner, _) in store._index.items()} == {bob_uid}
        # ...and so did the workspace, which holds files no table knows about.
        assert not ws.exists()

        # Bob is untouched on every axis.
        assert [f["text"] for f in client.get("/v1/memories", headers=hb).json()["facts"]] == [
            "bob 的事实"
        ]
        assert len(client.get("/v1/sessions", headers=hb).json()["sessions"]) == 1
        assert self._wait_audit_flushed(client, "bob", at_least=2) == 2, "bob's audit history survives"

        # The username is free again, and the epoch does not follow it: a fresh
        # account under the same name gets a working token immediately - and a
        # completely clean slate, not the deleted account's leftovers. Under her
        # name remain exactly three audit rows: the erasure receipt itself
        # (written after the purge, on purpose) plus the new register and login.
        assert _register(client, "alice", "password123").status_code == 200
        fresh = {"Authorization": f"Bearer {_login(client, 'alice', 'password123')}"}
        assert client.get("/v1/me", headers=fresh).json() == {"username": "alice", "is_admin": False}
        assert self._wait_audit_flushed(client, "alice", at_least=3) == 3
        assert self._counts(client, _uid(client, "alice"), "alice") == {
            "memories": 0,
            "messages": 0,
            "sessions": 0,
            "usage_records": 0,
            "audit_events": 3,
            "agent_runs": 0,
            "agent_steps": 0,
        }

    def test_an_extraction_in_flight_at_deregister_leaves_nothing_behind(
        self, memory_server, monkeypatch, tmp_path
    ):
        """The race that waiting in the test only papered over.

        A run answers the user and spawns its memory extraction fire-and-forget,
        so an extraction is routinely still in flight when the next request
        arrives - including DELETE /v1/me. It writes user_memories and a turns=0
        usage_records row. Purge first and both land on an account that no longer
        exists: personal data survives an erasure whose receipt claimed to be
        complete, and the receipt under-reports by however many rows landed late.
        """
        client, store = memory_server
        ha = _auth(client, "alice")
        sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=ha).json()["id"]
        uid = _uid(client, "alice")

        entered, release = threading.Event(), threading.Event()

        async def held_extract(provider, messages):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            # Real work, so a regression has something to write: a fact for
            # user_memories and the spend that becomes the turns=0 usage row.
            return (
                [FactCandidate(text="alice 偏好 uv", kind="preference")],
                Usage(input_tokens=3),
            )

        monkeypatch.setattr("pi.memory.service.extract_facts", held_extract)
        ran = client.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hello"}, headers=ha)
        assert ran.status_code == 200
        assert entered.wait(5), "the run must have spawned the extraction"

        # Release while the DELETE is parked in drain(). Without suppression plus
        # the scoped drain the purge has already committed by this point.
        threading.Timer(0.2, release.set).start()
        r = self._deregister(client, ha, "password123")
        release.set()
        assert r.status_code == 200, r.text

        # The receipt is queued after the purge, and the queue is FIFO, so seeing
        # it means everything the extraction wrote has landed too.
        assert self._wait_audit_flushed(client, "alice", at_least=1) == 1
        after = self._counts(client, uid, "alice")
        assert after["memories"] == 0, "an in-flight extraction resurrected a fact"
        assert after["usage_records"] == 0, "an in-flight extraction resurrected its metering"
        assert after["sessions"] == 0 and after["messages"] == 0
        assert after["agent_runs"] == 0 and after["agent_steps"] == 0
        assert after["audit_events"] == 1, "only the erasure receipt may survive"

        # Completeness is recorded in the audit trail, not the response body:
        # DeregisterOut is part of the OpenAPI contract the frontend is codegen'd
        # from, and an auditor is who needs to find an incomplete erasure later.
        rows = _auth_audit(tmp_path)
        assert rows[-1]["action"] == "deregister"
        assert rows[-1]["erasure"] == "complete"

    def test_an_audit_record_still_queued_at_deregister_is_erased_not_orphaned(
        self, memory_server, monkeypatch
    ):
        """The sibling race, same promise.

        audit_events reach MySQL through a bounded queue and a background drainer,
        so a record queued before the purge can be inserted after it. purge_user()
        deletes by actor, and those rows carry IP and user agent - precisely the
        personal data the erasure existed to remove.
        """
        client, store = memory_server
        release = threading.Event()
        real = AuditEventRepo.append_many

        async def held_append(self, records):
            await asyncio.to_thread(release.wait, 5)
            return await real(self, records)

        # Patched before registering, so the rows this test is about cannot land.
        monkeypatch.setattr(AuditEventRepo, "append_many", held_append)
        ha = _auth(client, "alice")
        uid = _uid(client, "alice")
        # Precondition, and what stops this test from passing vacuously if the
        # timer below ever fires before the DELETE reaches flush().
        assert self._counts(client, uid, "alice")["audit_events"] == 0

        threading.Timer(0.5, release.set).start()
        r = self._deregister(client, ha, "password123")
        release.set()
        assert r.status_code == 200, r.text

        assert r.json()["purged"]["audit_events"] >= 2, (
            "register and login must be counted as erased, not left queued to "
            "land after the DELETE"
        )
        assert self._wait_audit_flushed(client, "alice", at_least=1) == 1
        assert self._counts(client, uid, "alice")["audit_events"] == 1, (
            "only the erasure receipt may survive it"
        )

    def test_the_last_admin_cannot_delete_themselves(self, memory_server):
        client, _ = memory_server
        h = _auth(client, "admin")
        repo = UserRepo(client.app.state.db)

        async def _promote(username: str) -> None:
            user = await repo.by_username(username)
            await repo.set_admin(user.id, True)

        client.portal.call(_promote, "admin")
        r = self._deregister(client, h, "password123")
        assert r.status_code == 400
        assert r.json()["detail"] == "cannot delete the last admin"
        assert client.get("/v1/me", headers=h).status_code == 200

        # A second admin is the escape hatch.
        _auth(client, "backup")
        client.portal.call(_promote, "backup")
        assert self._deregister(client, h, "password123").status_code == 200


class TestTraces:
    """agent_runs/agent_steps: every POST /runs leaves its execution trace.

    The trace is written when the stream ends (same lifecycle as the usage
    row), so a completed HTTP response implies a complete trace - no polling
    needed, unlike the async audit drainer.
    """

    def _admin(self, client: TestClient, username: str = "root") -> dict:
        h = _auth(client, username)
        repo = UserRepo(client.app.state.db)

        async def _promote() -> None:
            user = await repo.by_username(username)
            await repo.set_admin(user.id, True)

        client.portal.call(_promote)
        return h

    def test_a_run_with_a_tool_call_is_traced_step_by_step(self, server, monkeypatch):
        _scripted(monkeypatch, [[[TextBlock(text="我看看目录。"), _tc("c1", "ls", {"path": "."})]]])
        h, sid = _session(server, "alice")
        resp = server.post(f"/v1/sessions/{sid}/runs", json={"prompt": "列一下"}, headers=h)
        assert resp.status_code == 200
        request_id = resp.headers["X-Request-Id"]

        admin = self._admin(server)
        runs = server.get(
            "/v1/admin/traces", headers=admin, params={"user": "alice"}
        ).json()["runs"]
        assert len(runs) == 1
        run = runs[0]
        assert run["username"] == "alice" and run["session_id"] == sid
        assert run["model"] == "fake/demo"
        assert run["status"] == "ok" and run["flags"] == []
        assert run["turns"] >= 1 and run["input_tokens"] > 0
        assert run["duration_ms"] >= 0 and run["ended_at"]
        assert run["prompt"] == "列一下", "the input rides on the trace"
        assert run["request_id"] == request_id, "correlates with X-Request-Id / access log"
        assert run["steps"] is None, "the list view omits steps on purpose"
        assert run["messages"] is None, "the list view omits the transcript on purpose"

        detail = server.get(f"/v1/admin/traces/{run['run_id']}", headers=admin).json()
        assert detail["run_id"] == run["run_id"]
        assert detail["prompt"] == "列一下"
        # One step per model round-trip and one per tool call, in the order they
        # happened: the turn that asked for the tool, the tool itself, and the turn
        # that answered after it. Two round-trips is the part the run's totals
        # cannot show - a looping run and a slow run have the same duration.
        assert [(s["kind"], s["name"]) for s in detail["steps"]] == [
            ("llm_call", "demo"),
            ("tool_call", "ls"),
            ("llm_call", "demo"),
        ]
        assert [s["seq"] for s in detail["steps"]] == [0, 1, 2]

        calls = [s for s in detail["steps"] if s["kind"] == "llm_call"]
        assert all(s["ok"] for s in calls) and all(s["duration_ms"] >= 0 for s in calls)
        assert json.loads(calls[0]["detail"])["turn"] == 1
        assert json.loads(calls[0]["detail"])["stop_reason"] == "tool_use"
        assert json.loads(calls[1]["detail"])["turn"] == 2

        step = detail["steps"][1]
        assert step["ok"] is True and step["seq"] == 1 and step["ts"]
        assert json.loads(step["args"]) == {"path": "."}, "full tool arguments are traced"
        assert step["detail"], "the full result is part of the trace"

        # The transcript slice: input -> assistant output, joined through
        # first_idx/last_idx on the very rows the run persisted.
        msgs = detail["messages"]
        assert detail["first_idx"] == 0 and detail["last_idx"] == len(msgs) - 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["blocks"][0]["text"] == "列一下"
        roles = [m["role"] for m in msgs]
        assert "assistant" in roles
        # The tool call and its result are both in the slice, in block form.
        kinds = [b["type"] for m in msgs for b in m["blocks"]]
        assert "tool_call" in kinds and "tool_result" in kinds

    def test_a_failing_run_is_flagged_and_filterable(self, server, monkeypatch):
        class Exploding(StrictFakeProvider):
            async def stream(self, system, messages, tools):
                raise RuntimeError("模型网关炸了")
                yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr("pi.server.runner.resolve_chain", lambda model, **kwargs: Exploding())
        h, sid = _session(server, "bob")
        resp = server.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h)
        assert resp.status_code == 200
        assert "error" in [name for name, _ in _frames(resp)]

        admin = self._admin(server)
        runs = server.get(
            "/v1/admin/traces", headers=admin, params={"user": "bob"}
        ).json()["runs"]
        assert len(runs) == 1
        run = runs[0]
        assert run["status"] == "error"
        assert "RuntimeError: 模型网关炸了" in run["error"]
        # The loop itself surfaces provider failures as an ErrorEvent and still
        # emits TurnEnd, so the run has turns - the flag that matters is status.
        assert "error" in run["flags"]

        # The anomaly filter finds it, and so does the status filter.
        anomalous = server.get(
            "/v1/admin/traces", headers=admin, params={"anomaly": "true"}
        ).json()["runs"]
        assert [r["run_id"] for r in anomalous] == [run["run_id"]]
        by_status = server.get(
            "/v1/admin/traces", headers=admin, params={"status": "error"}
        ).json()["runs"]
        assert [r["run_id"] for r in by_status] == [run["run_id"]]

        detail = server.get(f"/v1/admin/traces/{run['run_id']}", headers=admin).json()
        # The round-trip that raised is recorded before the run-level error, and it
        # is the row that says *what* broke: the ErrorEvent only says the run did.
        assert [s["kind"] for s in detail["steps"]] == ["llm_call", "error"]
        assert detail["steps"][0]["ok"] is False
        assert "RuntimeError: 模型网关炸了" in json.loads(detail["steps"][0]["detail"])["error"]
        assert detail["steps"][1]["ok"] is False

    def test_a_memory_run_records_what_was_retrieved(self, memory_server, monkeypatch):
        """The retrieval step is the only record of what the model was given.

        loop.py keeps the injected block out of the persisted transcript, so
        first_idx..last_idx cannot replay it. Without this row, "the model forgot
        what I told it" has no evidence to start from - which was the whole gap.
        """
        client, store = memory_server
        h, sid = _session(client, "alice")
        _seed(client, store, "alice", "用户偏好 uv")
        _scripted(monkeypatch, [[[TextBlock(text="好")]]])

        resp = client.post(
            f"/v1/sessions/{sid}/runs", json={"prompt": "uv 怎么装依赖"}, headers=h
        )
        assert resp.status_code == 200

        admin = self._admin(client)
        run = client.get(
            "/v1/admin/traces", headers=admin, params={"user": "alice"}
        ).json()["runs"][0]
        detail = client.get(f"/v1/admin/traces/{run['run_id']}", headers=admin).json()
        steps = detail["steps"]

        step = steps[0]
        assert step["kind"] == "retrieval", "retrieval is the first thing a run does"
        assert step["ok"] is True and step["name"] == "memory.retrieve"
        assert step["args"] == "uv 怎么装依赖", "the query facts were selected for"
        assert step["duration_ms"] >= 0

        stats = json.loads(step["detail"])
        assert stats["outcome"] == "injected"
        assert stats["kept"] == 1 and stats["recalled"] >= 1
        assert "用户偏好 uv" in stats["text"], "the injected block itself is the record"
        candidate = stats["candidates"][0]
        assert candidate["verdict"] == "kept"
        assert candidate["cosine"] >= stats["min_similarity"], "the gate travels with the verdict"
        assert {"embed", "recall", "join"} <= set(stats["stages_ms"]), "per-stage latency"
        assert stats["index"], "which path recalled: the vector store, or the repo fallback"

        # The rest of the run is still there, after it.
        assert [s["kind"] for s in steps[1:]] == ["llm_call"]

    def test_a_broken_retrieval_is_flagged_but_does_not_fail_the_run(self, memory_server, monkeypatch):
        """The quiet failure: a healthy-looking run with the memory silently gone.

        Every retrieval stage is guarded so memory can never break a turn, which
        also means nothing downstream reports it. This is the step and the flag that
        do - a dead embedding endpoint is otherwise indistinguishable from "the user
        has no facts yet".
        """
        client, store = memory_server
        h, sid = _session(client, "bob")
        _seed(client, store, "bob", "用户偏好 uv")
        _scripted(monkeypatch, [[[TextBlock(text="好")]]])

        async def down(self, texts):
            raise ConnectionError("embedding endpoint is down")

        monkeypatch.setattr(HashEmbedder, "embed", down)

        resp = client.post(
            f"/v1/sessions/{sid}/runs", json={"prompt": "uv 怎么装依赖"}, headers=h
        )
        assert resp.status_code == 200
        names = [name for name, _ in _frames(resp)]
        assert "turn_end" in names and "error" not in names, "the run still answered"

        admin = self._admin(client)
        run = client.get(
            "/v1/admin/traces", headers=admin, params={"user": "bob"}
        ).json()["runs"][0]
        assert run["status"] == "ok", "a retrieval failure is not a run failure"
        assert run["flags"] == ["memory_failed"]

        # Which is how an operator finds these without reading every run.
        anomalous = client.get(
            "/v1/admin/traces", headers=admin, params={"anomaly": "true"}
        ).json()["runs"]
        assert [r["run_id"] for r in anomalous] == [run["run_id"]]

        detail = client.get(f"/v1/admin/traces/{run['run_id']}", headers=admin).json()
        step = detail["steps"][0]
        assert step["kind"] == "retrieval" and step["ok"] is False
        stats = json.loads(step["detail"])
        assert stats["outcome"] == "embed_failed" and stats["stage"] == "embed"
        assert "ConnectionError" in stats["error"]
        assert stats["kept"] == 0 and stats["text"] == ""
        assert "embed" in stats["stages_ms"], "a stage that failed still reports its time"

    def test_auth_gates_and_unknown_run_404(self, server):
        admin = self._admin(server)
        assert server.get("/v1/admin/traces", headers=admin).json() == {"runs": []}
        assert server.get("/v1/admin/traces/deadbeef0000", headers=admin).status_code == 404
        h = _auth(server, "alice")
        assert server.get("/v1/admin/traces", headers=h).status_code == 403
        assert server.get("/v1/admin/traces").status_code == 401
        assert server.get("/v1/admin/traces/deadbeef0000").status_code == 401

    def test_retention_deletes_old_runs_and_their_steps(self, server):
        from pi.server.db import AgentRunRepo

        repo = AgentRunRepo(server.app.state.db)
        old_day = "2020-01-01T00:00:00+00:00"
        now_day = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        async def seed_and_sweep():
            await repo.append(
                run_id="oldrun000001", user_id=1, username="old", session_id="s1",
                model="m", status="ok", error="", input_tokens=1, output_tokens=1,
                turns=1, failed_tools=0, duration_ms=1.0, flags=[],
                started_at=old_day,
                steps=[{"kind": "tool_call", "name": "ls", "ok": True, "detail": "x"}],
            )
            await repo.append(
                run_id="newrun000001", user_id=1, username="new", session_id="s2",
                model="m", status="ok", error="", input_tokens=1, output_tokens=1,
                turns=1, failed_tools=0, duration_ms=1.0, flags=[],
                started_at=now_day, steps=[],
            )
            # A year-old cutoff: only the 2020 run is older.
            cutoff = "2021-01-01T00:00:00+00:00"
            removed = await repo.delete_older_than(cutoff)
            kept_old = await repo.get_run("oldrun000001")
            kept_new = await repo.get_run("newrun000001")
            return removed, kept_old, kept_new

        removed, kept_old, kept_new = server.portal.call(seed_and_sweep)
        assert removed == (1, 1), "one run and its one step"
        assert kept_old is None
        assert kept_new is not None


class TestTraceFlags:
    def test_the_three_verdicts(self):
        from pi.server.runner import trace_flags

        assert trace_flags("ok", 3, 0) == [], "unremarkable run"
        assert trace_flags("timeout", 3, 0) == ["timeout"]
        # user paid, model answered with nothing
        assert trace_flags("ok", 0, 0) == ["empty"]
        # a loop or a broken environment
        assert trace_flags("ok", 3, 3) == ["tool_storm"]
        assert trace_flags("error", 0, 5) == ["error", "empty", "tool_storm"]


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


class TestAuditMysql:
    """audit_events is the source of truth; the JSONL file is a mirror.

    The drainer is asynchronous, so every assertion polls the app's loop for the
    rows to appear - the same pattern the memory extraction tests use.
    """

    def _admin(self, client: TestClient, username: str = "root") -> dict:
        """Register + promote through the DB-only path, return auth headers."""
        h = _auth(client, username)
        repo = UserRepo(client.app.state.db)

        async def _promote() -> None:
            user = await repo.by_username(username)
            await repo.set_admin(user.id, True)

        client.portal.call(_promote)
        return h

    def _wait(self, client: TestClient, at_least: int, **filters) -> list[dict]:
        from pi.server.db import AuditEventRepo

        async def _poll() -> list[dict]:
            repo = AuditEventRepo(client.app.state.db)
            rows: list[dict] = []
            for _ in range(200):
                rows = await repo.list_recent(limit=500, **filters)
                if len(rows) >= at_least:
                    return rows
                await asyncio.sleep(0.01)
            return rows

        return client.portal.call(_poll)

    def test_every_event_lands_in_mysql(self, memory_server):
        client, _ = memory_server
        _register(client, "audited", "password123")
        assert _register(client, "audited", "password123").status_code == 409
        _login(client, "audited", "password123")
        assert client.post(
            "/v1/auth/login", json={"username": "audited", "password": "wrong"}
        ).status_code == 401

        rows = self._wait(client, 4, actor="audited")
        assert [(r["event"], r["action"], r["ok"]) for r in rows] == [
            ("auth", "login", False),
            ("auth", "login", True),
            ("auth", "register", False),
            ("auth", "register", True),
        ], "newest first, in arrival order"
        assert all(r["ip"] for r in rows)

    def test_the_admin_endpoint_reads_mysql_not_the_file(self, memory_server):
        client, _ = memory_server
        h = self._admin(client)
        _register(client, "alice", "password123")
        _login(client, "alice", "password123")
        # Poll on alice's rows: drainer order is queue order, so root's own
        # register+login are in by the time hers are.
        assert len(self._wait(client, 2, actor="alice")) == 2

        # Remove the JSONL mirror entirely: if the endpoint still answers, it
        # answers from the database.
        from pi.security.audit import _daily_path

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _daily_path(client.app.state.settings.audit_path, day).unlink(missing_ok=True)

        body = client.get("/v1/admin/audit", headers=h).json()
        events = {(r["event"], r.get("action")) for r in body["records"]}
        assert ("auth", "register") in events and ("auth", "login") in events

        # Filters go through the indexed columns...
        only_alice = client.get(
            "/v1/admin/audit", headers=h, params={"user": "alice"}
        ).json()
        assert only_alice["records"] and all(
            r.get("username") == "alice" for r in only_alice["records"]
        )
        auth_only = client.get(
            "/v1/admin/audit", headers=h, params={"event": "auth"}
        ).json()
        assert auth_only["records"] and all(r["event"] == "auth" for r in auth_only["records"])
        # ...and a user with no records yet gets an empty page, not a 500.
        assert (
            client.get("/v1/admin/audit", headers=h, params={"user": "nobody"}).json()
            == {"records": []}
        )

    def test_the_endpoint_requires_admin(self, memory_server):
        client, _ = memory_server
        h = _auth(client, "alice")
        assert client.get("/v1/admin/audit", headers=h).status_code == 403
        assert client.get("/v1/admin/audit").status_code == 401


PROXY_IP = "172.18.0.2"  # inside 172.16/12 (Docker bridge), outside 127.0.0.1
REAL_IP = "203.0.113.9"


class TestForwardedFor:
    """PI_FORWARDED_ALLOW_IPS decides whether request.client.host is the real
    client or the reverse proxy. Caddy runs in its own compose container, so
    uvicorn's 127.0.0.1 default silently drops X-Forwarded-For and every user
    gets logged as Caddy - which would also collapse any IP rate limit into a
    single bucket shared by the whole world."""

    def _register_ip(self, tmp_path: Path, monkeypatch, trusted: str, xff: str) -> str:
        monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'xff.db').as_posix()}")
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


# One instance per event the runner can emit, so the drift test below has a
# realistic payload to serialize rather than a hand-picked convenient one.
RUNNER_EVENTS = {
    "text_delta": TextDeltaEvent(text="你好 world"),
    "toolcall_start": ToolCallStartEvent(id="call_1", name="bash"),
    "toolcall_end": ToolCallEndEvent(id="call_1", name="bash", ok=False, result="exit 1"),
    "compaction": CompactionEvent(dropped=3, chars_before=9000, chars_after=1200),
    "plan": PlanEvent(plan=Plan(title="重构 sessions 表", steps=["加可空 plan 列", "写迁移"])),
    "turn_end": TurnEndEvent(usage=Usage(input_tokens=10, output_tokens=4), turns=2),
    "error": ErrorEvent(message="run timed out after 600s"),
}


def _parse_frame(frame: str) -> tuple[str, dict]:
    """'event: text_delta\\ndata: {"text": "hi"}' -> ('text_delta', {'text': 'hi'})"""
    name = ""
    data: dict = {}
    for line in frame.splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
    return name, data


class TestSseEventPayloads:
    """The Sse*Data models in app.py are documentation that gets compiled into
    the browser's TypeScript. Nothing but these tests stops them drifting away
    from what event_to_sse() actually writes onto the wire."""

    @pytest.mark.parametrize("name", sorted(RUNNER_EVENTS))
    def test_event_to_sse_matches_its_documented_model(self, name):
        got_name, data = _parse_frame(event_to_sse(RUNNER_EVENTS[name]))
        assert got_name == name
        assert name in SSE_DATA_MODELS, f"event_to_sse emits an undocumented event: {name}"
        SSE_DATA_MODELS[name].model_validate(data)

    def test_every_documented_event_is_one_the_server_emits(self):
        """Reverse direction: a model with no producer is stale documentation.

        `start` and `done` are built inline in app.py's stream() rather than by
        event_to_sse, so they are covered by the live test below instead.
        """
        inline = {"start", "done"}
        assert set(SSE_DATA_MODELS) - inline == set(RUNNER_EVENTS)

    def test_tool_result_truncation_matches_its_documented_preview(self):
        """The wire value is already cut to PREVIEW_LEN by the agent loop, so
        event_to_sse's own 400-char cap never binds in practice. The description
        quotes that constant, so changing it without updating the browser-facing
        docs fails here."""
        desc = SSE_DATA_MODELS["toolcall_end"].model_fields["result"].description or ""
        assert f"cuts it to {PREVIEW_LEN} characters" in desc
        _, data = _parse_frame(
            event_to_sse(ToolCallEndEvent(id="c", name="bash", ok=True, result="x" * 5000))
        )
        assert len(data["result"]) == 400

    def test_unserializable_event_falls_back_to_the_documented_marker(self):
        """A new AgentEvent type reaches the browser as `event: unknown` + `{}`,
        which is why SSE_DOC tells clients to ignore names they do not know."""
        name, data = _parse_frame(event_to_sse(object()))
        assert (name, data) == ("unknown", {})

    def test_live_stream_frames_match_the_documented_models(self, server):
        """Covers `start` and `done`, which event_to_sse never sees."""
        _register(server, "alice", "password123")
        h = {"Authorization": f"Bearer {_login(server, 'alice', 'password123')}"}
        sid = server.post("/v1/sessions", json={}, headers=h).json()["id"]

        r = server.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")

        seen: dict[str, list[dict]] = {}
        for frame in r.text.split("\n\n"):
            if not frame.strip():
                continue
            name, data = _parse_frame(frame)
            assert name in SSE_DATA_MODELS, f"undocumented SSE event on the wire: {name}"
            SSE_DATA_MODELS[name].model_validate(data)
            seen.setdefault(name, []).append(data)

        assert seen["start"] == [{"session": sid, "model": "fake/demo"}]
        assert seen["done"] == [{}]
        assert "".join(d["text"] for d in seen["text_delta"])  # the reply, in chunks
        assert seen["turn_end"] == [{"turns": 1, "usage": {"input_tokens": 1, "output_tokens": 1}}]

    def test_trace_only_events_never_reach_the_wire(self, memory_server, monkeypatch):
        """RetrievalEvent and LlmCallEvent are recorded, not streamed.

        Neither has a Sse*Data model, so letting either through would put
        `event: unknown` on the wire for every run - and web/'s KNOWN_EVENTS is a
        closed set precisely so an undocumented frame cannot be ignored forever.
        This needs the memory fixture: without a configured store there is no
        retrieval event to filter.
        """
        client, store = memory_server
        h, sid = _session(client, "alice")
        _seed(client, store, "alice", "用户偏好 uv")
        _scripted(monkeypatch, [[[TextBlock(text="好")]]])

        resp = client.post(
            f"/v1/sessions/{sid}/runs", json={"prompt": "uv 怎么装依赖"}, headers=h
        )
        assert resp.status_code == 200
        names = {name for name, _ in _frames(resp)}
        assert "unknown" not in names
        assert names == {"start", "text_delta", "turn_end", "done"}

        # Filtered from the stream, but still in the trace - the point of both.
        admin = TestTraces()._admin(client)
        run = client.get(
            "/v1/admin/traces", headers=admin, params={"user": "alice"}
        ).json()["runs"][0]
        kinds = [
            s["kind"]
            for s in client.get(f"/v1/admin/traces/{run['run_id']}", headers=admin).json()["steps"]
        ]
        assert "retrieval" in kinds and "llm_call" in kinds


class TestMetricsEndpoint:
    """/metrics: aggregate, scrapeable, and the only thing an alert can fire on."""

    def _app(self, tmp_path: Path, monkeypatch, **env) -> TestClient:
        monkeypatch.setenv(
            "PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}"
        )
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return TestClient(create_app(ServerSettings.from_env()))

    def test_a_completed_run_shows_up_in_the_series(self, server, monkeypatch):
        _scripted(
            monkeypatch, [[[TextBlock(text="hi"), _tc("c1", "ls", {"path": "."})]]]
        )
        h, sid = _session(server, "alice")
        posted = server.post(f"/v1/sessions/{sid}/runs", json={"prompt": "列一下"}, headers=h)
        assert posted.status_code == 200

        r = server.get("/metrics")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        assert 'pi_runs_total{model="fake/demo",status="ok"} 1.0' in body
        assert 'pi_tool_calls_total{ok="True",tool="ls"} 1.0' in body
        assert 'pi_llm_calls_total{model="demo",ok="True"}' in body
        assert "pi_runs_in_flight 0.0" in body, "the gauge is back down after the run"
        # Aggregate only. A per-user label would grow without bound and take the
        # metrics backend down with it - and would put usernames in Prometheus.
        assert "alice" not in body and sid not in body

    def test_metrics_are_not_part_of_the_generated_api(self, server):
        """web/ compiles TypeScript from this document; a scrape target is not an API."""
        assert "/metrics" not in server.get("/openapi.json").json()["paths"]

    def test_a_token_gates_it_and_a_wrong_one_404s(self, tmp_path: Path, monkeypatch):
        with self._app(tmp_path, monkeypatch, PI_METRICS_TOKEN="sekret") as client:
            # 404, not 403: "forbidden" tells a scanner the endpoint is there.
            assert client.get("/metrics").status_code == 404
            wrong = {"Authorization": "Bearer not-it"}
            assert client.get("/metrics", headers=wrong).status_code == 404
            ok = client.get("/metrics", headers={"Authorization": "Bearer sekret"})
            assert ok.status_code == 200
            assert ok.headers["content-type"].startswith("text/plain")
            assert "pi_runs_total" in ok.text, "registered families render with no samples yet"

    def test_switching_metrics_off_answers_503_with_the_reason(self, tmp_path: Path, monkeypatch):
        with self._app(tmp_path, monkeypatch, PI_METRICS="0") as client:
            r = client.get("/metrics")
            assert r.status_code == 503
            assert "PI_METRICS" in r.text, "say how to turn it back on"


class TestMessagesContract:
    def test_blocks_is_a_flat_array_not_a_nested_message(self, server):
        """Regression: the `blocks` column stores a whole serialized Message
        (runner.py writes m.model_dump_json()), and the route used to return
        that raw parse under `blocks`. Clients saw the role twice and had to
        reach into blocks.blocks to find the actual content."""
        _register(server, "alice", "password123")
        h = {"Authorization": f"Bearer {_login(server, 'alice', 'password123')}"}
        sid = server.post("/v1/sessions", json={}, headers=h).json()["id"]
        server.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h)

        r = server.get(f"/v1/sessions/{sid}/messages", headers=h)
        assert r.status_code == 200
        msgs = r.json()["messages"]
        assert [m["idx"] for m in msgs] == [0, 1]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        for m in msgs:
            assert isinstance(m["blocks"], list)
            for b in m["blocks"]:
                assert b["type"] in ("text", "tool_call", "tool_result")
        assert msgs[0]["blocks"][0]["text"] == "hi"


class TestOpenApiContract:
    """web/ generates its types from this document. A response left untyped here
    becomes a hand-written type in the browser, i.e. the two sources of truth
    the codegen setup exists to prevent."""

    @pytest.fixture()
    def schema(self, server):
        r = server.get("/openapi.json")
        assert r.status_code == 200
        return r.json()

    def test_no_response_body_is_left_untyped(self, schema):
        loose = [
            f"{method.upper()} {path} {code}"
            for path, ops in schema["paths"].items()
            for method, op in ops.items()
            for code, resp in op["responses"].items()
            for body in resp.get("content", {}).values()
            if body.get("schema") == {"additionalProperties": True, "type": "object"}
        ]
        assert loose == []

    def test_sse_payloads_are_merged_into_components(self, schema):
        """The stream route returns a StreamingResponse, so FastAPI has no
        response model to discover them from; custom_openapi() adds them."""
        names = set(schema["components"]["schemas"])
        missing = {m.__name__ for m in SSE_DATA_MODELS.values()} - names
        assert not missing

    def test_the_stream_route_documents_event_stream_and_its_failures(self, schema):
        resp = schema["paths"]["/v1/sessions/{session_id}/runs"]["post"]["responses"]
        # No application/json alongside it: the body is a stream, never an object.
        assert set(resp["200"]["content"]) == {"text/event-stream"}
        refs = {
            s["$ref"].rsplit("/", 1)[-1]
            for s in resp["200"]["content"]["text/event-stream"]["schema"]["anyOf"]
        }
        assert refs == {m.__name__ for m in SSE_DATA_MODELS.values()}
        for code in ("401", "402", "404", "429"):
            assert code in resp
        assert "Retry-After" in resp["429"]["headers"]

    def test_every_error_body_is_the_documented_shape(self, schema):
        """The client has one ApiError path, so every non-2xx body must be
        {"detail": ...}. 422 is FastAPI's own HTTPValidationError and 503 is
        ReadyOut, so both are excluded on purpose."""
        for path, ops in schema["paths"].items():
            for method, op in ops.items():
                for code, resp in op["responses"].items():
                    if code in ("200", "422", "503"):
                        continue
                    ref = resp["content"]["application/json"]["schema"]["$ref"]
                    assert ref == "#/components/schemas/ErrorOut", f"{method.upper()} {path} {code}"


class TestWebUiMount:
    """The built frontend is served by the API process itself.

    There is no CORS middleware, so the browser and /v1 have to share an origin,
    and a Caddy that only proxies to app:8300 has nowhere else to serve web/dist
    from. The mount sits at "/" and is registered last - that ordering is the
    part worth pinning, because getting it wrong makes every API route answer
    with index.html.
    """

    @pytest.fixture()
    def ui_server(self, tmp_path: Path, monkeypatch):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
        (dist / "assets" / "index-abc123.js").write_text("export {}", encoding="utf-8")
        monkeypatch.setenv("PI_WEB_DIST", str(dist))
        monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}")
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        with TestClient(create_app(ServerSettings.from_env())) as client:
            yield client

    def test_index_and_assets_are_served(self, ui_server):
        index = ui_server.get("/")
        assert index.status_code == 200
        assert '<div id="app"></div>' in index.text
        asset = ui_server.get("/assets/index-abc123.js")
        assert asset.status_code == 200
        assert asset.text == "export {}"

    def test_the_mount_does_not_shadow_the_api(self, ui_server):
        assert ui_server.get("/healthz").json() == {"status": "ok"}
        assert "openapi" in ui_server.get("/openapi.json").json()
        assert ui_server.get("/docs").status_code == 200
        # The failure that would be silent: an unauthenticated API call must come
        # back as a 401 JSON body, not as the SPA served with a 200.
        r = ui_server.get("/v1/me")
        assert r.status_code == 401
        assert set(r.json()) == {"detail"}

    def test_an_unknown_path_is_a_404_not_the_index(self, ui_server):
        assert ui_server.get("/nope").status_code == 404

    def test_a_missing_dist_is_not_an_error(self, server):
        """conftest points PI_WEB_DIST somewhere that does not exist: the
        backend-only and CI cases must still boot and serve the API."""
        assert server.get("/healthz").status_code == 200
        assert server.get("/").status_code == 404
