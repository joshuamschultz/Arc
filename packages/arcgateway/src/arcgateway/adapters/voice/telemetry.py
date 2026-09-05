"""Voice telemetry — OTEL spans for a turn (SPEC-077 COMP-016, REQ-021).

A thin wrapper so the adapter can span the STT and TTS steps without a hard
OpenTelemetry dependency: if OTEL is present the span is real, otherwise it is a
no-op context manager. Attributes carry non-sensitive metadata only (ids, counts —
never transcript content).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def _null_span() -> Iterator[None]:
    yield None


def voice_span(name: str, **attributes: Any) -> Any:
    """A current span named ``name``; a no-op when OpenTelemetry is unavailable."""
    try:
        from opentelemetry import trace
    except ImportError:
        return _null_span()
    tracer = trace.get_tracer("arcgateway.voice")
    return tracer.start_as_current_span(name, attributes=attributes)


__all__ = ["voice_span"]
