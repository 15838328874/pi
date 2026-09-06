"""Fallback chain, metering/quota, and tracing tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi.llm.base import LLMProvider, StreamEnd, StreamEvent, TextDelta
from pi.llm.fallback import FallbackProvider
from pi.llm.registry import resolve_chain
from pi.models import Message, Role, TextBlock, ToolSpec, Usage
from pi.observability.metrics import Metrics
from pi.observability.prices import estimate_cost, load_prices
from pi.observability.tracing import JsonlTracer, NoOpTracer, OtelTracer, get_tracer


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

    def test_spans_nest_and_each_root_gets_its_own_trace(self, tmp_path: Path):
        """A flat list of siblings cannot say what happened inside what.

        The trace_id used to be minted once per process, so every run since boot
        shared one id and the day's file was unreadable as a tree. Now the root
        span mints it and its descendants inherit, which is what makes "show me
        this run" possible at all.
        """
        tracer = JsonlTracer(tmp_path / "traces.jsonl")
        with tracer.track("agent.run", {"session": "s1"}):
            with tracer.track("memory.retrieve", {"kept": 2}):
                with tracer.track("memory.recall", {"hits": 20}):
                    pass
            with tracer.track("llm.call", {"model": "m", "turn": 1}):
                pass
        with tracer.track("agent.run", {"session": "s2"}):
            pass

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = (tmp_path / f"traces-{day}.jsonl").read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        # Spans are written when they END, so both roots land after their own
        # children, in the order the runs finished.
        by_name: dict[str, dict] = {}
        for row in rows:
            by_name.setdefault(row["span"], row)
        runs = [r for r in rows if r["span"] == "agent.run"]
        assert len(runs) == 2

        first, second = runs[0], runs[1]
        assert first["session"] == "s1" and second["session"] == "s2"
        assert first["parent_span_id"] == "", "a root has no parent"
        assert first["trace_id"] != second["trace_id"], "one trace per run, not per process"

        for child in ("memory.retrieve", "llm.call"):
            assert by_name[child]["trace_id"] == first["trace_id"]
            assert by_name[child]["parent_span_id"] == first["span_id"]
        assert by_name["memory.recall"]["parent_span_id"] == by_name["memory.retrieve"]["span_id"]
        assert by_name["memory.recall"]["trace_id"] == first["trace_id"]

    def test_otel_exports_one_tree_per_run(self):
        """The exporter is the half that was missing.

        A TracerProvider with no processor builds spans and drops them, so the
        previous OtelTracer looked instrumented and produced nothing. This pins the
        whole path: parenting, error status, the exception event, and the resource
        attributes a collector filters on.
        """
        pytest.importorskip("opentelemetry.sdk.trace")
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exported = InMemorySpanExporter()
        tracer = get_tracer(
            "otel",
            service_name="pi-py",
            environment="test",
            endpoint="http://localhost:4317",
            exporter=exported,
        )
        assert isinstance(tracer, OtelTracer)

        with tracer.track("agent.run", {"session": "s1", "user": "alice"}):
            with tracer.track("memory.retrieve", {"kept": 2}):
                with tracer.track("memory.recall", {"hits": 20}):
                    pass
            with tracer.track("llm.call", {"model": "m", "turn": 1}):
                pass
            try:
                with tracer.track("tool.call", {"tool": "bash"}):
                    raise ValueError("bad args")
            except ValueError:
                pass
        tracer.shutdown()  # flushes BatchSpanProcessor; without it the last spans go missing

        spans = {s.name: s for s in exported.get_finished_spans()}
        assert set(spans) == {
            "agent.run", "memory.retrieve", "memory.recall", "llm.call", "tool.call"
        }
        assert len({s.context.trace_id for s in spans.values()}) == 1, "one run, one trace"
        assert spans["agent.run"].parent is None
        for child in ("memory.retrieve", "llm.call", "tool.call"):
            assert spans[child].parent.span_id == spans["agent.run"].context.span_id
        assert spans["memory.recall"].parent.span_id == spans["memory.retrieve"].context.span_id

        failed = spans["tool.call"]
        assert failed.status.status_code.name == "ERROR"
        assert failed.status.description == "ValueError: bad args"
        assert any(e.attributes["exception.type"] == "ValueError" for e in failed.events)

        resource = spans["agent.run"].resource.attributes
        assert resource["service.name"] == "pi-py"
        assert resource["deployment.environment"] == "test"

    def test_otel_without_the_exporter_degrades_to_jsonl_loudly(self, tmp_path: Path, monkeypatch, caplog):
        """The fallback must say it happened.

        Silently returning JsonlTracer is how a deployment ends up with
        PI_TRACER=otel set, no collector data, and no idea why.
        """
        def no_exporter(*args, **kwargs):
            raise ImportError("opentelemetry-exporter-otlp-proto-grpc")

        monkeypatch.setattr("pi.observability.tracing.OtelTracer", no_exporter)
        path = tmp_path / "traces.jsonl"
        with caplog.at_level("WARNING", logger="pi.observability"):
            tracer = get_tracer("otel", path=path)
        assert isinstance(tracer, JsonlTracer)
        assert "otel" in caplog.text.lower() and "ImportError" in caplog.text

    def test_an_unknown_backend_disables_tracing_and_says_so(self, caplog):
        """A typo in PI_TRACER used to mean 'no tracing at all', silently."""
        with caplog.at_level("WARNING", logger="pi.observability"):
            tracer = get_tracer("jsnol")
        assert isinstance(tracer, NoOpTracer)
        assert "jsnol" in caplog.text


class TestMetrics:
    """/metrics' backing registry: what gets counted, and what must never leak in."""

    @staticmethod
    def _text(metrics: Metrics) -> str:
        rendered = metrics.render()
        assert rendered is not None
        return rendered[0].decode("utf-8")

    def test_recording_produces_the_series_a_dashboard_needs(self):
        metrics = Metrics(enabled=True)
        assert metrics.enabled, "prometheus-client is in the observability extra"

        async def record() -> None:
            # in_flight is async because the runner enters it alongside the run
            # semaphore in one `async with`, and that needs async managers.
            async with metrics.in_flight():
                metrics.llm_call(model="m", ok=True, duration_s=0.4)
                metrics.tool_call(tool="bash", ok=False, duration_s=0.01)
                metrics.retrieval(outcome="injected", duration_s=0.2, kept=3, index_path="index")

        asyncio.run(record())
        metrics.run_finished(
            status="ok", model="openai/m", duration_s=1.5, turns=2, tokens_in=100, tokens_out=20
        )

        text = self._text(metrics)
        assert metrics.render()[1].startswith("text/plain")
        assert 'pi_runs_total{model="openai/m",status="ok"} 1.0' in text
        assert 'pi_llm_calls_total{model="m",ok="True"} 1.0' in text
        assert 'pi_tool_calls_total{ok="False",tool="bash"} 1.0' in text
        assert 'pi_memory_retrievals_total{outcome="injected"} 1.0' in text
        assert 'pi_tokens_total{direction="input",model="openai/m"} 100.0' in text
        assert "pi_run_duration_seconds_bucket" in text

    def test_the_in_flight_gauge_comes_back_down_after_a_failure(self):
        """A gauge that only climbs is the most convincing false alert there is."""
        metrics = Metrics(enabled=True)

        async def boom() -> None:
            async with metrics.in_flight():
                raise RuntimeError("run blew up")

        with pytest.raises(RuntimeError):
            asyncio.run(boom())
        assert "pi_runs_in_flight 0.0" in self._text(metrics)

    def test_a_healthy_index_is_not_counted_as_a_fallback(self):
        """index_fallbacks ticks only when the vector database was NOT used.

        Asserted on the sample line, not the metric name: prometheus-client emits
        a HELP and a TYPE line for every registered family whether or not anything
        was ever observed, so the name alone is always present.
        """
        metrics = Metrics(enabled=True)
        metrics.retrieval(outcome="injected", duration_s=0.1, kept=1, index_path="index")
        assert "pi_memory_index_fallbacks_total{" not in self._text(metrics)
        metrics.retrieval(outcome="injected", duration_s=0.1, kept=1, index_path="index_failed")
        assert 'pi_memory_index_fallbacks_total{path="index_failed"} 1.0' in self._text(metrics)

    def test_no_series_carries_a_user_or_a_session(self):
        """Cardinality, and privacy: both are why agent_runs exists separately."""
        metrics = Metrics(enabled=True)
        metrics.run_finished(
            status="ok", model="openai/m", duration_s=1.0, turns=1, tokens_in=1, tokens_out=1
        )
        metrics.retrieval(outcome="injected", duration_s=0.1, kept=1, index_path="index")
        text = self._text(metrics)
        for label in ("username", "user_id", "session", "prompt", "run_id"):
            assert f'{label}=' not in text, f"{label} must never be a metric label"

    def test_disabled_metrics_render_nothing(self):
        """Which is what turns /metrics into a 503 rather than an empty 200."""
        metrics = Metrics(enabled=False)
        assert metrics.render() is None

        async def record() -> None:
            async with metrics.in_flight():
                metrics.llm_call(model="m", ok=True, duration_s=0.1)
                metrics.tool_call(tool="bash", ok=True, duration_s=0.1)
                metrics.retrieval(outcome="injected", duration_s=0.1, kept=1)
                metrics.trace_write_failed()

        # Every recorder is a no-op rather than an AttributeError on a missing
        # registry: a dev install without the extra still serves runs.
        asyncio.run(record())
        metrics.run_finished(
            status="ok", model="m", duration_s=0.1, turns=1, tokens_in=1, tokens_out=1
        )
        assert metrics.render() is None


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
