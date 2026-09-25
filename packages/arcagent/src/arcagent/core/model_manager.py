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

import arcrun
from arctrust import AuditEvent, RecordCipher, Signer, WormSink, read_verified_anchor

from arcagent.core.background_tasks import BackgroundTaskSupervisor
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
    signer: Signer,
    *,
    actor_did: str,
    witness: WitnessAnchor | None = None,
    cipher: RecordCipher | None = None,
) -> Callable[[dict[str, Any]], None]:
    """Build the trace-store checkpoint sink — an OPERATOR-signed WORM anchor.

    arcllm's ``JSONLTraceStore`` emits a ``build_checkpoint`` manifest at each
    rotation; this sink anchors it as one operator-signed ``trace.checkpoint``
    WORM record (SPEC-053 REQ-002/008), so ``read_verified_anchor`` proves the
    head under the operator pubkey — never the agent DID. When configured, the
    external ``witness`` receives the same operator-signed head (REQ-009).

    The chain lives in ``<agent_root>/.audit`` (outside the workspace). The
    WormSink is opened per-checkpoint (append + close): checkpoints are rare
    (rotation boundaries) and the sink restores its tip from the file, so this
    keeps ``ensure_model`` stateless with no long-held lock.

    A failed durable append, verification, or configured witness submission
    refuses the checkpoint at every tier.
    """
    chain = agent_root / ".audit" / "trace-checkpoint.worm"

    def _sink(checkpoint: dict[str, Any]) -> None:
        _anchor_local(chain, signer, actor_did, checkpoint, cipher)
        _submit_witness(witness, signer, checkpoint)

    return _sink


def _anchor_local(
    chain: Path,
    signer: Signer,
    actor_did: str,
    checkpoint: dict[str, Any],
    cipher: RecordCipher | None,
) -> None:
    """Append and verify the operator-signed checkpoint before continuing."""
    worm = WormSink(chain, signer, cipher=cipher)
    try:
        worm.write_durable(
            AuditEvent(
                actor_did=actor_did,
                action="trace.checkpoint",
                target="trace-store",
                outcome="anchored",
                extra=checkpoint,
            )
        )
    finally:
        worm.close()
    if read_verified_anchor(chain, signer.public_key, cipher=cipher) != checkpoint:
        raise RuntimeError("trace checkpoint anchor verification failed")


def _submit_witness(
    witness: WitnessAnchor | None,
    signer: Signer,
    checkpoint: dict[str, Any],
) -> None:
    """Submit a configured witness and propagate refusal at every tier."""
    if witness is None:
        return
    signature = signer.sign(_canonical_checkpoint_bytes(checkpoint))
    witness.submit(checkpoint, signature)


def decision_point_moment(event: arcrun.Event) -> dict[str, Any] | None:
    """Build an ``agent:moment`` decision-point payload for a loop decision point (COMP-004).

    ``turn.start`` → a ``pre_plan`` moment (the default, cheaper site): the turn event
    carries no situation text, so its cues come from the working set the Brain already
    holds. ``tool.start`` → a ``pre_tool`` moment (the finer, opt-in site) carrying the
    tool name as a cue and ``tool(args)`` as text so recall keys off what is about to run.
    Any other event → ``None``. Thin and best-effort; the payload stays primitive.
    """
    if event.type == "turn.start":
        return {
            "kind": "decision_point",
            "point": "pre_plan",
            "cues": [],
            "text": "",
            "session_id": None,
        }
    if event.type == "tool.start":
        data = dict(event.data)
        tool = str(data.get("tool") or data.get("name") or "").strip()
        if not tool:
            return None
        args = data.get("args")
        text = f"{tool}({args})" if args not in (None, "", {}, []) else tool
        return {
            "kind": "decision_point",
            "point": "pre_tool",
            "cues": [tool],
            "text": text,
            "session_id": None,
        }
    return None


def create_arcrun_bridge(
    bus: ModuleBus,
    *,
    model_id: str = "",
    agent_label: str = "",
    task_supervisor: BackgroundTaskSupervisor | None = None,
    reply_target: str | None = None,
    bridge_only_tools: frozenset[str] | None = None,
    take_tool_outcome: Callable[[str, str], dict[str, Any] | None] | None = None,
    session_id: str | None = None,
    agent_did: str = "",
) -> Callable[[arcrun.Event], None]:
    """Create on_event callback for arcrun.run().

    Maps ArcRun lifecycle events to Module Bus events:
      tool.start  → agent:pre_tool
      tool.end    → agent:post_tool after ArcRun confirms the terminal outcome
      tool.error  → agent:post_tool with explicit failure status
      turn.start  → agent:pre_plan  (+ agent:run_progress heartbeat tick)
      turn.end    → agent:post_plan
      dynamic.*        → agent:run_progress
      strategy.selected → agent:run_progress  (run-start marker)

    llm.call is NOT mapped — the arcllm bridge emits llm:call_complete
    from TraceRecord with trace_id, bodies, and phase timings.

    ``dynamic.*`` is the ``dynamic`` strategy announcing its own stages —
    the plan it wrote, each phase, every child agent it starts and collects.
    Those runs last minutes, so a consumer can narrate them while they happen.
    Each one is forwarded with ``reply_target``, the channel THIS turn arrived
    on, stamped onto it: the origin is threaded through the event rather than
    looked up later, because a progress line that resolves its own destination
    is a progress line that can answer a group post on someone's phone.
    ``None`` (a scheduled, dispatched, or headless run) travels as None, and a
    consumer is expected to stay silent on it.

    ArcRun's on_event is synchronous (Callable[[Event], None]),
    so we schedule the async bus.emit via the running event loop.
    """
    _event_map = {
        "turn.start": "agent:pre_plan",
        "turn.end": "agent:post_plan",
    }
    # SPEC-072 COMP-004: also announce a decision-point moment at the plan (turn.start)
    # and tool (tool.start) sites so the memory module can recall right before the agent
    # acts. Emitted unconditionally here; the memory subscriber owns the config gates
    # (proactive_decision_point, and decision_point_pre_tool for the pre_tool site).
    supervisor = task_supervisor or BackgroundTaskSupervisor(logger=_logger)

    # The two lifecycle markers a plain (non-dynamic) run needs so a consumer can
    # pace a "still working" milestone: the strategy pick (the run's start) and
    # each turn (the heartbeat tick). Forwarded on the same progress channel as
    # the dynamic stages, and, like them, carrying the origin so a long run can
    # speak on the phone it was started from.
    _progress_markers = {"strategy.selected", "turn.start"}

    def bridge(event: arcrun.Event) -> None:
        # Always copy to a plain dict — Event.data is typed as
        # MappingProxyType[Any, Any] (read-only) by arcrun; ModuleBus.emit
        # requires dict[str, Any]. Shallow copy is intentional here.
        forwarded: list[tuple[str, dict[str, Any]]] = []
        if event.type.startswith("tool."):
            tool_event = _tool_bus_event(
                event,
                bridge_only_tools=bridge_only_tools,
                take_tool_outcome=take_tool_outcome,
                session_id=session_id,
                agent_did=agent_did,
            )
            if tool_event is not None:
                forwarded.append(tool_event)
        mapped = _event_map.get(event.type)
        if mapped is not None:
            forwarded.append(
                (
                    mapped,
                    {
                        **dict(event.data),
                        "session_id": session_id,
                        "run_id": event.run_id,
                        "agent_did": agent_did,
                    },
                )
            )
        moment = decision_point_moment(event)
        if moment is not None:
            forwarded.append(("agent:moment", moment))
        if event.type.startswith("dynamic.") or event.type in _progress_markers:
            forwarded.append(
                (
                    "agent:run_progress",
                    {
                        "event": event.type,
                        "reply_target": reply_target,
                        "data": dict(event.data),
                    },
                )
            )
        if not forwarded:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            _logger.warning(
                "No running event loop for bridge event: %s",
                event.type,
            )
            return
        for bus_event, data in forwarded:
            bus.publish_ordered(
                event.run_id, bus_event, data, supervisor=supervisor, agent_did=agent_did
            )

    return bridge


def _tool_bus_event(
    event: arcrun.Event,
    *,
    bridge_only_tools: frozenset[str] | None,
    take_tool_outcome: Callable[[str, str], dict[str, Any] | None] | None,
    session_id: str | None,
    agent_did: str,
) -> tuple[str, dict[str, Any]] | None:
    """Forward one terminal tool outcome without duplicating registry emissions."""
    data = event.data
    tool = data.get("name") or data.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    owned_by_bridge = bridge_only_tools is None or tool in bridge_only_tools
    call_id = data.get("tool_call_id") if isinstance(data.get("tool_call_id"), str) else ""
    turn_number = data.get("turn_number")
    if event.type == "tool.start":
        args = data.get("arguments")
        if not owned_by_bridge:
            return None
        return "agent:pre_tool", {"tool": tool, "args": args if isinstance(args, dict) else {}}
    if event.type not in {"tool.end", "tool.error"}:
        return None
    if not call_id:
        _logger.warning("ArcRun terminal tool event missing tool_call_id")
        return None
    if not isinstance(turn_number, int) or turn_number < 1:
        _logger.warning("ArcRun terminal tool event missing turn_number")
        return None
    staged = take_tool_outcome(event.run_id, call_id) if take_tool_outcome else None
    if event.type == "tool.end":
        if data.get("replayed"):
            return None
        status = "ok"
    elif event.type == "tool.error":
        status = _tool_error_status(data.get("error"))
    else:
        return None
    outcome: dict[str, Any] = {
        "tool": tool,
        "status": status,
        "run_id": event.run_id,
        "session_id": session_id,
        "agent_did": agent_did,
        "call_id": call_id,
        "turn_number": turn_number,
        "source": "arcrun",
    }
    if status == "ok" and staged is not None:
        outcome["args"] = staged["args"]
        outcome["result"] = staged["result"]
        outcome["duration"] = staged["duration"]
    if status != "ok":
        error = data.get("error")
        outcome["error_type"] = (
            error
            if isinstance(error, str) and error.isidentifier() and len(error) <= 64
            else "ToolError"
        )
    return "agent:post_tool", outcome


def _tool_error_status(error: Any) -> str:
    """Classify an ArcRun error without propagating a private exception message."""
    kind = str(error)
    if "Cancel" in kind or "cancel" in kind:
        return "cancelled"
    if "Timeout" in kind or "timeout" in kind or "Deadline" in kind:
        return "timeout"
    return "error"


def create_arcllm_bridge(
    bus: ModuleBus, *, task_supervisor: BackgroundTaskSupervisor | None = None
) -> Callable[[Any], None]:
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
    supervisor = task_supervisor or BackgroundTaskSupervisor(logger=_logger)

    def bridge(record: Any) -> None:
        from arcagent.core.session_internal.capability_ledger import current_session_id

        data = record.model_dump() if hasattr(record, "model_dump") else record
        event_type = data.get("event_type", "")
        bus_event = _event_map.get(event_type)
        if bus_event is not None:
            lineage = data.get("lineage")
            lineage = lineage if isinstance(lineage, dict) else {}
            session_id = (
                data.get("session_id") or lineage.get("session_id") or current_session_id()
            )
            run_id = data.get("run_id") or lineage.get("run_id") or arcrun.current_run_id()
            data = {
                **data,
                "session_id": session_id,
                "run_id": run_id,
            }
            try:
                asyncio.get_running_loop()
                if not isinstance(run_id, str) or not run_id:
                    _logger.warning("LLM bridge event missing canonical run_id: %s", event_type)
                    return
                bus.publish_ordered(run_id, bus_event, data, supervisor=supervisor)
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
    operator_signer: Signer | None = None,
    actor_did: str = "",
    witness: WitnessAnchor | None = None,
    record_cipher: RecordCipher | None = None,
    task_supervisor: BackgroundTaskSupervisor | None = None,
    queue_coordinator: arcrun.CallQueueCoordinator | None = None,
    queue_context: arcrun.CallQueueContext | None = None,
) -> tuple[Any, Any]:
    """Load the eval model, wiring trace store + on_event bridge.

    Passes a JSONLTraceStore so every LLM call is persisted to
    ``<agent_root>/traces/`` for historical UI display and audit.
    Per ``arcllm.JSONLTraceStore`` (NIST AU-9), traces live OUTSIDE
    the workspace tool sandbox — the trace store wants the agent
    root, not the workspace subdirectory.

    Rotation checkpoints require an operator signer and are anchored in a
    durable, verified WORM chain. A configured witness also receives each head.

    Returns ``(model, trace_store)``. The caller is responsible for
    caching both — this helper is intentionally stateless so it can
    be unit-tested without an ArcAgent instance.
    """
    agent_root = workspace.parent
    if operator_signer is None:
        raise RuntimeError("operator signer required for trace checkpoints")
    if config.security.tier == "federal" and witness is None:
        raise RuntimeError("external witness required for federal trace checkpoints")
    checkpoint_sink = build_checkpoint_sink(
        agent_root,
        operator_signer,
        actor_did=actor_did,
        witness=witness,
        cipher=record_cipher,
    )
    trace_store = arcrun.create_model_trace_store(agent_root, checkpoint_sink=checkpoint_sink)
    on_event = (
        create_arcllm_bridge(bus, task_supervisor=task_supervisor) if bus is not None else None
    )
    model = load_eval_model(
        config.llm.model,
        trace_store=trace_store,
        agent_label=config.agent.name,
        agent_did=actor_did or None,
        on_event=on_event,
        arcllm_modules=_arcllm_modules(config),
        queue_coordinator=queue_coordinator,
        queue_context=queue_context,
    )
    return model, trace_store


def _arcllm_modules(config: ArcAgentConfig) -> dict[str, Any]:
    """The agent's arcllm module overrides, with its declared routes folded in.

    ``[llm.routes]`` reaches arcllm as the routing module's route table, which
    is also the permission boundary: the router can only ever dispatch to a
    model this agent declared. Everything else in ``[llm.modules]`` passes
    through untouched.

    The table is passed *always*, empty included. Omitting it would let the
    deployment-wide ``[modules.routing.routes]`` in ``~/.arc/arcllm.toml`` apply
    to an agent that declared nothing — which is exactly the case the boundary
    exists to prevent, since a model being configured on the machine is not a
    grant to every agent on it (ASI03). An agent that wants an alternate lane
    names it.
    """
    modules: dict[str, Any] = {name: dict(cfg) for name, cfg in config.llm.modules.items()}
    routing = dict(modules.get("routing", {}))
    routing["routes"] = {
        name: {"model": route.model, "phrases": list(route.phrases)}
        for name, route in config.llm.routes.items()
    }
    modules["routing"] = routing
    return modules
