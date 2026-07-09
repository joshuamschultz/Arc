"""Skill-mutation approval provider — binds the improver's seam to the shared HumanGate.

SPEC-044 D-10 / CRITICAL-2. The provider-free improver declares a thin injected
``ApprovalProvider`` callable (``(action, skill_name, detail) -> bool``); this builds the
arcagent-side implementation by REUSING the SPEC-035/043
:class:`~arcagent.tools.human_gate.HumanGate` (operator-signed one-shot grants, ASI09
self-approval guard, fail-closed at federal) — not a parallel approval system.

A skill mutation/retire/revive is surfaced as a synthetic :class:`~arctrust.policy.ToolCall`
carrying its own ``skill_mutation`` leg so the gate's audit records it as a skill-approval
event. ``HumanGate.request`` returns an :class:`ApprovalGrant` on approve and ``None`` on
deny/timeout/no-channel; the improver treats a falsy return as "blocked" and audits it.
No interactive approval channel is wired yet (SPEC-032 follow-on), so with the default
channel-less gate every gated mutation fails closed at enterprise/federal.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

_logger = logging.getLogger("arcagent.modules.skills.approval")

_SKILL_MUTATION_LEG = "skill_mutation"

ApprovalProvider = Callable[[str, str, str], Awaitable[bool]]


def build_skill_approval_provider(human_gate: object, agent_did: str) -> ApprovalProvider:
    """Return an ``ApprovalProvider`` that asks the shared HumanGate for a skill mutation."""
    from arctrust.policy import ToolCall

    async def _provider(action: str, skill_name: str, detail: str) -> bool:
        call = ToolCall(
            tool_name=f"skill.mutation:{action}",
            arguments={"skill": skill_name, "detail": detail[:200]},
            agent_did=agent_did,
            session_id="",
            classification="unclassified",
        )
        try:
            grant = await human_gate.request(call, legs=frozenset({_SKILL_MUTATION_LEG}))  # type: ignore[attr-defined]
        except Exception:  # reason: fail-closed — any approval error denies the mutation
            _logger.exception("skill approval request raised; failing closed")
            return False
        return grant is not None

    return _provider


__all__ = ["ApprovalProvider", "build_skill_approval_provider"]
