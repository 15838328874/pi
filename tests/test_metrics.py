"""Metrics tests: span hook, run/tool projection, gauge, gate, label discipline.

prometheus_client is a test-visible dependency (observability extra); the
Metrics class itself no-ops when it is missing, so these tests skip if the
package is absent rather than failing a lean dev install.
"""

from __future__ import annotations

import asyncio

import pytest

pc = pytest.importorskip("prometheus_client")

from pi.observability.metrics import Metrics  # noqa: E402
from pi.observability.tracing import JsonlTracer, NoOpTracer, get_tracer  # noqa: E402


class _Span:
    def __init__(self, **data):
        self._data = {"ok": True, **data}


def _label_map(metrics: Metrics, name: str) -> dict[tuple, float]:
    """{sorted label tuple: value} for one metric. prometheus-client strips the
    _total suffix from Counter names in the registry, so match both forms."""
    out: dict[tuple, float] = {}
    for m in metrics.registry.collect():
        if m.name == name or m.name + "_total" == name:
            for s in m.samples:
                if s.name == name or s.name == m.name:
                    out[tuple(sorted(s.labels.items()))] = s.value
    return out


class TestSpanProjection:
    def test_llm_call_ok_and_failure(self):
        m = Metrics(enabled=True)
        m.observe_span("llm.call", _Span(model="qwen3.8-max", ok=True), 0.5)
        m.observe_span("llm.call", _Span(model="qwen3.8-max", ok=False), 0.2)
        m.observe_span("llm.call", _Span(model="qwen3.6-flash", ok=True), 0.3)
        calls = _label_map(m, "pi_llm_calls_total")
        assert calls[(("model", "qwen3.8-max"), ("ok", "True"))] == 1
        assert calls[(("model", "qwen3.8-max"), ("ok", "False"))] == 1
        assert calls[(("model", "qwen3.6-flash"), ("ok", "True"))] == 1

    def test_non_llm_spans_ignored(self):
        m = Metrics(enabled=True)
        m.observe_span("tool.call", _Span(tool="bash", ok=True), 0.1)
        m.observe_span("agent.run", _Span(), 1.0)
        assert _label_map(m, "pi_llm_calls_total") == {}

    def test_tracer_track_hook_feeds_metrics(self, tmp_path):
        m = Metrics(enabled=True)
        tracer = get_tracer("jsonl", path=tmp_path / "tr", metrics=m)
        with tracer.track("llm.call", {"model": "x"}):
            pass
        assert _label_map(m, "pi_llm_calls_total")[(("model", "x"), ("ok", "True"))] == 1

    def test_tracer_exception_hook_records_failure(self, tmp_path):
        m = Metrics(enabled=True)
        tracer = get_tracer("jsonl", path=tmp_path / "tr", metrics=m)
        with pytest.raises(RuntimeError):
            with tracer.track("llm.call", {"model": "x"}):
                raise RuntimeError("boom")
        assert _label_map(m, "pi_llm_calls_total")[(("model", "x"), ("ok", "False"))] == 1


class TestRunProjection:
    def test_run_finished_counts_status_model_tokens(self):
        m = Metrics(enabled=True)
        m.run_finished(
            status="ok", model="openai/qwen3.8-max", duration_s=4.2,
            turns=3, tokens_in=100, tokens_out=40,
        )
        m.run_finished(
            status="error", model="openai/qwen3.8-max", duration_s=0.1,
            turns=1, tokens_in=10, tokens_out=0,
        )
        runs = _label_map(m, "pi_runs_total")
        assert runs[(("model", "openai/qwen3.8-max"), ("status", "ok"))] == 1
        assert runs[(("model", "openai/qwen3.8-max"), ("status", "error"))] == 1
        tokens = _label_map(m, "pi_tokens_total")
        assert tokens[(("direction", "input"), ("model", "openai/qwen3.8-max"))] == 110
        assert tokens[(("direction", "output"), ("model", "openai/qwen3.8-max"))] == 40

    def test_tool_call_counts_denied_as_failure(self):
        m = Metrics(enabled=True)
        m.tool_call(tool="read", ok=True, duration_s=0.1)
        m.tool_call(tool="web_fetch", ok=False, duration_s=0.01)  # denied lands here
        calls = _label_map(m, "pi_tool_calls_total")
        assert calls[(("ok", "False"), ("tool", "web_fetch"))] == 1
        assert calls[(("ok", "True"), ("tool", "read"))] == 1

    def test_in_flight_gauge_up_and_down(self):
        m = Metrics(enabled=True)

        async def main():
            async with m.in_flight():
                g = _label_map(m, "pi_runs_in_flight")
                assert g[()] == 1.0
            assert _label_map(m, "pi_runs_in_flight")[()] == 0.0
            with pytest.raises(RuntimeError):
                async with m.in_flight():
                    raise RuntimeError("x")
            assert _label_map(m, "pi_runs_in_flight")[()] == 0.0  # exception path too

        asyncio.run(main())


class TestDegradationCounters:
    def test_retrieval_outcomes(self):
        m = Metrics(enabled=True)
        m.retrieval(outcome="vector_hit", duration_s=0.2)
        m.retrieval(outcome="lexical_fallback", duration_s=0.3)
        m.retrieval(outcome="no_hits", duration_s=0.1)
        m.retrieval(outcome="embed_failed", duration_s=0.01)
        r = _label_map(m, "pi_memory_retrievals_total")
        assert {(k[0][1]): v for k, v in r.items()} == {
            "vector_hit": 1, "lexical_fallback": 1, "no_hits": 1, "embed_failed": 1,
        }

    def test_fallback_counter(self):
        m = Metrics(enabled=True)
        m.fallback(from_model="openai/qwen3.8-max", to_model="openai/qwen3.8-flash")
        f = _label_map(m, "pi_llm_fallbacks_total")
        assert f[(("from", "openai/qwen3.8-max"), ("to", "openai/qwen3.8-flash"))] == 1


class TestLabelDiscipline:
    def test_no_identity_labels(self):
        m = Metrics(enabled=True)
        m.observe_span("llm.call", _Span(model="m", ok=True), 0.1)
        m.run_finished(status="ok", model="m", duration_s=1.0, turns=1, tokens_in=1, tokens_out=1)
        m.tool_call(tool="bash", ok=True, duration_s=0.1)
        m.retrieval(outcome="vector_hit", duration_s=0.1)
        m.http_request(method="POST", route="/v1/sessions/{session_id}/runs", status=200, duration_s=0.1)
        forbidden = {"session", "username", "prompt", "user", "task_id", "session_id"}
        keys = set()
        for metric in m.registry.collect():
            for s in metric.samples:
                keys.update(k for k, _ in s.labels.items())
        assert not (keys & forbidden), keys


class TestDisabled:
    def test_disabled_is_silent_noop(self):
        m = Metrics(enabled=False)
        assert m.render() is None
        m.observe_span("llm.call", _Span(model="m", ok=True), 0.1)
        m.run_finished(status="ok", model="m", duration_s=1.0, turns=1, tokens_in=1, tokens_out=1)
        m.tool_call(tool="bash", ok=True, duration_s=0.1)
        m.retrieval(outcome="no_hits", duration_s=0.1)
        m.fallback(from_model="a", to_model="b")
        m.http_request(method="GET", route="/healthz", status=200, duration_s=0.1)

        async def main():
            async with m.in_flight():
                pass

        asyncio.run(main())


class TestHttpMiddlewareAndEndpoint:
    def _app(self, tmp_path, monkeypatch, **env):
        from fastapi.testclient import TestClient

        from pi.server.app import create_app
        from pi.server.config import ServerSettings

        monkeypatch.setenv("PI_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
        monkeypatch.setenv("PI_MODEL", "fake/demo")
        monkeypatch.setenv("PI_JWT_SECRET", "test-secret-key-0123456789abcdef")
        monkeypatch.setenv("PI_METRICS_TOKEN", "")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return TestClient(create_app(ServerSettings.from_env()))

    def test_http_counter_uses_route_pattern(self, tmp_path, monkeypatch):
        with self._app(tmp_path, monkeypatch) as client:
            client.get("/healthz")
            m = client.app.state.metrics
            r = _label_map(m, "pi_http_requests_total")
            assert r[(("method", "GET"), ("route", "/healthz"), ("status", "200"))] == 1
            client.get("/nonexistent-path-xyz")
            r = _label_map(m, "pi_http_requests_total")  # fresh snapshot
            assert r[(("method", "GET"), ("route", "unmatched"), ("status", "404"))] == 1

    def test_metrics_gate(self, tmp_path, monkeypatch):
        with self._app(tmp_path, monkeypatch, PI_METRICS_TOKEN="secret-token") as client:
            r = client.get("/metrics")  # no token
            assert r.status_code == 404
            r = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 404
            r = client.get("/metrics", headers={"Authorization": "Bearer secret-token"})
            assert r.status_code == 200
            assert "pi_runs_total" in r.text

    def test_metrics_off_answers_503(self, tmp_path, monkeypatch):
        with self._app(tmp_path, monkeypatch, PI_METRICS="0") as client:
            r = client.get("/metrics")
            assert r.status_code == 503
            assert "metrics are off" in r.text
