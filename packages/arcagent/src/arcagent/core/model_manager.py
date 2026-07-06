"""LLM model loading + ArcRun/ArcLLM event bridges.

Sibling of ``arcagent.core.agent``. Owns the lazy model loader that
wires a JSONLTraceStore + on_event bridge into ArcLLM, and the two
event bridges that map ArcRun events and ArcLLM TraceRecords onto the
ModuleBus.

Re-exported through ``arcagent.core.agent`` so existing imports
(``from arcagent.core.agent import create_arcrun_bridge,
   create_arcllm_bridge``) keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcrun import Event
from arctrust import AuditEvent, OperatorKey, WormSink, emit, sign

from arcagent.core.config import ArcAgentConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.utils import load_eval_model

if TYPE_CHECKING:
    from arctrust import WitnessAnchor

_logger = logging.getLogger("arcagent.model_manager")


def _canonical_checkpoint_bytes(checkpoint: dict[str, Any]) -> bytes:
    """Deterministic bytes an operator signature over a checkpoint covers."""
    return json.dumps(checkpoint, sort_keys=True, ensure_ascii=True).encode("utf-8")


def build_checkpoint_sink(
    agent_root: Path,
    operator_key: OperatorKey,
    *,
    actor_did: str,
    witness: WitnessAnchor | None = None,
    federal: bool = False,
) -> Callable[[dict[str, Any]], None]:
    """Build the trace-store checkpoint sink — an OPERATOR-signed WORM anchor.

    arcllm's ``JSONLTraceStore`` emits a ``build_checkpoint`` manifest at each
    rotation; this sink anchors it as one operator-signed ``trace.checkpoint``
    WORM record (SPEC-053 REQ-002/008), so ``read_verified_anchor`` proves the
    head under the operator pubkey — never the agent DID. At federal tier the
    same operator-signed head is also submitted to an external ``witness``
    (REQ-009), making a rollback past the last anchor detectable even by a
    holder of the operator key.

    The chain lives in ``<agent_root>/.audit`` (outside the workspace). The
    WormSink is opened per-checkpoint (append + close): checkpoints are rare
    (rotation boundaries) and the sink restores its tip from the file, so this
    keeps ``ensure_model`` stateless with no long-held lock.

    Local WORM anchoring is fail-open (AU-5) — it must never break a run. The
    federal witness is NOT: an unwitnessed head silently defeats the rollback
    defense, so at ``federal`` a failed ``witness.submit`` fails the operation
    (REQ-009). Below federal the witness is best-effort and swallowed.
    """
    chain = agent_root / ".audit" / "trace-checkpoint.worm"

    def _sink(checkpoint: dict[str, Any]) -> None:
        _anchor_local(chain, operator_key, actor_did, checkpoint)
        _submit_witness(witness, operator_key, checkpoint, federal=federal)

    return _sink


def _anchor_local(
    chain: Path, operator_key: OperatorKey, actor_did: str, checkpoint: dict[str, Any]
) -> None:
    """Append the operator-signed checkpoint to the local WORM chain (fail-open)."""
    try:
        worm = WormSink(chain, operator_key.seed)
        try:
            emit(
                AuditEvent(
                    actor_did=actor_did,
                    action="trace.checkpoint",
                    target="trace-store",
                    outcome="anchored",
                    extra=checkpoint,
                ),
                worm,
            )
        finally:
            worm.close()
    except Exception:  # reason: fail-open — local anchoring must never break a run (AU-5)
        _logger.warning("trace checkpoint anchoring failed — swallowing (AU-5)", exc_info=True)


def _submit_witness(
    witness: WitnessAnchor | None,
    operator_key: OperatorKey,
    checkpoint: dict[str, Any],
    *,
    federal: bool,
) -> None:
    """Submit the operator-signed head to the external witness.

    Federal: mandatory — a failed submit raises (an unwitnessed head defeats the
    rollback defense). Below federal: best-effort, swallowed (REQ-009).
    """
    if witness is None:
        return
    signature = sign(_canonical_checkpoint_bytes(checkpoint), operator_key.seed)
    try:
        witness.submit(checkpoint, signature)
    except Exception:
        if federal:
            raise
        _logger.warning("witness submission failed (non-federal) — swallowing", exc_info=True)


def create_arcrun_bridge(
    bus: ModuleBus,
    *,
    model_id: str = "",
    agent_label: str = "",
) -> Callable[[Event], None]:
    """Create on_event callback for arcrun.run().

    Maps ArcRun lifecycle events to Module Bus events:
      tool.start  → agent:pre_tool
      tool.end    → agent:post_tool
      turn.start  → agent:pre_plan
      turn.end    → agent:post_plan

    llm.call is NOT mapped — the arcllm bridge emits llm:call_complete
    from TraceRecord with trace_id, bodies, and phase timings.

    ArcRun's on_event is synchronous (Callable[[Event], None]),
    so we schedule the async bus.emit via the running event loop.
    """
    _event_map = {
        "tool.start": "agent:pre_tool",
        "tool.end": "agent:post_tool",
        "turn.start": "agent:pre_plan",
        "turn.end": "agent:post_plan",
    }
    _pending: set[asyncio.Task[Any]] = set()

    def bridge(event: Event) -> None:
        bus_event = _event_map.get(event.type)
        if bus_event is not None:
            # Always copy to a plain dict — Event.data is typed as
            # MappingProxyType[Any, Any] (read-only) by arcrun; ModuleBus.emit
            # requires dict[str, Any]. Shallow copy is intentional here.
            data: dict[str, Any] = dict(event.data)
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, data))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            except RuntimeError:
                _logger.warning(
                    "No running event loop for bridge event: %s",
                    event.type,
                )

    return bridge


def create_arcllm_bridge(bus: ModuleBus) -> Callable[[Any], None]:
    """Create on_event callback for ArcLLM's load_model().

    Maps ArcLLM TraceRecord event_types to Module Bus events:
      llm_call       → llm:call_complete
      config_change  → llm:config_change
      circuit_change → llm:circuit_change

    ArcLLM's on_event is synchronous (Callable[[TraceRecord], None]),
    so we schedule the async bus.emit via the running event loop.
    Accepts both TraceRecord (Pydantic) and plain dict inputs.
    """
    _event_map = {
        "llm_call": "llm:call_complete",
        "config_change": "llm:config_change",
        "circuit_change": "llm:circuit_change",
    }
    # Hold strong references to pending tasks so they aren't GC'd
    _pending: set[asyncio.Task[Any]] = set()

    def bridge(record: Any) -> None:
        data = record.model_dump() if hasattr(record, "model_dump") else record
        event_type = data.get("event_type", "")
        bus_event = _event_map.get(event_type)
        if bus_event is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(bus.emit(bus_event, data))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            except RuntimeError:
                _logger.warning(
                    "No running event loop for LLM bridge event: %s",
                    event_type,
                )

    return bridge


def ensure_model(
    *,
    config: ArcAgentConfig,
    workspace: Path,
    bus: ModuleBus | None,
    operator_key: OperatorKey | None = None,
    actor_did: str = "",
    witness: WitnessAnchor | None = None,
) -> tuple[Any, Any]:
    """Load the eval model, wiring trace store + on_event bridge.

    Passes a JSONLTraceStore so every LLM call is persisted to
    ``<agent_root>/traces/`` for historical UI display and audit.
    Per ``arcllm.JSONLTraceStore`` (NIST AU-9), traces live OUTSIDE
    the workspace tool sandbox — the trace store wants the agent
    root, not the workspace subdirectory.

    When an ``operator_key`` is supplied, the store's rotation checkpoints are
    anchored in an operator-signed WORM chain (SPEC-053); at federal tier the
    ``witness`` externally witnesses each head (REQ-009).

    Returns ``(model, trace_store)``. The caller is responsible for
    caching both — this helper is intentionally stateless so it can
    be unit-tested without an ArcAgent instance.
    """
    from arcllm.trace_store import JSONLTraceStore

    agent_root = workspace.parent
    checkpoint_sink = (
        build_checkpoint_sink(
            agent_root,
            operator_key,
            actor_did=actor_did,
            witness=witness,
            federal=config.security.tier == "federal",
        )
        if operator_key is not None
        else None
    )
    trace_store = JSONLTraceStore(agent_root, checkpoint_sink=checkpoint_sink)
    on_event = create_arcllm_bridge(bus) if bus is not None else None
    model = load_eval_model(
        config.llm.model,
        trace_store=trace_store,
        agent_label=config.agent.name,
        on_event=on_event,
        arcllm_modules=config.llm.modules or None,
    )
    return model, trace_store
