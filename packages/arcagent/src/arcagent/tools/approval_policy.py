"""Proactive-HITL approval policy — tier resolution + provider binding (SPEC-043).

arcrun's loop enforces a DUMB membership predicate: dispatch pauses iff a tool's
name is in the resolved approval set (``RunState.approval_required_tools``). ALL
tier logic lives here in arcagent, exactly like the SPEC-038 budget floor split —
arcagent resolves the policy, arcrun enforces it. The tier ladder (REQ-010b/c,
ADR-019):

* **personal** — empty (free run); config MAY opt specific tool names in.
* **enterprise** — every plain tool requires approval (skill-backed excluded).
* **federal** — every skill AND every tool (the full effecting-capability surface).

The same split governs which execution strategies a run may use: personal and
enterprise leave the set open so each run picks the shape that fits its task,
while federal narrows to ``react`` alone and cannot be widened by config.

The provider binds the pause to SPEC-035 ``HumanGate`` (operator-signed one-shot
``ApprovalGrant``). arcrun mints/verifies nothing (REQ-012).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

import arcrun
from arctrust.signer import Signer, verify_signature

from arcagent.tools._transport import RegisteredTool
from arcagent.tools.checkpoint_signing import sign_record
from arcagent.tools.human_gate import HumanGate

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent
    from arcagent.core.session_internal import SessionManager


_logger = logging.getLogger(__name__)

_REACT = "react"
"""The one strategy whose control flow is code an operator can read."""


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


def resolve_allowed_strategies(configured: list[str] | None, tier: str) -> list[str] | None:
    """Resolve which execution strategies a run may use, by tier.

    personal and enterprise leave the set open: ``None`` reaches arcrun meaning
    every registered strategy, so each run picks the shape that fits its task.
    federal narrows to ``react`` alone, because the other strategies let a model
    author its own control flow, and at that tier the sequence of work must be
    something an operator approved rather than something a model invented.

    The federal floor is **not relaxable**. An operator who lists a wider set at
    that tier still gets ``react``: a floor that config can widen is not a floor.
    """
    if tier == "federal":
        if configured and set(configured) - {_REACT}:
            _logger.warning(
                "tier 'federal' permits only the %r strategy; ignoring the configured %s",
                _REACT,
                sorted(set(configured) - {_REACT}),
            )
        return [_REACT]
    return configured


class _OperatorSeal:
    """Adapts the operator ``Signer`` to the sign/verify pair arcrun asks for.

    ``arctrust.Signer`` only signs; verification is a separate call needing the
    public key and algorithm. arcrun should not know either, so the pairing is
    made here and handed over as one opaque object.
    """

    def __init__(self, signer: Signer) -> None:
        self._signer = signer

    def sign(self, message: bytes) -> bytes:
        return self._signer.sign(message)

    def verify(self, message: bytes, signature: bytes) -> bool:
        return verify_signature(
            self._signer.algorithm, message, signature, self._signer.public_key
        )


def build_run_seal(agent: ArcAgent) -> arcrun.RunSeal | None:
    """Operator custody over the files a dynamic run resumes from.

    Signatures land in ``<agent_root>/.audit/`` — a sibling of the workspace,
    not a child of it, because the audited subject must not be its own audit
    authority. The agent's own ``write``/``bash`` tools can rewrite the script
    and journal in its workspace; they cannot reach the operator's signatures
    over them, so a rewrite is detected instead of trusted.

    No operator signer means no seal, which signs and verifies nothing. That is
    the same code path, not a second one, so personal tier behaves identically.
    """
    signer = agent._operator_signer
    if signer is None:
        return None
    return arcrun.RunSeal(
        signer=_OperatorSeal(signer),
        directory=agent._workspace.parent / ".audit" / "dynamic",
    )


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
    run_cfg = agent._config.arcrun
    registry = agent._tool_registry
    tools = list(registry.tools.values()) if registry is not None else []
    approval_set = resolve_approval_set(tools, sec.tier, opt_in=frozenset(run_cfg.approval_opt_in))
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

    sandbox = (
        arcrun.SandboxConfig(allowed_tools=run_cfg.sandbox.allowed_tools)
        if run_cfg.sandbox.allowed_tools is not None
        else None
    )

    return {
        "on_checkpoint": _on_checkpoint,
        "approval_provider": provider,
        "approval_required_tools": approval_set,
        # Durable home for run-scoped engine artifacts — today the dynamic
        # strategy's replay journal and script scratch. It is the agent's own
        # workspace, written with direct filesystem I/O and never through the
        # LLM-facing file tools, because a run record is agent state and the
        # brain stays home wherever the tools happen to be pointed (ADR-029).
        # arcrun never invents this path; without it a paused script simply
        # cannot resume after a restart.
        "work_dir": agent._workspace / "runs",
        # Operator signatures over those artifacts, kept outside the workspace.
        "seal": build_run_seal(agent),
        # Loop mechanics (arcrun.toml). Behaviour-preserving defaults: max_turns=25,
        # tool_timeout=None, allowed_strategies=None, sandbox=None (full-allow).
        "max_turns": run_cfg.max_turns,
        "tool_timeout": run_cfg.tool_timeout,
        "allowed_strategies": resolve_allowed_strategies(run_cfg.allowed_strategies, sec.tier),
        "sandbox": sandbox,
        # Security floors ([security]) — tier-resolved circuit breakers + parallelism cap.
        "max_parallel": sec.loop_max_parallel,
        "max_repeat": sec.runaway_max_repeat,
        "max_consecutive_errors": sec.error_cascade_max,
    }


def narrowed_loop_controls(
    agent: ArcAgent, session: SessionManager, requested: list[str] | None
) -> dict[str, Any]:
    """Loop-control kwargs, narrowed by a caller-requested strategy allowlist.

    A caller (a workflow node, SPEC-061 REQ-243) may pin which strategies its
    turn may use, but it may never WIDEN what the operator allowed in
    ``arcrun.toml``: the request is intersected with a configured allowlist, so
    the tighter set always wins — the same rule the per-run token/cost budget
    follows.

    An un-pinned turn (``requested is None`` — the ordinary inbound message)
    takes ``react`` alone unless the operator widened the ceiling. With one
    strategy allowed arcrun skips the per-run ``select_strategy`` model call
    entirely, so a basic message runs one full-context react turn instead of
    paying a stripped selection call that could route it onto a model-authored
    ``code`` / ``dynamic`` path. Widening the ceiling is how an operator opts
    basic turns back into model-selected control flow.
    """
    controls = build_loop_controls(agent, session)
    if requested is None:
        controls["allowed_strategies"] = controls.get("allowed_strategies") or [_REACT]
        return controls
    configured = controls.get("allowed_strategies")
    controls["allowed_strategies"] = (
        requested if not configured else [s for s in requested if s in configured]
    )
    return controls


__all__ = [
    "build_approval_provider",
    "build_loop_controls",
    "narrowed_loop_controls",
    "resolve_approval_set",
]
