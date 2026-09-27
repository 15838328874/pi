"""Fallback chain, metering/quota, and tracing tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta
from pi.llm.fallback import FallbackProvider
from pi.llm.registry import resolve_chain
from pi.models import Message, Role, TextBlock, ToolSpec, Usage
from pi.observability.prices import estimate_cost, load_prices
from pi.observability.tracing import JsonlTracer, NoOpTracer, get_tracer


def _msg(text: str = "hi") -> Message:
    return Message(role=Role.user, blocks=[TextBlock(text=text)])


class BrokenProvider(LLMProvider):
    """Always raises a transient-style error."""

    name = "broken"

    def __init__(self, model: str = "broken/model", exc: Exception | None = None):
        self.model = model
        self.exc = exc or ConnectionError("connection refused")
        self.calls = 0

    async def stream(self, system, messages, tools):
        self.calls += 1
        raise self.exc
        yield  # pragma: no cover


class WorkingProvider(LLMProvider):
    name = "working"

    def __init__(self, model: str = "ok/model"):
        self.model = model

    async def stream(self, system, messages, tools):
        yield TextDelta("hello")
        yield StreamEnd("end_turn", Usage(input_tokens=1, output_tokens=1))


class HalfProvider(LLMProvider):
    """Fails mid-stream (already emitted events) - must NOT be retried."""

    name = "half"

    def __init__(self, model: str = "half/model"):
        self.model = model
        self.calls = 0

    async def stream(self, system, messages, tools):
        self.calls += 1
        yield TextDelta("partial")
        raise ConnectionError("mid-stream failure")


class TestFallback:
    def test_degrades_to_working_model(self):
        async def main():
            broken = BrokenProvider("primary/model")
            ok = WorkingProvider("backup/model")
            seen = []

            async def on_fb(frm, to, reason):
                seen.append((frm, to))

            provider = FallbackProvider(broken, [ok], on_fallback=on_fb)
            events = [ev async for ev in provider.stream("s", [_msg()], [])]
            return broken, ok, seen, events

        broken, ok, seen, events = asyncio.run(main())
        assert any(isinstance(ev, TextDelta) for ev in events)
        assert any(isinstance(ev, StreamEnd) for ev in events)
        assert seen == [("primary/model", "backup/model")]
        assert broken.calls == 3  # 1 + 2 retries

    def test_non_transient_error_propagates(self):
        async def main():
            provider = FallbackProvider(BrokenProvider(exc=ValueError("bad args")), [WorkingProvider()])
            events = []
            async for ev in provider.stream("s", [_msg()], []):
                events.append(ev)
            return events

        with pytest.raises(ValueError):
            asyncio.run(main())

    def test_midstream_failure_not_retried(self):
        async def main():
            half = HalfProvider()
            provider = FallbackProvider(half, [HalfProvider("other/model")])
            events = []
            try:
                async for ev in provider.stream("s", [_msg()], []):
                    events.append(ev)
            except ConnectionError:
                pass
            return half, events

        half, events = asyncio.run(main())
        assert half.calls == 1  # no retry after partial output
        assert len(events) == 1  # the single partial delta

    def test_all_models_down_raises_last(self):
        async def main():
            provider = FallbackProvider(BrokenProvider("a/m"), [BrokenProvider("b/m")])
            async for _ in provider.stream("s", [_msg()], []):
                pass

        with pytest.raises(ConnectionError):
            asyncio.run(main())

    def test_resolve_chain_wraps_fallback(self, monkeypatch):
        monkeypatch.setenv("PI_FALLBACK_CHAIN", "fake/demo,fake/backup")
        provider = resolve_chain("fake/demo")
        assert isinstance(provider, FallbackProvider)

    def test_resolve_chain_empty_is_plain(self, monkeypatch):
        monkeypatch.delenv("PI_FALLBACK_CHAIN", raising=False)
        provider = resolve_chain("fake/demo")
        assert not isinstance(provider, FallbackProvider)


class TestPrices:
    def test_builtin_price(self):
        prices = load_prices()
        assert "qwen3.8-max" in prices

    def test_estimate_cost(self):
        cost = estimate_cost("openai/qwen3.8-max", 1_000_000, 1_000_000)
        assert cost == pytest.approx(1.6 + 6.4)

    def test_unknown_model_zero(self):
        assert estimate_cost("unknown/model", 100, 100) == 0.0


class TestTracing:
    def test_noop_tracer(self):
        tracer = get_tracer("noop")
        assert isinstance(tracer, NoOpTracer)
        with tracer.track("agent.run", {"model": "x"}) as span:
            span.set_attribute("turns", 1)

    def test_jsonl_tracer_writes(self, tmp_path: Path):
        path = tmp_path / "traces.jsonl"
        tracer = get_tracer("jsonl", path=path)
        assert isinstance(tracer, JsonlTracer)
        with tracer.track("tool.call", {"tool": "bash", "ok": True}) as span:
            span.set_attribute("duration_ms", 12.5)
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = tmp_path / f"traces-{day}.jsonl"
        lines = daily.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["span"] == "tool.call"
        assert rec["tool"] == "bash"
        assert rec["ok"] is True
        assert rec["duration_ms"] >= 0
        assert rec["trace_id"]

    def test_tracer_records_exception(self, tmp_path: Path):
        path = tmp_path / "traces.jsonl"
        tracer = JsonlTracer(path)
        with pytest.raises(RuntimeError):
            with tracer.track("agent.run", {}):
                raise RuntimeError("boom")
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rec = json.loads((tmp_path / f"traces-{day}.jsonl").read_text(encoding="utf-8").strip().splitlines()[0])
        assert rec["ok"] is False
        assert "RuntimeError" in rec["error"]


class TestServerMetering:
    def test_usage_recorded_and_summary(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'u.db').as_posix()}")
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_TRACER", "noop")
        from pi.server.app import create_app
        from pi.server.config import ServerSettings

        app = create_app(ServerSettings.from_env())
        with TestClient(app) as client:
            client.post("/v1/auth/register", json={"username": "alice", "password": "password123"})
            token = client.post(
                "/v1/auth/login", json={"username": "alice", "password": "password123"}
            ).json()["access_token"]
            h = {"Authorization": f"Bearer {token}"}
            sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
            with client.stream(
                "POST", f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h
            ) as resp:
                list(resp.iter_lines())

            summary = client.get("/v1/usage", headers=h).json()
            assert summary["total_input_tokens"] >= 1
            assert summary["total_output_tokens"] >= 1
            assert summary["used_tokens"] >= 2
            assert any(m["model"] == "fake/demo" for m in summary["models"])

    def test_quota_exhaustion_returns_402(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'q.db').as_posix()}")
        monkeypatch.setenv("PI_REDIS_NS", "test-" + uuid.uuid4().hex[:8])
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
        monkeypatch.setenv("PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
        monkeypatch.setenv("PI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
        monkeypatch.setenv("PI_TRACER", "noop")
        monkeypatch.setenv("PI_DEFAULT_QUOTA_TOKENS", "1")  # 1 token quota
        from pi.server.app import create_app
        from pi.server.config import ServerSettings

        app = create_app(ServerSettings.from_env())
        with TestClient(app) as client:
            client.post("/v1/auth/register", json={"username": "alice", "password": "password123"})
            token = client.post(
                "/v1/auth/login", json={"username": "alice", "password": "password123"}
            ).json()["access_token"]
            h = {"Authorization": f"Bearer {token}"}
            sid = client.post("/v1/sessions", json={"model": "fake/demo"}, headers=h).json()["id"]
            # soft quota: first run passes (used=0 < 1), records 2 tokens,
            # second run is blocked
            r1 = client.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h)
            assert r1.status_code == 200
            r2 = client.post(f"/v1/sessions/{sid}/runs", json={"prompt": "hi"}, headers=h)
            assert r2.status_code == 402
            assert "quota" in r2.json()["detail"]
