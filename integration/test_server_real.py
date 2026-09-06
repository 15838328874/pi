"""The full HTTP path against real infrastructure.

One golden-path test: signup -> login -> a real-model run that states durable
preferences -> background extraction lands in MySQL + Milvus (observed through
/v1/memories) -> a second run whose answer depends on the injected memory.
Every hop is real: MySQL pi_py_test, Redis ns=test, docker sandbox,
qwen-flash chat, qwen-flash extraction, embedding + rerank, Milvus it_memories.

MySQL is the source of truth, so the test closes by asserting every
interaction table holds this user's rows directly in SQL - not through the
API that reads them: user_memories (with its packed embedding blob and the
milvus_synced receipt), messages, usage_records, audit_events (auth + tool
calls), and the agent_runs/agent_steps execution traces.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

PREFERENCES = (
    "跟我们合作的几个约定先说清楚，之后都照此执行：这个项目统一用 uv 管理 Python "
    "依赖，不要再用 pip，uv.lock 必须提交到仓库；后端是 FastAPI，数据库是 MySQL 8，"
    "ORM 用 SQLAlchemy 2.0 的异步引擎，所有查询走异步会话，不要在协程里写同步的"
    "数据库调用；测试框架用 pytest，但绝对不要引入 pytest-asyncio 这个插件，异步"
    "测试统一用 asyncio.run 包一层同步函数来跑；代码注释一律用中文写，函数和变量"
    "的命名用英文；错误处理不要裸抛，统一走项目的统一异常封装再返回给上层；分支"
    "命名用 feature/ 加短横线的英文小写；提交信息用中文，一行说清楚改了什么就行，"
    "不用写长篇说明；给我们的回复一律用中文写，回答简洁直接，不要每次都重复这些"
    "约定；另外我们每周五下午五点开一次项目例会，会上要过一遍当周合入的所有提交，"
    "你如果替我写周报就按这个结构来组织。就这些，开始吧。"
)
# The prompt alone must stay above PI_MEMORY_EXTRACT_MIN_CHARS (400): whether the
# model answers in plain text or reaches for tools then varies run to run, and a
# short pure-text exchange would legitimately skip extraction via the
# skip-short-turns guard, failing the test for no real reason.
assert len(PREFERENCES) > 400

ASK = "我之前跟你说过这个项目用什么工具管理 Python 依赖吗？直接回答工具名就行。"


@pytest.fixture()
def real_server():
    from pi.server.app import create_app
    from pi.server.config import ServerSettings

    app = create_app(ServerSettings.from_env())
    with TestClient(app) as client:
        yield client, app


def _register(client: TestClient, username: str, password: str):
    r = client.post("/v1/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _run(client: TestClient, sid: str, headers: dict, prompt: str) -> tuple[list[str], str]:
    """One SSE run -> (event names, concatenated assistant text)."""
    events: list[str] = []
    text: list[str] = []
    with client.stream(
        "POST", f"/v1/sessions/{sid}/runs", json={"prompt": prompt}, headers=headers
    ) as resp:
        assert resp.status_code == 200, resp.read().decode()
        name = ""
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
                events.append(name)
            elif line.startswith("data: ") and name == "text_delta":
                text.append(json.loads(line.removeprefix("data: "))["text"])
    return events, "".join(text)


def _sql(sql: str, params: dict) -> list:
    """Run one statement against the real test database.

    A throwaway engine per call: the app's pooled aiomysql connections were
    created inside TestClient's portal loop and cannot be reused from this
    thread's loop. Fresh engine, fresh connection, disposed - deterministic.
    """
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def go():
        engine = create_async_engine(os.environ["PI_DATABASE_URL"])
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                if sql.lstrip().lower().startswith("select"):
                    return result.all()
                await conn.commit()
                return [(result.rowcount,)]
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _purge_user(username: str) -> None:
    """Remove this test's rows from pi_py_test: every table the interaction
    design says must hold a trace - the same cascade as DELETE /v1/me."""
    rows = _sql("SELECT id FROM users WHERE username = :u", {"u": username})
    if not rows:
        return
    uid = rows[0][0]
    for stmt in (
        "DELETE FROM agent_steps WHERE run_id IN "
        "(SELECT id FROM agent_runs WHERE user_id = :i)",
        "DELETE FROM agent_runs WHERE user_id = :i",
        "DELETE FROM audit_events WHERE actor = :n",
        "DELETE FROM user_memories WHERE user_id = :i",
        "DELETE FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE user_id = :i)",
        "DELETE FROM sessions WHERE user_id = :i",
        "DELETE FROM usage_records WHERE user_id = :i",
        "DELETE FROM users WHERE id = :i",
    ):
        _sql(stmt, {"i": uid, "n": username})


def _memory_usage_rows(username: str) -> int:
    """turns=0 rows for this user in the real pi_py_test usage_records table."""
    rows = _sql(
        "SELECT COUNT(*) FROM usage_records u JOIN users s ON u.user_id = s.id "
        "WHERE s.username = :n AND u.turns = 0",
        {"n": username},
    )
    return int(rows[0][0])


def _wait_audit_rows(username: str, at_least: int, timeout: float = 30.0) -> list[tuple]:
    """Audit rows reach MySQL through the background drainer, not the request
    path - poll until at least `at_least` events by this actor have landed."""
    deadline = time.time() + timeout
    rows: list[tuple] = []
    while time.time() < deadline:
        rows = _sql(
            "SELECT event, tool FROM audit_events WHERE actor = :n ORDER BY id",
            {"n": username},
        )
        if len(rows) >= at_least:
            return rows
        time.sleep(0.5)
    return rows


def test_a_run_writes_memory_and_the_next_run_uses_it(real_server):
    client, _ = real_server
    username = f"it_{uuid.uuid4().hex[:8]}"
    token = ""
    try:
        _register(client, username, "password123")
        token = _login(client, username, "password123")
        h = {"Authorization": f"Bearer {token}"}
        sid = client.post(
            "/v1/sessions", json={"model": "openai/qwen-flash"}, headers=h
        ).json()["id"]

        events1, _ = _run(client, sid, h, PREFERENCES)
        assert events1[-1] == "done", f"run 1 did not finish: {events1[-3:]}"
        assert "error" not in events1

        # The extraction is fire-and-forget: poll the listing until it lands.
        deadline = time.time() + 120
        facts: list[dict] = []
        while time.time() < deadline:
            facts = client.get("/v1/memories", headers=h).json()["facts"]
            if facts:
                break
            time.sleep(2)
        assert facts, "background extraction never landed in Milvus"
        assert any("uv" in f["text"] for f in facts), [f["text"] for f in facts]

        events2, answer = _run(client, sid, h, ASK)
        assert events2[-1] == "done"
        assert "uv" in answer, f"memory was not injected or ignored: {answer[:400]!r}"

        # The extraction spend must have reached the real MySQL meter table.
        assert _memory_usage_rows(username) >= 1

        # ---- every interaction table, asserted in SQL, not through the API ----
        uid = _sql("SELECT id FROM users WHERE username = :u", {"u": username})[0][0]

        # 1. user_memories owns the fact, its embedding blob, and the sync receipt.
        mem = _sql(
            "SELECT content, LENGTH(embedding), milvus_synced FROM user_memories "
            "WHERE user_id = :i AND is_active = 1",
            {"i": uid},
        )
        assert mem, "extracted facts never landed in MySQL user_memories"
        assert any("uv" in r[0] for r in mem), [r[0] for r in mem]
        assert all(r[1] and r[1] > 0 for r in mem), "embedding blob missing"
        assert all(r[2] == 1 for r in mem), "vector index never confirmed the upsert"

        # 2. messages: both runs' turns persisted for the session.
        n_msgs = _sql(
            "SELECT COUNT(*) FROM messages WHERE session_id = :s", {"s": sid}
        )[0][0]
        assert n_msgs >= 4, f"expected both runs' messages, found {n_msgs}"

        # 3. agent_runs/agent_steps: one trace row per run, written before "done".
        runs = _sql(
            "SELECT run_id, status, session_id, model, flags FROM agent_runs "
            "WHERE user_id = :i ORDER BY id",
            {"i": uid},
        )
        assert len(runs) == 2, f"expected one trace per run, found {len(runs)}"
        assert {r[1] for r in runs} == {"ok"}, [r[1] for r in runs]
        assert {r[2] for r in runs} == {sid}, "trace rows lost their session link"
        assert all(r[0] for r in runs), "run_id column empty"
        assert all("qwen-flash" in r[3] for r in runs), [r[3] for r in runs]
        assert all(r[4] == "" for r in runs), f"healthy runs flagged: {[r[4] for r in runs]}"
        # Whether the model reached for tools varies run to run; steps and
        # tool_call audit rows exist exactly when it did.
        tool_starts = sum(e == "toolcall_start" for e in events1 + events2)
        step_rows = _sql(
            "SELECT COUNT(*) FROM agent_steps WHERE run_id IN "
            "(SELECT id FROM agent_runs WHERE user_id = :i)",
            {"i": uid},
        )[0][0]
        if tool_starts:
            assert step_rows >= 1, "tool calls left no steps behind"

        # 4. audit_events: auth + memory events, through the background drainer.
        audit = _wait_audit_rows(username, at_least=3)
        kinds = {r[0] for r in audit}
        assert "auth" in kinds, audit
        assert "memory" in kinds, audit
        if tool_starts:
            assert any(r[1] for r in audit), f"no tool_call audit events: {audit}"
    finally:
        if token:
            client.delete("/v1/memories", headers={"Authorization": f"Bearer {token}"})
        _purge_user(username)
