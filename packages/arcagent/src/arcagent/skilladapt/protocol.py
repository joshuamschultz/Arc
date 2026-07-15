"""The ``SkillAdapter`` seam — arcagent's skill-self-improvement boundary (SPEC-044).

arcagent ships **improver-less by default**, mirroring the ``Brain`` seam (SPEC-041).
This module defines a *structural* ``SkillAdapter`` Protocol (primitives only, so an
implementation need not import arcagent) and a no-op :class:`NullSkillAdapter` that is
the default. A user who wants skill self-improvement selects the ``arcskill`` adapter
(whose ``ArcSkillImprover`` satisfies this Protocol structurally) or a signed BYO class
by config (see :mod:`arcagent.skilladapt.select`).

The Protocol speaks only ``str``/``int``/``None`` at the boundary so that arcagent never
imports an arcskill type and any adapter is a drop-in. With :class:`NullSkillAdapter`
active, skill improvement is a silent no-op — no traces stored, no mutations, **no files
ever written** (REQ-002, AC-1).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SkillAdapter(Protocol):
    """The pluggable skill-self-improvement contract arcagent talks to (structural).

    The extension forwards *primitive* per-turn signals: each observed tool call
    (``observe``), each turn boundary (``on_turn_end``), the improvement-pass trigger
    (``maybe_improve``), and the retire/revive sweep (``review_lifecycle``). All
    parameters are primitives; a ``session_id`` optionally narrows the scope.
    """

    async def observe(
        self,
        *,
        skill_name: str,
        tool_name: str,
        status: str,
        error_type: str | None,
        session_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        """Record one tool call inside an active skill-usage span.

        ``args`` is the raw tool-call argument dict (REQ-117). arcagent only forwards;
        whether args are scrubbed, hashed, or persisted is the adapter's decision.
        """
        ...

    async def on_turn_end(self, *, turn: int, outcome: str, session_id: str | None = None) -> None:
        """Close the active span at turn end; accrue usage statistics."""
        ...

    async def maybe_improve(self, *, insight: str = "", session_id: str | None = None) -> None:
        """Trigger a bounded, eval-gated improvement pass when usage warrants it.

        ``insight`` is optional Brain-derived recall/insight text when a memory ``Brain``
        is active (the memory module's ``agent:pre_respond`` hook retrieves it); empty
        otherwise, and the improver works fully memory-less (REQ-060).
        """
        ...

    async def review_lifecycle(self, *, turn: int) -> None:
        """Run the retire/revive lifecycle sweep on the proactive tick."""
        ...

    async def sweep_suites(self) -> None:
        """Bootstrap golden suites for suite-less skills on the Curator tick (REQ-107)."""
        ...

    def retired_skills(self) -> frozenset[str]:
        """Names of currently-retired skills — excluded from the agent's offering."""
        ...


class NullSkillAdapter:
    """The default no-op adapter: improvement off, zero files, never errors.

    Every method is inert. This is what ``pip install arcagent`` alone runs with —
    the agent works end-to-end, skill improvement is a silent no-op, and nothing is
    persisted (REQ-002, AC-1).
    """

    async def observe(
        self,
        *,
        skill_name: str,
        tool_name: str,
        status: str,
        error_type: str | None,
        session_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def on_turn_end(self, *, turn: int, outcome: str, session_id: str | None = None) -> None:
        return None

    async def maybe_improve(self, *, insight: str = "", session_id: str | None = None) -> None:
        return None

    async def review_lifecycle(self, *, turn: int) -> None:
        return None

    async def sweep_suites(self) -> None:
        return None

    def retired_skills(self) -> frozenset[str]:
        return frozenset()


__all__ = ["NullSkillAdapter", "SkillAdapter"]
