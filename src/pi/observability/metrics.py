"""Prometheus metrics, exposed by the server at /metrics.

Spans answer "what happened in this run"; metrics answer "is it happening to
everyone right now". They are separate on purpose: a trace is per-run, sampled and
stored for 30 days, while a metric is aggregated, always on, and the only thing an
alert can fire on. Neither substitutes for the other - a Jaeger full of healthy
runs says nothing about the run nobody can start.

Recorded from TraceRecorder, which already sees every event of every run and
already computes the durations. One place, so the numbers cannot drift apart from
the traces they describe.

Labels are bounded by design: model, tool, status, outcome. Nothing per-user,
per-session or per-prompt - those are what agent_runs is for, and a label that
grows with the user count turns a metrics backend into an outage of its own.

prometheus-client is optional (pi-py[observability], included in production).
Without it every method is a no-op and /metrics answers 503 with the reason, so a
dev install still runs.
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

#: Seconds. A run is dominated by model latency, so its buckets have to span "one
#: fast answer" and "the run timeout" or everything lands in one bar.
_RUN_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
#: Seconds. One model round-trip, one tool call, one retrieval: all of these are
#: usually well under a second and occasionally minutes.
_CALL_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
#: Fact counts, so small they need their own scale.
_COUNT_BUCKETS = (0, 1, 2, 3, 5, 8, 13, 21, 40)


class Metrics:
    """The server's counters and histograms.

    Constructed once per app and handed to RunManager. When prometheus-client is
    missing - or metrics are switched off - every method returns immediately and
    render() answers None, which is what makes /metrics a 503 rather than a 500.
    """

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
            "Agent runs finished, by final status. `model` is the model the run "
            "ASKED for (PI_MODEL or the run's own), which under a fallback chain is "
            "not necessarily what answered - pi_llm_calls_total carries that.",
            ("status", "model"),
            registry=registry,
        )
        # The semaphore's own limit is PI_MAX_CONCURRENT_RUNS; this is how close to
        # it the process actually runs, which is the number that says "scale out".
        self.runs_in_flight = Gauge(
            "pi_runs_in_flight", "Runs currently executing in this process.", registry=registry
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
            "Model round-trips, by the provider that actually served them and "
            "whether they completed. Under PI_FALLBACK_CHAIN this `model` differs "
            "from the run's requested one, and the difference is the point: it is "
            "how a silent fallback shows up on a dashboard.",
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
            "Tool executions, by tool and outcome.",
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
        # outcome is MemoryService's verdict (injected, no_hits, gated_out,
        # rerank_empty, embed_failed, ...): the rate of `injected` falling while
        # runs hold steady is memory quietly going dark.
        self.retrievals = Counter(
            "pi_memory_retrievals_total",
            "Memory retrievals before a run, by verdict.",
            ("outcome",),
            registry=registry,
        )
        self.retrieval_duration = Histogram(
            "pi_memory_retrieve_duration_seconds",
            "Embedding + vector search + repo join + rerank, on the hot path.",
            buckets=_CALL_BUCKETS,
            registry=registry,
        )
        self.facts_kept = Histogram(
            "pi_memory_facts_kept",
            "Facts selected for injection per retrieval.",
            buckets=_COUNT_BUCKETS,
            registry=registry,
        )
        # A recall that came from brute-force cosine over MySQL instead of the
        # vector index. Silent by design at the time it happens, so this is the
        # only thing that says the index is down or its breaker is open.
        self.index_fallbacks = Counter(
            "pi_memory_index_fallbacks_total",
            "Retrievals served without the vector index.",
            ("path",),
            registry=registry,
        )
        # Tracing is wrapped in "must never fail a run", so a broken trace write is
        # invisible everywhere else. This counts the blindness.
        self.trace_failures = Counter(
            "pi_trace_failures_total",
            "Runs whose agent_runs/agent_steps write failed.",
            registry=registry,
        )

    # ------------------------------------------------------------------ recording

    @asynccontextmanager
    async def in_flight(self) -> AsyncIterator[None]:
        """Hold pi_runs_in_flight for the length of one run.

        A context manager rather than a started()/finished() pair: the gauge has to
        come back down on the paths where a run raises, and a manual dec is exactly
        the kind of line that gets skipped. A gauge that only ever climbs produces
        the most convincing false saturation alert there is.

        Async because it is entered alongside the run semaphore in one `async with`,
        and that statement needs every item in it to be an async manager.
        """
        if self.registry is None:
            yield
            return
        with self.runs_in_flight.track_inprogress():
            yield

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

    def llm_call(self, *, model: str, ok: bool, duration_s: float) -> None:
        """One model round-trip. Tokens are counted once per run from its total:
        summing the per-turn numbers would double-count every turn after the first,
        since each turn re-sends the whole history."""
        if self.registry is None:
            return
        self.llm_calls.labels(model=model, ok=str(ok)).inc()
        self.llm_duration.labels(model=model).observe(duration_s)

    def tool_call(self, *, tool: str, ok: bool, duration_s: float) -> None:
        if self.registry is None:
            return
        self.tool_calls.labels(tool=tool, ok=str(ok)).inc()
        self.tool_duration.labels(tool=tool).observe(duration_s)

    def retrieval(
        self, *, outcome: str, duration_s: float, kept: int, index_path: str = ""
    ) -> None:
        if self.registry is None:
            return
        self.retrievals.labels(outcome=outcome).inc()
        self.retrieval_duration.observe(duration_s)
        self.facts_kept.observe(kept)
        # Only the degraded paths are counted: 'index' is the healthy case, and a
        # counter that ticks on every run is a counter nobody reads.
        if index_path and index_path != "index":
            self.index_fallbacks.labels(path=index_path).inc()

    def trace_write_failed(self) -> None:
        if self.registry is not None:
            self.trace_failures.inc()

    # ------------------------------------------------------------------ exposition

    def render(self) -> tuple[bytes, str] | None:
        """(payload, content type), or None when metrics are off or unavailable."""
        if self.registry is None:
            return None
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
