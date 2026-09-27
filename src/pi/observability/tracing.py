"""Tracing: unified span API with three backends.

- NoOpTracer   : default, zero overhead
- JsonlTracer  : built-in, writes span trees to a JSONL file (no extra deps)
- OtelTracer   : bridges to OpenTelemetry when opentelemetry-sdk is installed

AgentLoop and the server embed spans: agent.run -> llm.call / tool.call.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, ok: bool, description: str = "") -> None:
        pass


class JsonlSpan:
    """Dict-backed span for the JSONL tracer."""

    def __init__(self, name: str, attributes: dict[str, Any]):
        self._data: dict[str, Any] = {"name": name, "ok": True, "error": ""}
        self._data.update(attributes or {})

    def set_attribute(self, key: str, value: Any) -> None:
        self._data[key] = value

    def set_status(self, ok: bool, description: str = "") -> None:
        self._data["ok"] = ok
        self._data["error"] = "" if ok else description


class Tracer:
    """Base tracer; subclasses implement span() and end_span()."""

    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        return NoOpSpan()

    @contextmanager
    def track(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        span = self.span(name, attributes)
        start = time.perf_counter()
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            span.set_status(False, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.end_span(name, span, attributes, time.perf_counter() - start)

    def end_span(self, name: str, span: Any, attributes: dict[str, Any] | None, duration_s: float) -> None:
        pass


class NoOpTracer(Tracer):
    pass


class JsonlTracer(Tracer):
    """Writes one JSONL line per span to a trace file (append-only)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else Path.home() / ".pi-py" / "traces.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._trace_id = uuid.uuid4().hex[:16]

    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        return JsonlSpan(name, attributes or {})

    def end_span(self, name: str, span: Any, attributes: dict[str, Any] | None, duration_s: float) -> None:
        data = span._data if isinstance(span, JsonlSpan) else {"name": name}
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": self._trace_id,
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
    """Bridges to OpenTelemetry; requires opentelemetry-sdk."""

    def __init__(self, service_name: str = "pi-py"):
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("pi-py")

    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        return self._tracer.start_span(name, attributes=attributes or {})

    def end_span(self, name: str, span: Any, attributes: dict[str, Any] | None, duration_s: float) -> None:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        span.set_attribute("duration_ms", round(duration_s * 1000, 1))
        span.end()
        # flush is handled by the provider's processor at shutdown


def get_tracer(backend: str = "noop", **kwargs) -> Tracer:
    """backend: 'noop' | 'jsonl' | 'otel' (falls back to jsonl if otel missing)."""
    if backend == "jsonl":
        return JsonlTracer(kwargs.get("path"))
    if backend == "otel":
        try:
            return OtelTracer(kwargs.get("service_name", "pi-py"))
        except ImportError:
            return JsonlTracer(kwargs.get("path"))
    return NoOpTracer()
