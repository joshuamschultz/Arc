"""Operator-approval seam for skill mutations (SPEC-044 D-10 / CRITICAL-2).

Implements the arcskill ``Approver`` Protocol (``request(*, action, skill_name, detail)
-> bool``) for the arcagent side. It reuses the SPEC-035 human-gate *contract* — an async
approval channel that surfaces the request to a human and returns the decision, with
**fail-closed** semantics on any timeout/error/absent channel — rather than the
:class:`~arcagent.tools.human_gate.HumanGate` itself, which is bound to a trifecta
``ToolCall``/``ApprovalGrant`` and does not model a skill mutation.

No interactive skill-approval channel is wired yet (that surface is a SPEC-032 follow-on),
so ``channel=None`` — the production default — makes every gated mutation/retire fail
closed (deny) at enterprise/federal. That is the correct secure default (NFR-006): a
federal deployment never auto-mutates a skill; wiring a channel later opts approval in.

The operator-signed *audit* of each approve/deny decision is emitted by the improver
(``ArcSkillImprover._authorize`` → the operator-key WORM sink), so this seam only decides.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

_logger = logging.getLogger("arcagent.modules.skills.approver")

# Surfaces (action, skill_name, detail) to a human and returns True iff approved.
ApprovalChannel = Callable[[str, str, str], Awaitable[bool]]


class SkillApprover:
    """arcskill ``Approver``: consult the operator channel, or fail closed (deny)."""

    def __init__(
        self, *, channel: ApprovalChannel | None = None, timeout_s: float = 300.0
    ) -> None:
        self._channel = channel
        self._timeout_s = timeout_s

    async def request(self, *, action: str, skill_name: str, detail: str) -> bool:
        """Return True iff a human explicitly approved. Fail closed on absence/timeout/error."""
        channel = self._channel
        if channel is None:
            _logger.info(
                "skill approval required for %s on %r but no approval channel is wired; "
                "failing closed (deny)",
                action,
                skill_name,
            )
            return False
        try:
            return await asyncio.wait_for(
                channel(action, skill_name, detail), timeout=self._timeout_s
            )
        except TimeoutError:
            return False
        except Exception:  # reason: fail-closed — any channel error denies the mutation
            _logger.exception("skill approval channel raised; failing closed")
            return False


__all__ = ["ApprovalChannel", "SkillApprover"]
