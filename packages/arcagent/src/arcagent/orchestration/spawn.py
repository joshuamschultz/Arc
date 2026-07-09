"""Spawn tool — recursively start a child run() for task decomposition.

Security considerations (ASI-01, ASI-08, LLM10, NIST AU-2/AU-3):
- System prompt override: child inherits parent's prompt as immutable preamble
- Timeout: wall-clock limit on child execution prevents unbounded consumption
- Audit: spawn.start and spawn.complete events emitted for every child
- Error sanitization: internal details logged, generic message returned to LLM
- Concurrency: max_concurrent_spawns limits parallel child runs
- Token budget: RootTokenBudget prevents children from silently overrunning the
  caller's allocation (Hermes implicit-token-pool bug fix).

Sibling modules
---------------
- ``arcagent.orchestration.token_budget``  — RootTokenBudget + TokenUsage.
- ``arcagent.orchestration.spawn_handle``  — SpawnResult + SpawnSpec
  dataclasses + the _SpawnStatus literal.

Names from the siblings are re-exported through this module so existing
imports
(``from arcagent.orchestration.spawn import RootTokenBudget,
   SpawnResult, SpawnSpec, TokenUsage``) keep working unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from arcllm.modules.telemetry import agent_identity
from arcrun.events import Event, EventBus
from arcrun.state import RunState
from arcrun.types import SandboxConfig, Tool, ToolContext
from arcstore.records import SpoolRecord as _SpoolRecord
from arcstore.spool import record as _spool_record
from arctrust import ChildIdentity
from arctrust.classification import Classification

from arcagent.orchestration.spawn_handle import (
    _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    SpawnResult,
    SpawnSpec,
    _SpawnStatus,
)
from arcagent.orchestration.token_budget import RootTokenBudget, TokenUsage

_logger = logging.getLogger("arcagent.orchestration.spawn")


__all__ = [
    "RootTokenBudget",
    "SpawnResult",
    "SpawnSpec",
    "TokenUsage",
    "make_spawn_tool",
    "spawn",
    "spawn_many",
]


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


# ---------------------------------------------------------------------------
# Spawn observability (SPEC-028 FR-3) — operational identity + lineage edge.
# arcrun stays a pure loop; this is all arcagent. The arctrust WORM keeps the
# compliance edge (``_emit_spawn_audit``); the spool carries the operational
# edge the UI renders, and child run/llm records spool under the child identity.
# ---------------------------------------------------------------------------


def _parent_did(state: RunState) -> str | None:
    """The DID the parent run is spooling under (set via ``run(actor_did=...)``).

    None when the parent isn't recording operationally (arcstore off) — in which
    case the child stays silent too, preserving the parent's posture. Reads the
    public ``EventBus`` accessor so a rename surfaces loudly, not silently.
    """
    return state.event_bus.spool_actor_did


def _child_label(child_did: str, role: str | None, depth: int) -> str:
    """Human-readable child label for the cost/identity view (UI groups on this)."""
    suffix = child_did.rsplit("/", 1)[-1]
    return f"{role or 'child'}:{suffix}:d{depth}"


def _spool_spawn_event(
    *, parent_did: str, child_did: str, role: str | None, depth: int, outcome: str
) -> None:
    """Record the operational parent→child lineage edge (fail-open like all spool)."""
    _spool_record(
        _SpoolRecord(
            kind="spawn_event",
            actor_did=child_did,
            parent_did=parent_did,
            child_did=child_did,
            role=role,
            depth=depth,
            outcome=outcome,
        )
    )


def make_spawn_tool(
    *,
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    sandbox: SandboxConfig | None = None,
    allowed_strategies: list[str] | None = None,
    spawn_timeout_seconds: int = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    max_concurrent_spawns: int = _DEFAULT_MAX_CONCURRENT_SPAWNS,
    max_child_turns: int = _DEFAULT_MAX_CHILD_TURNS,
    root_token_budget: RootTokenBudget | None = None,
) -> Tool:
    """Create a spawn_task tool that starts a child run().

    State is read from ``ctx.parent_state`` at execute time, set by the
    arcrun executor.

    When ``root_token_budget`` is supplied, it is a pool shared by every child
    this tool spawns during the run (LLM10). Each child runs with its
    ``max_tokens`` clamped to the pool's remaining balance, and its actual usage
    is debited on completion; once the pool is exhausted, further spawns are
    refused. This is the cross-child cap that stops one run from silently
    spending several times its allocation.
    """
    # Semaphore limits concurrent child runs (ASI-08, LLM10)
    spawn_semaphore = asyncio.Semaphore(max_concurrent_spawns)

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        run_state: RunState | None = ctx.parent_state
        if run_state is None:
            return "Error: spawn_task invoked outside an arcrun loop"

        # Depth guard
        if run_state.depth >= run_state.max_depth:
            return f"Error: max spawn depth ({run_state.max_depth}) reached"

        # Cross-child token cap (LLM10): refuse once the shared pool is spent.
        if root_token_budget is not None and root_token_budget.is_exhausted():
            return "Error: spawn token budget exhausted for this run"

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
        event_bus = run_state.event_bus
        parent_run_id = run_state.run_id
        bubble_handler = _make_bubble_handler(child_run_id, event_bus)

        def _complete(success: bool, **extra: Any) -> None:
            """Emit spawn.complete with the shared run ids plus caller extras."""
            event_bus.emit(
                "spawn.complete",
                {
                    "child_run_id": child_run_id,
                    "parent_run_id": parent_run_id,
                    "success": success,
                    **extra,
                },
            )

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
        from arcrun.capabilities import StaticProvider
        from arcrun.loop import run

        # Operational identity + lineage (SPEC-028 FR-3), only when the parent
        # is recording. The child spools its run_events/llm_calls under its own
        # DID and a spawn_event captures the parent→child edge for the UI.
        parent_did = _parent_did(run_state)
        child_did = f"did:arc:spawn:child/{child_run_id[:8]}"
        child_depth = run_state.depth + 1
        child_actor = child_did if parent_did is not None else None
        child_label = _child_label(child_did, None, child_depth) if child_actor else None
        if parent_did is not None:
            _spool_spawn_event(
                parent_did=parent_did,
                child_did=child_did,
                role=None,
                depth=child_depth,
                outcome="allow",
            )

        # Clamp the child's token ceiling to what the shared pool still allows,
        # so arcrun's breaker stops a child that would overrun the pool.
        child_max_tokens = root_token_budget.remaining if root_token_budget is not None else None

        try:
            async with spawn_semaphore:
                # agent_identity is a sync contextmanager (task-local identity);
                # bind it around the awaited child run, not as an async CM.
                with agent_identity(child_actor, child_label):
                    result = await asyncio.wait_for(
                        run(
                            model,
                            StaticProvider(child_tools),
                            child_system_prompt,
                            child_task,
                            max_turns=max_child_turns,
                            depth=run_state.depth + 1,
                            max_depth=run_state.max_depth,
                            on_event=bubble_handler,
                            sandbox=sandbox,
                            allowed_strategies=allowed_strategies,
                            actor_did=child_actor,
                            store_raw_bodies=run_state.event_bus.store_raw_bodies,
                            max_tokens=child_max_tokens,
                        ),
                        timeout=spawn_timeout_seconds,
                    )

            # Debit the child's actual usage from the shared pool (LLM10).
            if root_token_budget is not None:
                await root_token_budget.record_actual(int(result.tokens_used.get("total", 0)))

            # Audit event: spawn complete
            _complete(True, turns_used=result.turns, cost_usd=result.cost_usd)

            return result.content or "(no content)"

        except TimeoutError:
            _logger.warning(
                "Child run %s timed out after %ds",
                child_run_id,
                spawn_timeout_seconds,
            )
            _complete(False, error="timeout")
            return f"Error: child task timed out after {spawn_timeout_seconds}s"

        except Exception as exc:  # reason: fail-open — log + continue
            # Log full details internally, return sanitized message to LLM
            _logger.warning(
                "Child run %s failed: %s: %s",
                child_run_id,
                type(exc).__name__,
                str(exc)[:_MAX_ERROR_LEN],
            )
            _complete(False, error=type(exc).__name__)
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
    except Exception:  # reason: OTel optional — degrade gracefully (air-gapped)
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
    except Exception:  # reason: OTel optional — log at debug, never fatal
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


# arcrun's circuit breaker records why a child halted in
# ``completion_payload['error']`` (its budget-breach vocabulary). Map each reason
# onto the spawn terminal status so a truncated child is never reported as a
# genuine completion (audit honesty). A clean end_turn / task_complete leaves no
# breach reason and stays "completed".
_BREACH_TO_STATUS: dict[str, _SpawnStatus] = {
    "max_turns": "max_iterations",
    "max_tokens": "budget_exhausted",
    "max_cost": "budget_exhausted",
    "runaway_loop": "error",
    "error_cascade": "error",
}


def _status_from_loop_result(loop_result: Any) -> _SpawnStatus:
    """Derive the child's terminal status from its LoopResult."""
    payload = loop_result.completion_payload or {}
    reason = payload.get("error")
    if not isinstance(reason, str):
        return "completed"
    return _BREACH_TO_STATUS.get(reason, "completed")


async def spawn(
    *,
    parent_state: RunState,
    task: str,
    tools: list[Tool],
    system_prompt: str,
    identity: ChildIdentity | None = None,
    model: Any = None,
    context: str | None = None,
    role: str | None = None,
    max_turns: int = _DEFAULT_MAX_CHILD_TURNS,
    parent_clearance: Classification = Classification.UNCLASSIFIED,
    token_budget: int | None = None,
    wallclock_timeout_s: float = _DEFAULT_SPAWN_TIMEOUT_SECONDS,
    sandbox: SandboxConfig | None = None,
    audit_sink: Any | None = None,
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

    Returns:
        SpawnResult with status, summary, token counts, and audit chain tip.
    """
    # arcrun imports are local to avoid a circular import at module load; time is
    # stdlib (module-top).
    from arcrun.capabilities import StaticProvider
    from arcrun.loop import run

    child_run_id = str(uuid.uuid4())

    # Resolve identity first so error results carry the correct DID. SPEC-038
    # REQ-022 — a delegated child's clearance is monotone-non-increasing: it can
    # never exceed the delegator's. Clamp both the derived and any caller-
    # supplied identity down to the parent's clearance (no privilege escalation).
    if identity is None:
        seed = uuid.uuid4().bytes[:32]
        hex_suffix = seed[:4].hex()
        identity = ChildIdentity(
            did=f"did:arc:delegate:child/{hex_suffix}",
            sk_bytes=seed,
            ttl_s=int(wallclock_timeout_s),
            clearance=parent_clearance,
        )
    else:
        identity = identity.model_copy(
            update={"clearance": min(identity.clearance, parent_clearance)}
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

    # Operational identity + lineage (SPEC-028 FR-3). The child spools under its
    # own DID only when the parent is recording (posture preserved); the spool
    # edge mirrors the audit edge for the UI lineage tree.
    parent_did = _parent_did(parent_state)
    child_depth = parent_state.depth + 1
    child_actor = child_did if parent_did is not None else None
    child_label = _child_label(child_did, role, child_depth)
    if parent_did is not None:
        _spool_spawn_event(
            parent_did=parent_did,
            child_did=child_did,
            role=role,
            depth=child_depth,
            outcome="allow",
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
        # Bind the child identity on this task so the child's llm_calls (which
        # reuse the parent's model/telemetry) attribute to the child, not the
        # parent (C2). Set before the await; reset on exit.
        with agent_identity(child_actor, child_label if child_actor else None):
            loop_result = await asyncio.wait_for(
                run(
                    model,
                    StaticProvider(tools),
                    system_prompt,
                    full_task,
                    max_turns=max_turns,
                    depth=parent_state.depth + 1,
                    max_depth=parent_state.max_depth,
                    on_event=_trace_handler,
                    sandbox=sandbox,
                    actor_did=child_actor,
                    store_raw_bodies=parent_state.event_bus.store_raw_bodies,
                ),
                timeout=wallclock_timeout_s,
            )

        duration_s = time.monotonic() - start
        tokens = TokenUsage(
            input=loop_result.tokens_used.get("input", 0),
            output=loop_result.tokens_used.get("output", 0),
            total=loop_result.tokens_used.get("total", 0),
        )

        # Determine status from loop result — honor a breaker-truncated child
        # rather than reporting its partial output as a completion.
        result_status: _SpawnStatus = _status_from_loop_result(loop_result)

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

    except Exception as exc:  # reason: fail-open — log + continue
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
    except Exception:  # reason: fail-open — log + continue
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
        root_budget: RootTokenBudget | None = getattr(spec.parent_state, "root_token_budget", None)
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
