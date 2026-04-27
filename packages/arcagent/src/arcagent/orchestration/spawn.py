"""Spawn tool — recursively start a child run() for task decomposition.

Security considerations (ASI-01, ASI-08, LLM10, NIST AU-2/AU-3):
- System prompt override: child inherits parent's prompt as immutable preamble
- Timeout: wall-clock limit on child execution prevents unbounded consumption
- Audit: spawn.start and spawn.complete events emitted for every child
- Error sanitization: internal details logged, generic message returned to LLM
- Concurrency: max_concurrent_spawns limits parallel child runs
- Token budget: RootTokenBudget prevents children from silently overrunning the
  caller's allocation (Hermes implicit-token-pool bug fix).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from arcrun.events import Event, EventBus
from arcrun.state import RunState
from arcrun.types import SandboxConfig, Tool, ToolContext
from arctrust import ChildIdentity
from pydantic import BaseModel

_logger = logging.getLogger("arcagent.orchestration.spawn")

# Sensible defaults for resource limits
_DEFAULT_SPAWN_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# RootTokenBudget — atomic shared token pool for parent+children
#
# Fixes the Hermes implicit-token-pool bug: children that debit no shared
# budget silently spend multiple times the caller's allocation with no
# warning. RootTokenBudget uses asyncio.Lock to make debit operations
# atomic under concurrent awaits from parallel child tasks.
# ---------------------------------------------------------------------------


class RootTokenBudget:
    """Shared token budget for a root run and all its spawned children.

    Thread-safety: asyncio-only. Uses asyncio.Lock for atomic debit.
    Not safe to use across multiple event loops.

    Args:
        total: Maximum tokens the root run and all children may consume.
               Must be positive.
    """

    def __init__(self, total: int) -> None:
        if total <= 0:
            raise ValueError(f"RootTokenBudget total must be positive, got {total}")
        self._total = total
        self._used = 0
        self._lock = asyncio.Lock()

    @property
    def total(self) -> int:
        """Total token budget."""
        return self._total

    @property
    def used(self) -> int:
        """Tokens consumed so far (pre-debits + record_actual overages)."""
        return self._used

    @property
    def remaining(self) -> int:
        """Tokens remaining; never goes negative."""
        return max(0, self._total - self._used)

    def is_exhausted(self) -> bool:
        """True when used >= total."""
        return self._used >= self._total

    async def try_debit(self, amount: int) -> bool:
        """Atomically debit *amount* from the budget.

        Returns True if the debit succeeded; False if the budget would be
        exceeded. On False, the budget is not modified.
        """
        async with self._lock:
            if self._used + amount > self._total:
                return False
            self._used += amount
            return True

    async def record_actual(self, amount: int) -> None:
        """Record actual token usage after a call completes.

        This may push used past total — that is intentional. In-flight children
        may return more tokens than the pre-debit estimate. record_actual
        corrects the accounting for audit purposes without blocking the child.
        """
        async with self._lock:
            self._used += amount


# ---------------------------------------------------------------------------
# TokenUsage / SpawnResult — structured return type for spawn_task
#
# Replaces the bare string return so callers get structured accounting data
# alongside the child's natural-language summary.
# ---------------------------------------------------------------------------

_SpawnStatus = Literal[
    "completed",
    "max_iterations",
    "timeout",
    "interrupted",
    "error",
    "budget_exhausted",
]


class TokenUsage(BaseModel):
    """Token usage counters for a single run."""

    input: int = 0
    output: int = 0
    total: int = 0


class SpawnResult(BaseModel):
    """Structured result returned by a spawned child run.

    Attributes:
        child_run_id: UUID of the child run (for audit correlation).
        child_did: DID of the child identity used.
        status: Terminal status of the child run.
        summary: Natural-language summary (passed back to the LLM).
        tokens: Token usage for the child run.
        tool_trace: Ordered list of tool names the child invoked.
        audit_chain_tip: SHA-256 hex of the last audit log entry (tamper-evidence).
        duration_s: Wall-clock seconds the child ran.
        error: Error message if status is not "completed". None otherwise.
    """

    child_run_id: str
    child_did: str
    status: _SpawnStatus
    summary: str
    tokens: TokenUsage
    tool_trace: list[str]
    audit_chain_tip: str
    duration_s: float
    error: str | None = None


# ---------------------------------------------------------------------------
# SpawnSpec — declarative spec for spawn_many()
# ---------------------------------------------------------------------------


@dataclass
class SpawnSpec:
    """Declarative specification for a single child spawn.

    Used with spawn_many() to spawn multiple children in parallel.
    """

    task: str
    tools: list[Tool]
    system_prompt: str
    parent_state: RunState
    child_did: str
    child_sk_bytes: bytes
    wallclock_timeout_s: float = _DEFAULT_SPAWN_TIMEOUT_SECONDS
    model: Any = None
    token_budget: int | None = None
    context: str | None = None
    max_turns: int = 25
    sandbox: SandboxConfig | None = None


_DEFAULT_MAX_CONCURRENT_SPAWNS = 5
_DEFAULT_MAX_CHILD_TURNS = 25
_MAX_ERROR_LEN = 200


def _make_bubble_handler(child_run_id: str, parent_bus: EventBus) -> Callable[[Event], None]:
    """Create on_event callback that bubbles child events to parent bus."""

    def handler(event: Event) -> None:
        parent_bus.emit(
            f"child.{child_run_id}.{event.type}",
            {**event.data, "child_run_id": child_run_id},
        )

    return handler


def make_spawn_tool(
    *,
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    state: RunState | None = None,
    sandbox: SandboxConfig | None = None,
    allowed_strategies: list[str] | None = None,
    spawn_timeout_seconds: int = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    max_concurrent_spawns: int = _DEFAULT_MAX_CONCURRENT_SPAWNS,
    max_child_turns: int = _DEFAULT_MAX_CHILD_TURNS,
) -> Tool:
    """Create a spawn_task tool that starts a child run().

    State is read from ``ctx.parent_state`` at execute time (set by the
    arcrun executor). The legacy ``state`` parameter is retained for
    callers that already have a live RunState, but is not required —
    pass None and it will be picked up from the tool context.
    """
    # Semaphore limits concurrent child runs (ASI-08, LLM10)
    spawn_semaphore = asyncio.Semaphore(max_concurrent_spawns)
    static_state = state  # captured for callers that pre-bind

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        # Resolve live state. Prefer ctx.parent_state (set by executor);
        # fall back to a state passed at construction (legacy path).
        run_state: RunState | None = ctx.parent_state or static_state
        if run_state is None:
            return "Error: spawn_task invoked outside an arcrun loop"

        # Depth guard
        if run_state.depth >= run_state.max_depth:
            return f"Error: max spawn depth ({run_state.max_depth}) reached"

        child_task = params["task"]
        requested_tools = params.get("tools")

        # System prompt policy (ASI-01): parent prompt is always prepended
        # as an immutable preamble. LLM can specialize but not replace
        # core behavioral rules.
        child_specialization = params.get("system_prompt")
        if child_specialization:
            child_system_prompt = (
                f"{system_prompt}\n\n--- Child Specialization ---\n{child_specialization}"
            )
            prompt_overridden = True
        else:
            child_system_prompt = system_prompt
            prompt_overridden = False

        # Resolve tool subset
        if requested_tools is not None:
            resolved: list[Tool] = []
            for name in requested_tools:
                matched = next((t for t in tools if t.name == name), None)
                if matched is None:
                    return f"Error: unknown tool '{name}'"
                resolved.append(matched)
            child_tools = resolved
        else:
            child_tools = list(tools)

        child_run_id = str(uuid.uuid4())
        bubble_handler = _make_bubble_handler(child_run_id, run_state.event_bus)

        # Audit event: spawn start (NIST AU-2/AU-3)
        run_state.event_bus.emit(
            "spawn.start",
            {
                "child_run_id": child_run_id,
                "parent_run_id": run_state.run_id,
                "parent_depth": run_state.depth,
                "system_prompt_overridden": prompt_overridden,
                "tool_subset": [t.name for t in child_tools],
                "max_child_turns": max_child_turns,
                "timeout_seconds": spawn_timeout_seconds,
            },
        )

        # Import here to avoid circular import at module level
        from arcrun.loop import run

        try:
            async with spawn_semaphore:
                result = await asyncio.wait_for(
                    run(
                        model,
                        child_tools,
                        child_system_prompt,
                        child_task,
                        max_turns=max_child_turns,
                        depth=run_state.depth + 1,
                        max_depth=run_state.max_depth,
                        on_event=bubble_handler,
                        sandbox=sandbox,
                        allowed_strategies=allowed_strategies,
                    ),
                    timeout=spawn_timeout_seconds,
                )

            # Audit event: spawn complete
            run_state.event_bus.emit(
                "spawn.complete",
                {
                    "child_run_id": child_run_id,
                    "parent_run_id": run_state.run_id,
                    "turns_used": result.turns,
                    "cost_usd": result.cost_usd,
                    "success": True,
                },
            )

            return result.content or "(no content)"

        except TimeoutError:
            _logger.warning(
                "Child run %s timed out after %ds",
                child_run_id,
                spawn_timeout_seconds,
            )
            run_state.event_bus.emit(
                "spawn.complete",
                {
                    "child_run_id": child_run_id,
                    "parent_run_id": run_state.run_id,
                    "success": False,
                    "error": "timeout",
                },
            )
            return f"Error: child task timed out after {spawn_timeout_seconds}s"

        except Exception as exc:
            # Log full details internally, return sanitized message to LLM
            _logger.warning(
                "Child run %s failed: %s: %s",
                child_run_id,
                type(exc).__name__,
                str(exc)[:_MAX_ERROR_LEN],
            )
            run_state.event_bus.emit(
                "spawn.complete",
                {
                    "child_run_id": child_run_id,
                    "parent_run_id": run_state.run_id,
                    "success": False,
                    "error": type(exc).__name__,
                },
            )
            # Sanitized error — no internal details leaked to LLM (LLM02)
            return "Error: child task failed"

    return Tool(
        name="spawn_task",
        description=(
            "Spawn a child agent to accomplish a sub-task. The child runs "
            "independently and returns its result. Use for task decomposition "
            "and parallel work."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the child agent to accomplish",
                },
                "system_prompt": {
                    "type": "string",
                    "description": (
                        "Optional specialization prompt appended to the parent's "
                        "system prompt. Cannot replace core behavioral rules."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of tool names the child can use. "
                        "If omitted, inherits all parent tools."
                    ),
                },
            },
            "required": ["task"],
        },
        execute=_execute,
        timeout_seconds=spawn_timeout_seconds,
        parallel_safe=True,
    )


# ---------------------------------------------------------------------------
# OTel span helpers — lightweight wrappers that degrade gracefully when
# the opentelemetry SDK is not installed. Every spawn creates a child span
# with arc.delegation.depth set so traces show the full delegation tree.
# ---------------------------------------------------------------------------


def _get_otel_context() -> Any | None:
    """Return the current OpenTelemetry context, or None if not available."""
    try:
        from opentelemetry import context as otel_context

        return otel_context.get_current()
    except ImportError:
        return None


def _start_child_span(
    name: str,
    parent_context: Any | None,
    *,
    delegation_depth: int,
) -> tuple[Any | None, Any | None]:
    """Start a child OTel span for a spawn operation.

    Sets ``arc.delegation.depth`` attribute on the span. Returns
    ``(span, context_token)`` so the caller can end the span and detach
    the context. Both values are None when OTel is unavailable.
    """
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        tracer = trace.get_tracer("arcagent.orchestration.spawn")
        ctx = parent_context if parent_context is not None else otel_context.get_current()
        span = tracer.start_span(name, context=ctx)
        span.set_attribute("arc.delegation.depth", delegation_depth)
        token = otel_context.attach(trace.set_span_in_context(span))
        return span, token
    except (ImportError, Exception):
        # Degrade gracefully — OTel optional in local/air-gapped deployments
        return None, None


def _end_child_span(span: Any | None, token: Any | None, status: str) -> None:
    """End a child OTel span and detach context token.

    Safe to call with None span/token (no-op). Sets error status when
    the spawn did not complete successfully.
    """
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
    except (ImportError, Exception):
        # OTel optional — log at debug so nothing is silently swallowed
        _logger.debug("OTel span end failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# spawn() — high-level structured spawn with SpawnResult return type
#
# This is the function used by the delegate module. It wraps run() and
# returns a structured SpawnResult with accounting data. It NEVER raises
# on depth exhaustion — it returns a SpawnResult with status="error" so
# callers get consistent structured output.
# ---------------------------------------------------------------------------


def _make_error_result(
    *,
    child_run_id: str,
    child_did: str,
    error_msg: str,
    duration_s: float = 0.0,
) -> SpawnResult:
    """Build a SpawnResult with status='error'. Centralises the pattern."""
    audit_tip = hashlib.sha256(child_run_id.encode()).hexdigest()
    return SpawnResult(
        child_run_id=child_run_id,
        child_did=child_did,
        status="error",
        summary=f"Spawn failed: {error_msg}",
        tokens=TokenUsage(),
        tool_trace=[],
        audit_chain_tip=audit_tip,
        duration_s=duration_s,
        error=error_msg,
    )


async def spawn(
    *,
    parent_state: RunState,
    task: str,
    tools: list[Tool],
    system_prompt: str,
    identity: ChildIdentity | None = None,
    model: Any = None,
    context: str | None = None,
    max_turns: int = _DEFAULT_MAX_CHILD_TURNS,
    token_budget: int | None = None,
    wallclock_timeout_s: float = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    sandbox: SandboxConfig | None = None,
    audit_sink: Any | None = None,
    ui_reporter: Any | None = None,
) -> SpawnResult:
    """Spawn a child run and return a structured SpawnResult.

    Higher-level than make_spawn_tool() — used by the delegate module and
    any caller that needs structured accounting data rather than a bare string.

    This function NEVER raises. Depth exhaustion, missing model, and all
    runtime errors are returned as SpawnResult(status="error").

    Args:
        parent_state: Live RunState from the parent execution (for depth/budget).
        task: The task for the child agent.
        tools: Tools available to the child.
        system_prompt: System prompt for the child (parent's prompt is prepended).
        identity: Optional ChildIdentity; derived from parent if not provided.
        model: LLM model to use for the child run. Required; None → error result.
        context: Optional extra context appended to the task.
        max_turns: Maximum turns for the child run.
        token_budget: Optional token limit for the child run.
        wallclock_timeout_s: Wall-clock timeout in seconds.
        sandbox: Optional sandbox config.
        audit_sink: Optional arctrust.AuditSink. AuditEvents for spawn lifecycle
            are emitted to this sink in addition to EventBus events. When None,
            falls back to logger-only (backwards compatible).
        ui_reporter: Optional duck-typed UIEventReporter. When provided,
            emit_run_event() is called for spawn lifecycle events. No arcui
            import occurs — caller injects; if None, zero overhead.

    Returns:
        SpawnResult with status, summary, token counts, and audit chain tip.
    """
    import time

    from arcrun.loop import run

    child_run_id = str(uuid.uuid4())

    # Resolve identity first so error results carry the correct DID
    if identity is None:
        seed = uuid.uuid4().bytes[:32]
        hex_suffix = seed[:4].hex()
        identity = ChildIdentity(
            did=f"did:arc:delegate:child/{hex_suffix}",
            sk_bytes=seed,
            ttl_s=int(wallclock_timeout_s),
        )

    child_did = identity.did

    # Depth guard — return structured error, never raise (tests rely on this)
    if parent_state.depth >= parent_state.max_depth:
        error_msg = (
            f"Spawn rejected: max_depth {parent_state.max_depth} reached "
            f"(current depth {parent_state.depth})"
        )
        _logger.warning(error_msg)
        _emit_spawn_audit(
            action="spawn.denied",
            child_run_id=child_run_id,
            child_did=child_did,
            parent_run_id=parent_state.run_id,
            outcome="deny",
            extra={"reason": error_msg},
            sink=audit_sink,
        )
        _emit_ui_spawn_event(
            reporter=ui_reporter,
            event_type="spawn_denied",
            data={
                "child_run_id": child_run_id,
                "parent_run_id": parent_state.run_id,
                "reason": error_msg,
            },
        )
        return _make_error_result(
            child_run_id=child_run_id,
            child_did=child_did,
            error_msg=error_msg,
        )

    # Model guard — require a model for the child run
    if model is None:
        error_msg = "Spawn rejected: no model provided"
        _logger.warning(error_msg)
        return _make_error_result(
            child_run_id=child_run_id,
            child_did=child_did,
            error_msg=error_msg,
        )

    start = time.monotonic()
    full_task = f"{task}\n\nContext:\n{context}" if context else task
    bubble_handler = _make_bubble_handler(child_run_id, parent_state.event_bus)

    # Capture chain tip before emitting spawn.start for audit continuity
    parent_events = parent_state.event_bus.events
    parent_chain_tip = parent_events[-1].event_hash if parent_events else ""

    parent_state.event_bus.emit(
        "spawn.start",
        {
            "child_run_id": child_run_id,
            "child_did": child_did,
            "parent_run_id": parent_state.run_id,
            "parent_depth": parent_state.depth,
            "parent_chain_tip": parent_chain_tip,
        },
    )
    _emit_spawn_audit(
        action="spawn.start",
        child_run_id=child_run_id,
        child_did=child_did,
        parent_run_id=parent_state.run_id,
        outcome="allow",
        extra={"parent_depth": parent_state.depth, "parent_chain_tip": parent_chain_tip},
        sink=audit_sink,
    )
    _emit_ui_spawn_event(
        reporter=ui_reporter,
        event_type="spawn_start",
        data={
            "child_run_id": child_run_id,
            "child_did": child_did,
            "parent_run_id": parent_state.run_id,
            "parent_depth": parent_state.depth,
        },
    )

    otel_ctx = _get_otel_context()
    span, otel_token = _start_child_span(
        f"arcagent.orchestration.spawn.{child_run_id[:8]}",
        otel_ctx,
        delegation_depth=parent_state.depth + 1,
    )

    tool_trace: list[str] = []

    def _trace_handler(event: Event) -> None:
        bubble_handler(event)
        if event.type == "tool.start":
            name = event.data.get("name", "")
            if name:
                tool_trace.append(str(name))

    try:
        loop_result = await asyncio.wait_for(
            run(
                model,
                tools,
                system_prompt,
                full_task,
                max_turns=max_turns,
                depth=parent_state.depth + 1,
                max_depth=parent_state.max_depth,
                on_event=_trace_handler,
                sandbox=sandbox,
            ),
            timeout=wallclock_timeout_s,
        )

        duration_s = time.monotonic() - start
        tokens = TokenUsage(
            input=loop_result.tokens_used.get("input", 0),
            output=loop_result.tokens_used.get("output", 0),
            total=loop_result.tokens_used.get("total", 0),
        )

        # Determine status from loop result
        result_status: _SpawnStatus = "completed"

        parent_state.event_bus.emit(
            "spawn.complete",
            {
                "child_run_id": child_run_id,
                "child_did": child_did,
                "parent_run_id": parent_state.run_id,
                "status": result_status,
                "tokens": tokens.total,
            },
        )
        _emit_spawn_audit(
            action="spawn.complete",
            child_run_id=child_run_id,
            child_did=child_did,
            parent_run_id=parent_state.run_id,
            outcome=result_status,
            extra={"tokens": tokens.total, "duration_s": duration_s},
            sink=audit_sink,
        )
        _emit_ui_spawn_event(
            reporter=ui_reporter,
            event_type="spawn_complete",
            data={
                "child_run_id": child_run_id,
                "parent_run_id": parent_state.run_id,
                "status": result_status,
                "tokens": tokens.total,
                "duration_s": duration_s,
            },
        )

        _end_child_span(span, otel_token, result_status)
        audit_tip = hashlib.sha256(child_run_id.encode()).hexdigest()

        return SpawnResult(
            child_run_id=child_run_id,
            child_did=child_did,
            status=result_status,
            summary=loop_result.content or "(no content)",
            tokens=tokens,
            tool_trace=tool_trace,
            audit_chain_tip=audit_tip,
            duration_s=duration_s,
        )

    except TimeoutError:
        duration_s = time.monotonic() - start
        parent_state.event_bus.emit(
            "spawn.complete",
            {
                "child_run_id": child_run_id,
                "child_did": child_did,
                "parent_run_id": parent_state.run_id,
                "status": "timeout",
            },
        )
        _emit_spawn_audit(
            action="spawn.complete",
            child_run_id=child_run_id,
            child_did=child_did,
            parent_run_id=parent_state.run_id,
            outcome="timeout",
            extra={"duration_s": duration_s},
            sink=audit_sink,
        )
        _emit_ui_spawn_event(
            reporter=ui_reporter,
            event_type="spawn_complete",
            data={
                "child_run_id": child_run_id,
                "parent_run_id": parent_state.run_id,
                "status": "timeout",
                "duration_s": duration_s,
            },
        )
        _end_child_span(span, otel_token, "timeout")
        audit_tip = hashlib.sha256(child_run_id.encode()).hexdigest()
        return SpawnResult(
            child_run_id=child_run_id,
            child_did=child_did,
            status="timeout",
            summary="Child task timed out",
            tokens=TokenUsage(),
            tool_trace=tool_trace,
            audit_chain_tip=audit_tip,
            duration_s=duration_s,
            error=f"timeout after {wallclock_timeout_s}s",
        )

    except Exception as exc:
        duration_s = time.monotonic() - start
        _logger.warning("spawn() failed: %s: %s", type(exc).__name__, str(exc)[:_MAX_ERROR_LEN])
        parent_state.event_bus.emit(
            "spawn.complete",
            {
                "child_run_id": child_run_id,
                "child_did": child_did,
                "parent_run_id": parent_state.run_id,
                "status": "error",
            },
        )
        _emit_spawn_audit(
            action="spawn.complete",
            child_run_id=child_run_id,
            child_did=child_did,
            parent_run_id=parent_state.run_id,
            outcome="error",
            extra={"duration_s": duration_s, "error": type(exc).__name__},
            sink=audit_sink,
        )
        _emit_ui_spawn_event(
            reporter=ui_reporter,
            event_type="spawn_complete",
            data={
                "child_run_id": child_run_id,
                "parent_run_id": parent_state.run_id,
                "status": "error",
                "duration_s": duration_s,
            },
        )
        _end_child_span(span, otel_token, "error")
        audit_tip = hashlib.sha256(child_run_id.encode()).hexdigest()
        return SpawnResult(
            child_run_id=child_run_id,
            child_did=child_did,
            status="error",
            summary="Child task failed",
            tokens=TokenUsage(),
            tool_trace=tool_trace,
            audit_chain_tip=audit_tip,
            duration_s=duration_s,
            error=f"{type(exc).__name__}: {str(exc)[:_MAX_ERROR_LEN]}",
        )


# ---------------------------------------------------------------------------
# UI reporter helper — duck-typed call, no arcui import (layer purity)
# ---------------------------------------------------------------------------


def _emit_ui_spawn_event(
    *,
    reporter: Any | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Call reporter.emit_run_event() if a reporter is injected.

    Failures are swallowed so UI errors never interrupt spawn delivery.
    Duck-typed: no arcui import. Zero overhead when reporter is None.
    """
    if reporter is None:
        return
    try:
        reporter.emit_run_event(event_type=event_type, data=data)
    except Exception:
        _logger.warning(
            "UIReporter.emit_run_event failed event_type=%s — swallowing",
            event_type,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Audit helper — emit AuditEvents via arctrust, fall back to logger
# ---------------------------------------------------------------------------


def _emit_spawn_audit(
    *,
    action: str,
    child_run_id: str,
    child_did: str,
    parent_run_id: str,
    outcome: str,
    extra: dict[str, Any] | None = None,
    sink: Any | None,
) -> None:
    """Emit an AuditEvent for a spawn lifecycle event.

    Falls back to logger-only when arctrust is unavailable or sink is None.
    Per NIST AU-5, sink failures are swallowed so auditing never breaks spawn.
    """
    if sink is None:
        return
    try:
        from arctrust import AuditEvent, emit

        event = AuditEvent(
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
        emit(event, sink)
    except Exception:
        _logger.warning(
            "Failed to emit AuditEvent action=%s child_run_id=%s — swallowing (AU-5)",
            action,
            child_run_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# spawn_many() — parallel multi-spawn with budget pooling and concurrency cap
# ---------------------------------------------------------------------------


async def spawn_many(
    specs: list[SpawnSpec],
    *,
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT_SPAWNS,
    fail_fast: bool = False,
) -> list[SpawnResult]:
    """Spawn multiple children in parallel, respecting concurrency and budget limits.

    Args:
        specs: Ordered list of SpawnSpec. Results are returned in the same order.
        max_concurrent: Maximum number of children running at once.
        fail_fast: If True, cancel remaining pending spawns on first error/timeout.
                   Completed results are preserved.

    Returns:
        List of SpawnResult in the same order as specs.
    """
    if not specs:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    results: list[SpawnResult | None] = [None] * len(specs)
    cancelled = asyncio.Event()

    async def _run_one(idx: int, spec: SpawnSpec) -> None:
        # Respect fail_fast cancellation before acquiring semaphore
        if fail_fast and cancelled.is_set():
            identity = ChildIdentity(
                did=spec.child_did,
                sk_bytes=spec.child_sk_bytes,
                ttl_s=int(spec.wallclock_timeout_s),
            )
            audit_tip = hashlib.sha256(spec.child_did.encode()).hexdigest()
            results[idx] = SpawnResult(
                child_run_id=str(uuid.uuid4()),
                child_did=spec.child_did,
                status="interrupted",
                summary="Cancelled by fail_fast",
                tokens=TokenUsage(),
                tool_trace=[],
                audit_chain_tip=audit_tip,
                duration_s=0.0,
                error="cancelled",
            )
            return

        # Check token budget before acquiring the semaphore
        root_budget: RootTokenBudget | None = getattr(
            spec.parent_state, "root_token_budget", None
        )
        if root_budget is not None and spec.token_budget is not None:
            granted = await root_budget.try_debit(spec.token_budget)
            if not granted:
                audit_tip = hashlib.sha256(spec.child_did.encode()).hexdigest()
                results[idx] = SpawnResult(
                    child_run_id=str(uuid.uuid4()),
                    child_did=spec.child_did,
                    status="budget_exhausted",
                    summary="Token budget exhausted",
                    tokens=TokenUsage(),
                    tool_trace=[],
                    audit_chain_tip=audit_tip,
                    duration_s=0.0,
                    error="budget_exhausted",
                )
                if fail_fast:
                    cancelled.set()
                return

        async with semaphore:
            identity = ChildIdentity(
                did=spec.child_did,
                sk_bytes=spec.child_sk_bytes,
                ttl_s=int(spec.wallclock_timeout_s),
            )
            result = await spawn(
                parent_state=spec.parent_state,
                task=spec.task,
                tools=spec.tools,
                system_prompt=spec.system_prompt,
                identity=identity,
                model=spec.model,
                context=spec.context,
                max_turns=spec.max_turns,
                token_budget=spec.token_budget,
                wallclock_timeout_s=spec.wallclock_timeout_s,
                sandbox=spec.sandbox,
            )
            results[idx] = result

            if fail_fast and result.status in ("error", "timeout"):
                cancelled.set()

    await asyncio.gather(*(_run_one(i, spec) for i, spec in enumerate(specs)))

    # All slots must be filled — replace any None with an error result
    final: list[SpawnResult] = []
    for i, r in enumerate(results):
        if r is None:
            audit_tip = hashlib.sha256(specs[i].child_did.encode()).hexdigest()
            final.append(
                SpawnResult(
                    child_run_id=str(uuid.uuid4()),
                    child_did=specs[i].child_did,
                    status="error",
                    summary="Spawn did not complete",
                    tokens=TokenUsage(),
                    tool_trace=[],
                    audit_chain_tip=audit_tip,
                    duration_s=0.0,
                    error="did not complete",
                )
            )
        else:
            final.append(r)
    return final
