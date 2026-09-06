"""Tracing: unified span API with three backends.

- NoOpTracer   : default, zero overhead
- JsonlTracer  : built-in, writes span trees to a JSONL file (no extra deps)
- OtelTracer   : exports over OTLP/gRPC to any collector (Jaeger, Tempo, an
                 OTel Collector) when opentelemetry-sdk and an OTLP exporter
                 are installed

AgentLoop and the server embed spans: agent.run -> llm.call / tool.call /
memory.retrieve -> memory.embed / memory.recall / memory.join / memory.rerank.

Every backend nests. track() pushes its span onto a ContextVar stack and hands
the innermost one to span() as the parent, so one run renders as a tree with a
single trace_id instead of a flat list of siblings that cannot be told apart.
A ContextVar because a run streams inside one asyncio task: an attribute on the
tracer would leak spans across concurrent requests.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("pi.observability")

#: Innermost active span per task. Roots (an empty stack) start a new trace.
_active_spans: ContextVar[tuple[Any, ...]] = ContextVar("pi_active_spans", default=())


class NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, ok: bool, description: str = "") -> None:
        pass


class JsonlSpan:
    """Dict-backed span for the JSONL tracer."""

    def __init__(
        self,
        name: str,
        attributes: dict[str, Any],
        trace_id: str = "",
        span_id: str = "",
        parent_span_id: str = "",
    ):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self._data: dict[str, Any] = {"name": name, "ok": True, "error": ""}
        self._data.update(attributes or {})

    def set_attribute(self, key: str, value: Any) -> None:
        self._data[key] = value

    def set_status(self, ok: bool, description: str = "") -> None:
        self._data["ok"] = ok
        self._data["error"] = "" if ok else description


class OtelSpan:
    """Adapts an OTel span to this module's set_status(ok, description) contract.

    OTel's own set_status takes a StatusCode or Status, and silently drops
    anything else with a warning - a bool would lose exactly the error path,
    which is the only reason a span carries a status at all.
    """

    def __init__(self, inner: Any):
        self.inner = inner

    def set_attribute(self, key: str, value: Any) -> None:
        self.inner.set_attribute(key, value)

    def set_status(self, ok: bool, description: str = "") -> None:
        from opentelemetry.trace import StatusCode

        self.inner.set_status(StatusCode.OK if ok else StatusCode.ERROR, description)

    def record_exception(self, exc: BaseException) -> None:
        self.inner.record_exception(exc)


class Tracer:
    """Base tracer; subclasses implement span() and end_span()."""

    def span(
        self, name: str, attributes: dict[str, Any] | None = None, parent: Any = None
    ) -> Any:
        return NoOpSpan()

    def current(self) -> Any:
        """The innermost open span in this task, or None at a root."""
        stack = _active_spans.get()
        return stack[-1] if stack else None

    @contextmanager
    def track(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        span = self.span(name, attributes, self.current())
        token = _active_spans.set((*_active_spans.get(), span))
        start = time.perf_counter()
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            span.set_status(False, f"{type(exc).__name__}: {exc}")
            record = getattr(span, "record_exception", None)
            if record is not None:
                record(exc)
            raise
        finally:
            _active_spans.reset(token)
            self.end_span(name, span, attributes, time.perf_counter() - start)

    def end_span(
        self, name: str, span: Any, attributes: dict[str, Any] | None, duration_s: float
    ) -> None:
        pass

    def shutdown(self) -> None:
        """Flush buffered spans. Called from the server's lifespan shutdown."""


class NoOpTracer(Tracer):
    pass


class JsonlTracer(Tracer):
    """Writes one JSONL line per span to a trace file (append-only).

    trace_id is per *root span*, not per process: a process-level id made every
    run since boot look like one trace, so the file could not answer "what did
    this run do". A span with no parent starts a new trace and its children
    inherit it, which is what makes the day's file replayable as trees.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path.home() / ".pi-py" / "traces.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def span(
        self, name: str, attributes: dict[str, Any] | None = None, parent: Any = None
    ) -> Any:
        if isinstance(parent, JsonlSpan):
            trace_id, parent_span_id = parent.trace_id, parent.span_id
        else:
            trace_id, parent_span_id = uuid.uuid4().hex[:16], ""
        return JsonlSpan(
            name,
            attributes or {},
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent_span_id,
        )

    def end_span(
        self, name: str, span: Any, attributes: dict[str, Any] | None, duration_s: float
    ) -> None:
        data = span._data if isinstance(span, JsonlSpan) else {"name": name}
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": getattr(span, "trace_id", ""),
            "span_id": getattr(span, "span_id", ""),
            # "" on a root: the line is a tree's top, not an orphan.
            "parent_span_id": getattr(span, "parent_span_id", ""),
            "span": name,
            "duration_ms": round(duration_s * 1000, 1),
            **data,
            **(attributes or {}),
        }
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = self.path.with_name(f"{self.path.stem}-{day}{self.path.suffix}")
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


class OtelTracer(Tracer):
    """Bridges to OpenTelemetry and exports over OTLP/gRPC.

    A TracerProvider with no processor drops every span on the floor, which is
    what this class used to do: spans were created, timed and ended, and never
    left the process. BatchSpanProcessor plus OTLPSpanExporter is the half that
    actually reaches a collector, so the endpoint is the one setting that must
    be right.

    The provider is held per instance and never registered globally:
    trace.set_tracer_provider refuses to override, so a second create_app in one
    process (every test, every reload) would keep exporting to the first
    provider and its shutdown would flush the wrong one.
    """

    def __init__(
        self,
        service_name: str = "pi-py",
        endpoint: str = "",
        environment: str = "",
        sample_rate: float = 1.0,
        exporter: Any = None,
    ):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        self.endpoint = (
            endpoint
            or os.environ.get("PI_OTLP_ENDPOINT", "")
            or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            or "http://localhost:4317"
        )
        # gRPC to a collector on the same host or the same compose network is
        # plaintext; TLS is the exception and is spelled https://.
        resource_attrs = {"service.name": service_name}
        if environment:
            resource_attrs["deployment.environment"] = environment
        self._provider = TracerProvider(
            resource=Resource.create(resource_attrs),
            # ParentBased so a sampled-out run does not leave half a tree: the
            # decision is made once per trace at the root and inherited.
            sampler=ParentBased(TraceIdRatioBased(min(max(sample_rate, 0.0), 1.0))),
        )
        # `exporter` is a test seam: injecting an in-memory exporter keeps the
        # suite off the network instead of pointing OTLP at a port nothing
        # listens on and reading back the retry warnings.
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(
                endpoint=self.endpoint, insecure=self.endpoint.startswith("http://")
            )
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("pi-py")

    def span(
        self, name: str, attributes: dict[str, Any] | None = None, parent: Any = None
    ) -> Any:
        from opentelemetry import trace

        inner_parent = parent.inner if isinstance(parent, OtelSpan) else None
        context = trace.set_span_in_context(inner_parent) if inner_parent is not None else None
        return OtelSpan(self._tracer.start_span(name, attributes=attributes or {}, context=context))

    def end_span(
        self, name: str, span: Any, attributes: dict[str, Any] | None, duration_s: float
    ) -> None:
        inner = span.inner if isinstance(span, OtelSpan) else span
        if attributes:
            for k, v in attributes.items():
                inner.set_attribute(k, v)
        inner.set_attribute("duration_ms", round(duration_s * 1000, 1))
        inner.end()

    def shutdown(self) -> None:
        """Flush what BatchSpanProcessor is still holding. Never raises: this runs
        on the way out of lifespan, after the answer is already delivered."""
        try:
            self._provider.shutdown()
        except Exception:  # noqa: BLE001 - shutdown must not mask a real failure
            log.warning("OTel tracer shutdown failed", exc_info=True)


def get_tracer(backend: str = "noop", **kwargs) -> Tracer:
    """backend: 'noop' | 'jsonl' | 'otel' (falls back to jsonl if otel is missing).

    The fallback is loud on purpose. It used to be silent, which turns a missing
    extra into "we have no traces and no idea why" - the exact situation tracing
    exists to prevent.
    """
    if backend == "jsonl":
        return JsonlTracer(kwargs.get("path"))
    if backend == "otel":
        try:
            return OtelTracer(
                service_name=kwargs.get("service_name") or "pi-py",
                endpoint=kwargs.get("endpoint") or "",
                environment=kwargs.get("environment") or "",
                sample_rate=float(kwargs.get("sample_rate", 1.0) or 1.0),
                exporter=kwargs.get("exporter"),
            )
        except ImportError as exc:
            log.warning(
                "PI_TRACER=otel needs pi-py[observability] plus an OTLP exporter "
                "(%s: %s); falling back to jsonl",
                type(exc).__name__,
                exc,
            )
            return JsonlTracer(kwargs.get("path"))
    if backend != "noop":
        log.warning("unknown PI_TRACER=%r; tracing disabled", backend)
    return NoOpTracer()
