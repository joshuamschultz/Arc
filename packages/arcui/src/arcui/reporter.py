"""UIEventReporter — emits typed UIEvents for all four layers.

Provides a clean API for agent code to emit structured events to the arcui
pipeline without importing arcui internals directly. Each emit_* method
produces a fully-typed UIEvent on the appropriate layer.

Usage::

    from arcui.reporter import UIEventReporter
    from arcui.event_buffer import EventBuffer  # injected by server

    reporter = UIEventReporter(
        event_buffer=event_buffer,
        agent_id=agent.did,
        agent_name=agent.name,
    )
    reporter.emit_llm_trace(model="claude-sonnet-4-6", provider="anthropic", ...)

Layers
------
- ``llm``   — LLM call telemetry (TraceRecord fields)
- ``run``   — Agentic loop lifecycle (spawn start/complete, stream tokens)
- ``agent`` — Agent-level events (tool calls, skill/extension loads, memory writes)
- ``team``  — Team coordination (entity register, message routing)

Security
--------
- No secrets in event payloads (LLM01/LLM02).
- Sequence numbers are per-reporter, monotonically increasing.
- All events carry agent_id and agent_name for identity tracing (IA-2/IA-8).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from arcui.types import UIEvent

if TYPE_CHECKING:
    from arcui.event_buffer import EventBuffer


class UIEventReporter:
    """Emits UIEvents on all 4 layers to an EventBuffer.

    Thread-safe: sequence counter is protected by a lock.

    Args:
        event_buffer: The EventBuffer to push events into.
        agent_id: Stable identifier for this agent (DID or UUID string).
        agent_name: Human-readable name for UI display.
        source_id: Optional override for UIEvent.source_id (defaults to agent_id).
    """

    def __init__(
        self,
        event_buffer: EventBuffer,
        agent_id: str,
        agent_name: str,
        source_id: str | None = None,
    ) -> None:
        self._buffer = event_buffer
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._source_id = source_id or agent_id
        self._seq = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        with self._lock:
            seq = self._seq
            self._seq += 1
            return seq

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _emit(self, layer: str, event_type: str, data: dict[str, Any]) -> None:
        """Build and push a UIEvent. Called by all emit_* methods."""
        valid = ("llm", "run", "agent", "team")
        if layer not in valid:
            msg = f"Invalid layer: {layer!r}. Must be one of {valid}"
            raise ValueError(msg)
        ui_event = UIEvent(
            layer=layer,  # type: ignore[arg-type]
            event_type=event_type,
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            source_id=self._source_id,
            timestamp=self._now(),
            data=data,
            sequence=self._next_seq(),
        )
        self._buffer.push(ui_event)

    # ------------------------------------------------------------------
    # LLM layer
    # ------------------------------------------------------------------

    def emit_llm_trace(
        self,
        *,
        model: str,
        provider: str,
        duration_ms: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
        phase_timings: dict[str, float] | None = None,
        request_body: dict[str, Any] | None = None,
        response_body: dict[str, Any] | None = None,
        agent_did: str | None = None,
        agent_label: str | None = None,
        status: str = "success",
        stop_reason: str = "end_turn",
        trace_id: str | None = None,
        **extra: Any,
    ) -> None:
        """Emit an LLM call trace event on the 'llm' layer.

        Carries all user-required fields:
          model, provider, duration_ms, phase_timings, input_tokens,
          output_tokens, total_tokens, cost_usd, request_body (with messages
          and tools), response_body (with content, tool_calls, usage),
          agent_did, agent_label.

        Any extra kwargs are merged into the data payload.
        """
        data: dict[str, Any] = {
            "model": model,
            "provider": provider,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "phase_timings": phase_timings or {},
            "status": status,
            "stop_reason": stop_reason,
        }
        if request_body is not None:
            data["request_body"] = request_body
        if response_body is not None:
            data["response_body"] = response_body
        if agent_did is not None:
            data["agent_did"] = agent_did
        if agent_label is not None:
            data["agent_label"] = agent_label
        if trace_id is not None:
            data["trace_id"] = trace_id
        data.update(extra)
        self._emit("llm", "llm_trace", data)

    def emit_from_trace_record(self, record: Any) -> None:
        """Emit a UIEvent from an arcllm.TraceRecord object.

        Accepts any object with a `model_dump()` method (Pydantic BaseModel).
        All TraceRecord fields are forwarded to the llm layer payload without
        truncation.

        This is the recommended integration point for arcllm TelemetryModule
        callbacks — call this from the on_trace_complete hook.
        """
        data: dict[str, Any]
        if hasattr(record, "model_dump"):
            data = record.model_dump()
        else:
            # Fallback: treat as dict-like
            data = dict(record)

        # Build an llm_trace event directly from the full data dict.
        # We push directly rather than calling emit_llm_trace to avoid
        # losing any fields that emit_llm_trace doesn't explicitly handle.
        ui_event = UIEvent(
            layer="llm",
            event_type="llm_trace",
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            source_id=self._source_id,
            timestamp=self._now(),
            data=data,
            sequence=self._next_seq(),
        )
        self._buffer.push(ui_event)

    # ------------------------------------------------------------------
    # Run layer
    # ------------------------------------------------------------------

    def emit_run_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event on the 'run' layer.

        event_type must match ^[a-z_]+$ (e.g. 'spawn_start', 'spawn_complete',
        'stream_token', 'loop_step', 'tool_result').
        """
        self._emit("run", event_type, data)

    # ------------------------------------------------------------------
    # Agent layer
    # ------------------------------------------------------------------

    def emit_agent_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event on the 'agent' layer.

        event_type examples: 'tool_call', 'skill_load', 'extension_load',
        'memory_write', 'context_compact', 'policy_evaluate'.
        """
        self._emit("agent", event_type, data)

    # ------------------------------------------------------------------
    # Team layer
    # ------------------------------------------------------------------

    def emit_team_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event on the 'team' layer.

        event_type examples: 'entity_register', 'entity_unregister',
        'message_route', 'task_delegate', 'team_dissolve'.
        """
        self._emit("team", event_type, data)
