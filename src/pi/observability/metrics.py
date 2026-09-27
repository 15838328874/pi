"""Prometheus metrics for the server (pull model, exposed at /metrics).

Design (bystander-architect): every metric family projects from ONE existing
choke point - no new instrumentation in the hot paths:
  - llm.call          <- the Tracer.track() finally hook (real-time, includes
                          mid-stream failures, which the trajectory misses)
  - tool.call         <- the run-end trajectory projection in RunManager
                          (covers denied/unknown/invalid-args calls, which the
                          tool.call span misses)
  - run-level         <- RunManager (TurnEndEvent / ErrorEvent / timeout)
  - memory retrieval  <- MemoryRepo's on_retrieval callback seam
  - HTTP              <- the existing request middleware (TTFB semantics)
  - model fallback    <- FallbackProvider's on_fallback callback

Labels are bounded by design: model / tool / status / outcome / method /
route. Never username, session, prompt or task id - a label that grows with
the user count turns a metrics backend into an outage of its own.

prometheus-client is optional: without it every method is a no-op and
render() answers None, which makes /metrics a 503 with the reason instead of
a 500, so a dev install still runs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger("pi.observability")

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _AVAILABLE = True
except ImportError:  # a dev install without the observability extra
    _AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

# Seconds. A run is dominated by model latency: buckets span "one fast answer"
# to "the run timeout" or everything lands in one bar.
_RUN_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
# Seconds. One model round-trip or tool call: usually well under a second.
_CALL_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
# Fact/turn counts, small enough to need their own scale.
_COUNT_BUCKETS = (0, 1, 2, 3, 5, 8, 13, 21, 40)


class Metrics:
    """The server's counters and histograms; every method is safe to call
    when disabled (no-op), so callers never branch on availability."""

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled and _AVAILABLE)
        self.registry: Any = None
        if not self.enabled:
            if enabled and not _AVAILABLE:
                log.warning(
                    "PI_METRICS is on but prometheus-client is not installed "
                    "(pip install 'pi-py[observability]'); /metrics will answer 503"
                )
            return

        registry = CollectorRegistry()
        self.registry = registry
        self.runs = Counter(
            "pi_runs_total",
            "Agent runs finished, by final status and the model the run asked for.",
            ("status", "model"),
            registry=registry,
        )
        self.runs_in_flight = Gauge(
            "pi_runs_in_flight",
            "Runs currently executing in this process (held by a context manager "
            "so exception paths bring it back down).",
            registry=registry,
        )
        self.run_duration = Histogram(
            "pi_run_duration_seconds",
            "Wall time of a whole run, including its tool calls.",
            ("status",),
            buckets=_RUN_BUCKETS,
            registry=registry,
        )
        self.run_turns = Histogram(
            "pi_run_turns",
            "Model round-trips per run. A high count with a small answer is a loop.",
            ("model",),
            buckets=_COUNT_BUCKETS,
            registry=registry,
        )
        self.tokens = Counter(
            "pi_tokens_total",
            "Tokens billed, by model and direction.",
            ("model", "direction"),
            registry=registry,
        )
        self.llm_calls = Counter(
            "pi_llm_calls_total",
            "Model round-trips by the model that served them and whether they "
            "completed; mid-stream failures land here with ok=false.",
            ("model", "ok"),
            registry=registry,
        )
        self.llm_duration = Histogram(
            "pi_llm_call_duration_seconds",
            "One model round-trip. Separates a slow provider from a slow tool.",
            ("model",),
            buckets=_CALL_BUCKETS,
            registry=registry,
        )
        self.tool_calls = Counter(
            "pi_tool_calls_total",
            "Tool executions by tool and outcome; denied / unknown / invalid-args "
            "calls are included (they only exist in the trajectory).",
            ("tool", "ok"),
            registry=registry,
        )
        self.tool_duration = Histogram(
            "pi_tool_call_duration_seconds",
            "One tool execution.",
            ("tool",),
            buckets=_CALL_BUCKETS,
            registry=registry,
        )
        self.retrievals = Counter(
            "pi_memory_retrievals_total",
            "Memory retrievals by outcome: vector_hit / lexical_fallback / "
            "no_hits / embed_failed. lexical_fallback ticking while runs hold "
            "steady is memory quietly going dark.",
            ("outcome",),
            registry=registry,
        )
        self.retrieval_duration = Histogram(
            "pi_memory_retrieve_duration_seconds",
            "Embedding + vector search on the turn-start hot path.",
            buckets=_CALL_BUCKETS,
            registry=registry,
        )
        self.fallbacks = Counter(
            "pi_llm_fallbacks_total",
            "Model fallback-chain degradations; the only thing that makes a "
            "silent fallback show up on a dashboard.",
            ("from", "to"),
            registry=registry,
        )
        self.http_requests = Counter(
            "pi_http_requests_total",
            "HTTP requests by method, route pattern and status.",
            ("method", "route", "status"),
            registry=registry,
        )
        self.http_duration = Histogram(
            "pi_http_request_duration_seconds",
            "Time to response headers (TTFB semantics: an SSE run streams after "
            "this point, so this is NOT the run duration).",
            ("method", "route"),
            buckets=_CALL_BUCKETS,
            registry=registry,
        )

    # ------------------------------------------------------------------ recording

    @asynccontextmanager
    async def in_flight(self) -> AsyncIterator[None]:
        """Hold pi_runs_in_flight for the length of one run. A context manager
        rather than a started()/finished() pair so the gauge comes back down on
        exception paths too."""
        if self.registry is None:
            yield
            return
        with self.runs_in_flight.track_inprogress():
            yield

    def observe_span(self, name: str, span: Any, duration_s: float) -> None:
        """Project one closed tracer span (called from Tracer.track's finally).

        Only llm.call feeds metrics here - tool calls come from the trajectory
        (the span misses denied/unknown paths) and runs from RunManager (the
        span has no token/turn totals). One family, one source."""
        if self.registry is None or name != "llm.call":
            return
        data = getattr(span, "_data", None) or dict(getattr(span, "attributes", {}))
        model = str(data.get("model", "?"))
        ok = bool(data.get("ok", True))
        self.llm_calls.labels(model=model, ok=str(ok)).inc()
        self.llm_duration.labels(model=model).observe(duration_s)

    def run_finished(
        self,
        *,
        status: str,
        model: str,
        duration_s: float,
        turns: int,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        if self.registry is None:
            return
        self.runs.labels(status=status, model=model).inc()
        self.run_duration.labels(status=status).observe(duration_s)
        self.run_turns.labels(model=model).observe(turns)
        self.tokens.labels(model=model, direction="input").inc(tokens_in)
        self.tokens.labels(model=model, direction="output").inc(tokens_out)

    def tool_call(self, *, tool: str, ok: bool, duration_s: float) -> None:
        if self.registry is None:
            return
        self.tool_calls.labels(tool=tool, ok=str(ok)).inc()
        self.tool_duration.labels(tool=tool).observe(duration_s)

    def retrieval(self, *, outcome: str, duration_s: float) -> None:
        if self.registry is None:
            return
        self.retrievals.labels(outcome=outcome).inc()
        self.retrieval_duration.observe(duration_s)

    def fallback(self, *, from_model: str, to_model: str) -> None:
        if self.registry is None:
            return
        self.fallbacks.labels(**{"from": from_model, "to": to_model}).inc()

    def http_request(self, *, method: str, route: str, status: int, duration_s: float) -> None:
        if self.registry is None:
            return
        self.http_requests.labels(method=method, route=route, status=str(status)).inc()
        self.http_duration.labels(method=method, route=route).observe(duration_s)

    # ------------------------------------------------------------------ exposition

    def render(self) -> tuple[bytes, str] | None:
        """(payload, content type), or None when metrics are off/unavailable."""
        if self.registry is None:
            return None
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
