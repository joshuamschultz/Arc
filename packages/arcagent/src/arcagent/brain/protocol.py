"""The ``Brain`` seam — arcagent's memory boundary (SPEC-041 / SPEC-047).

arcagent ships **memory-less by default**. It depends on no memory package: this
module defines a *structural* ``Brain`` Protocol (primitives only, so an
implementation need not import arcagent) and a no-op :class:`NullBrain` that is the
default. A user who wants memory ``pip install``s a memory backend (whose Brain class
satisfies this Protocol structurally) or plugs in their own compatible class — selected
by config (see :mod:`arcagent.brain.select`).

The Protocol speaks only ``str``/``int``/``float`` at the boundary so that:

* arcagent never imports a memory type, and
* any Brain (a memory backend, a SaaS provider adapter, a fake) is a drop-in.

With :class:`NullBrain` active, memory is a silent no-op — capture does nothing,
recall is empty, consolidation is empty — and **no memory files are ever written**.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class Brain(Protocol):
    """The pluggable memory contract arcagent talks to (structural).

    Three memory speeds (fast capture, query-conditioned retrieve, slow
    consolidate) plus an index rebuild. All parameters are primitives; a
    ``session_id`` optionally narrows the shared-nothing scope.
    """

    async def capture(
        self,
        text: str,
        *,
        kind: str = "observation",
        salience: float = 0.0,
        classification: str = "unclassified",
        session_id: str | None = None,
    ) -> None:
        """Fast, zero-LLM capture of one observation."""
        ...

    async def retrieve(
        self,
        query: str,
        *,
        clearance: str = "unclassified",
        top_k: int = 5,
        budget: int = 1024,
        summary: str = "",
        cues: list[str] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Query-conditioned, clearance-gated recall; returns injectable text.

        ``summary`` is the turn's already-computed abstraction (reused, no new LLM
        call) and ``cues`` its active concept/entity nodes — both optional, so a Brain
        that ignores them still satisfies the contract. They feed the analogical
        (structural) recall channel so a different-domain turn can still match a stored
        abstraction without sharing surface tokens.
        """
        ...

    async def consolidate(self, *, session_id: str | None = None) -> Mapping[str, object]:
        """Slow "sleep" consolidation; returns mutation counts + ``episode_summary``."""
        ...

    async def rebuild_index(self, *, session_id: str | None = None) -> None:
        """Re-derive the disposable indices from the source-of-truth files."""
        ...


class NullBrain:
    """The default no-op Brain: memory off, zero files, never errors.

    Every method is inert. This is what ``pip install arcagent`` alone runs with —
    the agent works end-to-end, memory is a silent no-op, and nothing is persisted.
    """

    async def capture(
        self,
        text: str,
        *,
        kind: str = "observation",
        salience: float = 0.0,
        classification: str = "unclassified",
        session_id: str | None = None,
    ) -> None:
        return None

    async def retrieve(
        self,
        query: str,
        *,
        clearance: str = "unclassified",
        top_k: int = 5,
        budget: int = 1024,
        summary: str = "",
        cues: list[str] | None = None,
        session_id: str | None = None,
    ) -> str:
        return ""

    async def consolidate(self, *, session_id: str | None = None) -> Mapping[str, object]:
        return {}

    async def rebuild_index(self, *, session_id: str | None = None) -> None:
        return None


__all__ = ["Brain", "NullBrain"]
