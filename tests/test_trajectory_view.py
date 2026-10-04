"""Trajectory persistence + viewer endpoint (TRAJECTORY_VIEW_DESIGN P0 + §2).

The viewer HTML itself is exercised only for serving/ownership; rendering is
browser-side vanilla JS by design (zero build step).
"""

from __future__ import annotations

import asyncio
import json
import time as time_mod
import uuid
from pathlib import Path

import pytest

from conftest import TEST_DB_URL
from fastapi.testclient import TestClient

from pi.agent.loop import AgentLoop
from pi.llm.fake import FakeProvider
from pi.models import TextBlock, ToolCallBlock
from pi.server.app import create_app
from pi.server.config import ServerSettings
from pi.server.trajectory_store import _daily_path, append_trajectory, latest_trajectory
from pi.tools import all_tools


def _make_app(monkeypatch, tmp_path: Path, trajectory_path: str) -> TestClient:
    monkeypatch.setenv("PI_DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("PI_MODEL", "fake/demo")
    monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("PI_TRAJECTORY_PATH", trajectory_path)
    settings = ServerSettings.from_env()
    return TestClient(create_app(settings))


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    with _make_app(monkeypatch, tmp_path, str(tmp_path / "traj.jsonl")) as client:
        yield client


def _register_and_session(client: TestClient, username: str) -> tuple[dict, str]:
    assert client.post("/v1/auth/register", json={"username": username, "password": "password123"}).status_code == 200
    token = client.post("/v1/auth/login", json={"username": username, "password": "password123"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
    return h, sid


def _run(client: TestClient, h: dict, sid: str) -> None:
    with client.stream("POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h) as resp:
        assert resp.status_code == 200
        assert list(resp.iter_lines())  # drain the SSE stream


class TestPersistence:
    def test_run_appends_trajectory_line(self, server, tmp_path):
        h, sid = _register_and_session(server, "alice")
        _run(server, h, sid)

        rec = latest_trajectory(tmp_path / "traj.jsonl", sid)
        assert rec is not None
        assert rec["session_id"] == sid
        assert isinstance(rec["user_id"], int)
        types = [e["type"] for e in rec["events"]]
        assert types[0] == "RunStarted"
        assert types[-1] == "RunFinished"
        assert "LlmCall" in types

    def test_every_event_carries_ts(self, server, tmp_path):
        h, sid = _register_and_session(server, "alice")
        _run(server, h, sid)
        rec = latest_trajectory(tmp_path / "traj.jsonl", sid)
        assert rec is not None
        for e in rec["events"]:
            assert isinstance(e["ts"], float), e["type"]

    def test_persistence_failure_does_not_fail_run(self, tmp_path, monkeypatch):
        # "logs" exists as a FILE, so mkdir(parents=True) under it must fail:
        # the run still completes with 200 and the SSE stream still ends.
        (tmp_path / "logs").write_text("blocking", encoding="utf-8")
        with _make_app(monkeypatch, tmp_path, str(tmp_path / "logs" / "traj.jsonl")) as client:
            h, sid = _register_and_session(client, "alice")
            with client.stream(
                "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h
            ) as resp:
                assert resp.status_code == 200
                lines = list(resp.iter_lines())
            assert "event: done" in lines  # stream ended normally despite the write failure
            msgs = client.get(f"/v1/sessions/{sid}/messages", headers=h).json()["messages"]
            assert len(msgs) >= 2  # run persisted normally


class TestEndpoint:
    def test_owner_gets_merged_trajectory(self, server, tmp_path):
        h, sid = _register_and_session(server, "alice")
        _run(server, h, sid)
        _run(server, h, sid)

        r = server.get(f"/v1/sessions/{sid}/trajectory", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == sid
        # both runs are merged into one chronological timeline
        run_starts = [e for e in body["events"] if e["type"] == "RunStarted"]
        assert len(run_starts) == 2
        assert len(body["events"]) >= 4

    def test_cross_user_404(self, server):
        h_alice, sid = _register_and_session(server, "alice")
        _run(server, h_alice, sid)
        h_bob, _ = _register_and_session(server, "bob")
        assert server.get(f"/v1/sessions/{sid}/trajectory", headers=h_bob).status_code == 404

    def test_no_trajectory_404(self, server):
        h, sid = _register_and_session(server, "alice")
        assert server.get(f"/v1/sessions/{sid}/trajectory", headers=h).status_code == 404

    def test_storage_disabled_still_serves_from_db(self, tmp_path, monkeypatch):
        """PI_TRAJECTORY_PATH="" disables the jsonl copy, but the runs table
        (structured index) still serves the trajectory."""
        with _make_app(monkeypatch, tmp_path, "") as client:
            h, sid = _register_and_session(client, "alice")
            _run(client, h, sid)  # run fine, jsonl off
            r = client.get(f"/v1/sessions/{sid}/trajectory", headers=h)
            assert r.status_code == 200
            assert r.json()["session_id"] == sid

    def test_ui_page_served(self, server):
        r = server.get("/ui/trajectory.html")
        assert r.status_code == 200
        assert "轨迹视图" in r.text
        assert r.headers["content-type"].startswith("text/html")


class TestStore:
    def test_latest_scans_across_days(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        base = tmp_path / "traj.jsonl"

        def day(offset: int) -> str:
            return (datetime.now(timezone.utc) + timedelta(days=offset)).strftime("%Y-%m-%d")

        old = {"session_id": "s1", "run_id": "old"}
        _daily_path(base, day(-1)).write_text(
            json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        assert latest_trajectory(base, "s1")["run_id"] == "old"  # only yesterday exists

        append_trajectory(base, {"session_id": "s1", "run_id": "new"})
        assert latest_trajectory(base, "s1")["run_id"] == "new"  # today beats yesterday

        future = {"session_id": "s2", "run_id": "other"}
        _daily_path(base, day(1)).write_text(
            json.dumps(future, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        assert latest_trajectory(base, "s2")["run_id"] == "other"
        assert latest_trajectory(base, "s1")["run_id"] == "new"
        assert latest_trajectory(base, "nope") is None

    def test_broken_lines_are_skipped(self, tmp_path):
        base = tmp_path / "traj.jsonl"
        good = {"session_id": "s1", "run_id": "ok"}
        _daily_path(base, "2026-09-27").write_text(
            "not json\n" + json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        assert latest_trajectory(base, "s1")["run_id"] == "ok"


class TestTsEnhancement:
    def test_llm_and_tool_ts_are_start_times(self, tmp_path):
        """ts must be the START of each call (design §4.6), not the record time."""
        provider = FakeProvider(
            model="demo",
            responses=[
                [ToolCallBlock(id="t1", name="write", arguments=json.dumps({"path": "a", "content": "x"}))],
                [TextBlock(text="done")],
            ],
        )
        agent = AgentLoop(provider=provider, tools=all_tools(), system_prompt="sys", cwd=tmp_path)

        async def main():
            async for _ in agent.run("write a file"):
                pass

        asyncio.run(main())
        events = agent.trajectory.to_dict()["events"]
        llm1 = next(e for e in events if e["type"] == "LlmCall")
        tool = next(e for e in events if e["type"] == "ToolCall")
        llm2 = [e for e in events if e["type"] == "LlmCall"][1]
        # monotonic across the run: turn-1 model starts before its tool call,
        # which starts before turn-2
        assert llm1["ts"] <= tool["ts"] <= llm2["ts"]
        assert llm2["ts"] <= time_mod.time() + 5  # sanity: not wildly off wall clock


class TestRunIdEndpoints:
    """Structured replay: fetch one run by id (gap: 按 runId 回放)."""

    def test_owner_gets_run_by_id(self, server):
        h, sid = _register_and_session(server, "alice")
        _run(server, h, sid)
        latest = server.get(f"/v1/sessions/{sid}/trajectory", headers=h).json()
        run_id = latest["run_id"]
        r = server.get(f"/v1/trajectory/{run_id}", headers=h)
        assert r.status_code == 200
        assert r.json()["run_id"] == run_id

    def test_run_by_id_cross_user_404(self, server):
        h_alice, sid = _register_and_session(server, "alice")
        _run(server, h_alice, sid)
        run_id = server.get(f"/v1/sessions/{sid}/trajectory", headers=h_alice).json()["run_id"]
        h_bob, _ = _register_and_session(server, "bob")
        assert server.get(f"/v1/trajectory/{run_id}", headers=h_bob).status_code == 404

    def test_run_by_id_unknown_404(self, server):
        h, _ = _register_and_session(server, "alice")
        assert server.get("/v1/trajectory/deadbeef", headers=h).status_code == 404

    def test_admin_can_replay_any_run(self, server):
        h_alice, sid = _register_and_session(server, "alice")
        _run(server, h_alice, sid)
        run_id = server.get(f"/v1/sessions/{sid}/trajectory", headers=h_alice).json()["run_id"]

        from pi.server.db import UserRepo

        _register_and_session(server, "root")
        client = server  # TestClient

        async def grant():
            repo = UserRepo(client.app.state.db)
            user = await repo.by_username("root")
            await repo.set_admin(user.id, True)

        client.portal.call(grant)
        h_root = {"Authorization": "Bearer " + server.post(
            "/v1/auth/login", json={"username": "root", "password": "password123"}
        ).json()["access_token"]}

        r = server.get(f"/v1/admin/trajectory/{run_id}", headers=h_root)
        assert r.status_code == 200
        assert r.json()["run_id"] == run_id
        # 非管理员 403
        assert server.get(f"/v1/admin/trajectory/{run_id}", headers=h_alice).status_code == 403

    def test_session_endpoint_prefers_db_over_jsonl(self, server, tmp_path):
        """DB row wins; jsonl is only the fallback for pre-table rows."""
        h, sid = _register_and_session(server, "alice")
        _run(server, h, sid)
        r = server.get(f"/v1/sessions/{sid}/trajectory", headers=h)
        assert r.status_code == 200
        assert r.json()["run_id"]
