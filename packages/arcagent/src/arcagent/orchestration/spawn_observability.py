"""Operational lineage, tracing, and compliance events for child runs."""

from __future__ import annotations

import logging
from typing import Any

import arcrun
import arctrust
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record

_logger = logging.getLogger("arcagent.orchestration.spawn")


def parent_did(state: arcrun.ParentRunContext) -> str | None:
    """Return the operational actor recording the parent, when enabled."""
    return state.event_bus.spool_actor_did


def child_label(child_did: str, role: str | None, depth: int) -> str:
    """Build the stable human-readable child label used by telemetry."""
    suffix = child_did.rsplit("/", 1)[-1]
    return f"{role or 'child'}:{suffix}:d{depth}"


def spool_spawn_event(
    *, parent_did: str, child_did: str, role: str | None, depth: int, outcome: str
) -> None:
    """Record the operational parent-child lineage edge."""
    spool_record(
        SpoolRecord(
            kind="spawn_event",
            actor_did=child_did,
            parent_did=parent_did,
            child_did=child_did,
            role=role,
            depth=depth,
            outcome=outcome,
        )
    )


def get_otel_context() -> Any | None:
    """Return the current OpenTelemetry context when available."""
    try:
        from opentelemetry import context as otel_context

        return otel_context.get_current()
    except ImportError:
        return None


def start_child_span(
    name: str, parent_context: Any | None, *, delegation_depth: int
) -> tuple[Any | None, Any | None]:
    """Start an optional child span and return it with its context token."""
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        tracer = trace.get_tracer("arcagent.orchestration.spawn")
        ctx = parent_context if parent_context is not None else otel_context.get_current()
        span = tracer.start_span(name, context=ctx)
        span.set_attribute("arc.delegation.depth", delegation_depth)
        token = otel_context.attach(trace.set_span_in_context(span))
        return span, token
    except Exception:  # reason: optional integration must degrade gracefully
        return None, None


def end_child_span(span: Any | None, token: Any | None, status: str) -> None:
    """Finish an optional child span without affecting child execution."""
    if span is None:
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.trace import Status, StatusCode

        if status not in ("completed", "max_iterations"):
            span.set_status(Status(StatusCode.ERROR, status))
        span.end()
        if token is not None:
            otel_context.detach(token)
    except Exception:  # reason: optional integration must never break execution
        _logger.debug("OTel span end failed (non-fatal)", exc_info=True)


def emit_spawn_audit(
    *,
    action: str,
    child_run_id: str,
    child_did: str,
    parent_run_id: str,
    outcome: str,
    extra: dict[str, Any] | None = None,
    sink: Any | None,
) -> None:
    """Emit a compliance event, swallowing sink failures per NIST AU-5."""
    if sink is None:
        return
    try:
        event = arctrust.AuditEvent(
            actor_did=child_did,
            action=action,
            target=child_did,
            outcome=outcome,
            extra={
                "child_run_id": child_run_id,
                "parent_run_id": parent_run_id,
                **(extra or {}),
            },
        )
        arctrust.emit(event, sink)
    except Exception:  # reason: auditing cannot break child execution
        _logger.warning(
            "Failed to emit AuditEvent action=%s child_run_id=%s — swallowing (AU-5)",
            action,
            child_run_id,
            exc_info=True,
        )


__all__ = [
    "child_label",
    "emit_spawn_audit",
    "end_child_span",
    "get_otel_context",
    "parent_did",
    "spool_spawn_event",
    "start_child_span",
]
