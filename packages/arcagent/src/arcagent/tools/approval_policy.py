"""Proactive-HITL approval policy — tier resolution + provider binding (SPEC-043).

arcrun's loop enforces a DUMB membership predicate: dispatch pauses iff a tool's
name is in the resolved approval set (``RunState.approval_required_tools``). ALL
tier logic lives here in arcagent, exactly like the SPEC-038 budget floor split —
arcagent resolves the policy, arcrun enforces it. The tier ladder (REQ-010b/c,
ADR-019):

* **personal** — empty (free run); config MAY opt specific tool names in.
* **enterprise** — every plain tool requires approval (skill-backed excluded).
* **federal** — every skill AND every tool (the full effecting-capability surface).

The provider binds the pause to SPEC-035 ``HumanGate`` (operator-signed one-shot
``ApprovalGrant``). arcrun mints/verifies nothing (REQ-012).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from arcagent.tools._transport import RegisteredTool
from arcagent.tools.checkpoint_signing import sign_record
from arcagent.tools.human_gate import HumanGate

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent
    from arcagent.core.session_internal import SessionManager


def resolve_approval_set(
    tools: Iterable[RegisteredTool],
    tier: str,
    *,
    opt_in: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Resolve the tier's approval-required tool-name set (REQ-010b/c).

    federal → all capability names (skills + tools); enterprise → plain tools
    plus any config opt-ins; personal → opt-ins only. The result is a plain name
    set — arcrun does a membership test, no tier logic in the loop.
    """
    tool_list = list(tools)
    if tier == "federal":
        return frozenset(t.name for t in tool_list)
    if tier == "enterprise":
        return frozenset(t.name for t in tool_list if not t.skill_backed) | opt_in
    return frozenset(opt_in)


def build_approval_provider(
    human_gate: HumanGate,
    *,
    agent_did: str,
    session_id: str = "",
) -> Callable[[Any], Awaitable[Any]]:
    """Bind an ``approval_provider(tc)`` to SPEC-035 ``HumanGate`` (REQ-012).

    arcrun awaits this before dispatching a flagged call; a returned
    ``ApprovalGrant`` (truthy) admits the single call, ``None`` fails closed. The
    grant is operator-signed — the agent has no path to mint it (ASI09). arcrun
    never inspects the token; only its presence is read.
    """
    from arcagent.core.tool_policy import ToolCall

    async def provider(tc: Any) -> Any:
        call = ToolCall(
            tool_name=getattr(tc, "name", ""),
            arguments=dict(getattr(tc, "arguments", {}) or {}),
            agent_did=agent_did,
            session_id=session_id,
            classification="unclassified",
        )
        # legs empty: the proactive trigger is tool-identity-based, not a trifecta
        # composition — the gate still requires an operator-signed grant (or a
        # named auto-approve at personal/enterprise) or fails closed.
        return await human_gate.request(call, legs=frozenset())

    return provider


def build_loop_controls(agent: ArcAgent, session: SessionManager) -> dict[str, Any]:
    """Assemble the SPEC-043 loop-control kwargs for a streaming run.

    ALL tier resolution lives here (arcagent), never in the loop: the approval
    set is the tier ladder (REQ-010b/c), the breaker thresholds are the
    config-resolved floors (REQ-024), and the checkpoint hook persists each turn
    boundary via the session (REQ-005). arcrun enforces the resolved predicate +
    thresholds as dumb mechanism (same split as the SPEC-038 budget floor).
    """
    sec = agent._config.security
    registry = agent._tool_registry
    tools = list(registry.tools.values()) if registry is not None else []
    approval_set = resolve_approval_set(tools, sec.tier)
    provider = None
    if approval_set and agent._human_gate is not None:
        provider = build_approval_provider(
            agent._human_gate,
            agent_did=agent._identity.did if agent._identity else "did:arc:unknown",
            session_id=session.session_id,
        )

    signer = agent._operator_signer

    def _on_checkpoint(cp: Any) -> None:
        # arcrun emits synchronously at the turn boundary; persistence is async
        # and best-effort — scheduled off the hot path so it never blocks or
        # breaks the loop (REQ-002/005). The record is operator-signed so a
        # tampered/zeroed checkpoint fails closed on resume (REQ-004, F3).
        signature = sign_record(cp.to_record(), signer) if signer is not None else None
        asyncio.ensure_future(  # noqa: RUF006
            session.persist_checkpoint(cp, signature=signature)
        )

    return {
        "on_checkpoint": _on_checkpoint,
        "approval_provider": provider,
        "approval_required_tools": approval_set,
        "max_parallel": sec.loop_max_parallel,
        "max_repeat": sec.runaway_max_repeat,
        "max_consecutive_errors": sec.error_cascade_max,
    }


__all__ = ["build_approval_provider", "build_loop_controls", "resolve_approval_set"]
