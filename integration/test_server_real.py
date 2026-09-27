"""Real-stack server chain: Postgres + Redis + (Milvus/embedding when set).

Registers a throwaway user, runs a fake-model turn end to end, verifies the
readiness probes and persistence, then removes every trace of the user.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from integration.conftest import require_embedding_vars
from pi.server.app import create_app
from pi.server.config import ServerSettings

pytestmark = pytest.mark.skipif(
    os.environ.get("PI_INTEGRATION") != "1",
    reason="real-stack integration tests; set PI_INTEGRATION=1 + PI_ITEST_* to run",
)


def _settings() -> ServerSettings:
    from pathlib import Path

    s = ServerSettings(
        database_url=os.environ["PI_ITEST_DATABASE_URL"],
        redis_url=os.environ["PI_ITEST_REDIS_URL"],
        redis_ns="itest",
        jwt_secret="itest-secret-0123456789abcdef0123456789abcdef",
        default_model="fake/demo",
        sandbox="",
        policy_path="",
        audit_path=Path("/tmp/pi-itest-audit.jsonl"),
        workspace_root=Path("/tmp/pi-itest-workspaces"),
    )
    if all(
        os.environ.get(k)
        for k in ("PI_ITEST_MILVUS_URI", "PI_ITEST_EMBEDDING_URL", "PI_ITEST_EMBEDDING_API_KEY", "PI_ITEST_EMBEDDING_MODEL")
    ):
        s.embedding_url = os.environ["PI_ITEST_EMBEDDING_URL"]
        s.embedding_api_key = os.environ["PI_ITEST_EMBEDDING_API_KEY"]
        s.embedding_model = os.environ["PI_ITEST_EMBEDDING_MODEL"]
        s.milvus_uri = os.environ["PI_ITEST_MILVUS_URI"]
    return s


async def _cleanup_user(db, username: str) -> None:
    async with db.engine.connect() as conn:
        await conn.execute(text("DELETE FROM usage_records WHERE username = :u"), {"u": username})
        await conn.execute(text("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = :u))"), {"u": username})
        await conn.execute(text("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = :u)"), {"u": username})
        await conn.execute(text("DELETE FROM users WHERE username = :u"), {"u": username})
        await conn.commit()


def test_full_server_chain():
    settings = _settings()
    username = f"itest_{int(time.time())}"
    with TestClient(create_app(settings)) as client:
        r = client.get("/readyz")
        assert r.status_code == 200, r.text
        checks = r.json()["checks"]
        assert checks["db"] == "ok"
        assert checks["cache"] == "ok"
        if settings.vector_memory_enabled:
            assert checks["milvus"] == "ok"

        r = client.post("/v1/auth/register", json={"username": username, "password": "itest-pass-123"})
        assert r.status_code == 200, r.text
        token = client.post(
            "/v1/auth/login", json={"username": username, "password": "itest-pass-123"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        sid = client.post("/v1/sessions", json={"title": "itest"}, headers=headers).json()["id"]
        r = client.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=headers)
        assert r.status_code == 200
        body = r.text
        assert "event: done" in body and "event: text_delta" in body

        msgs = client.get(f"/v1/sessions/{sid}/messages", headers=headers).json()["messages"]
        assert len(msgs) >= 2  # user + assistant persisted

        # cleanup on the app's own loop (asyncpg connections are loop-bound)
        client.portal.call(_cleanup_user, client.app.state.db, username)
