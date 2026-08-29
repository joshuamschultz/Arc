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
        index: bool = True,
    ) -> str:
        """Query-conditioned, clearance-gated recall; returns injectable text.

        ``summary`` is the turn's already-computed abstraction (reused, no new LLM
        call) and ``cues`` its active concept/entity nodes — both optional, so a Brain
        that ignores them still satisfies the contract. They feed the analogical
        (structural) recall channel so a different-domain turn can still match a stored
        abstraction without sharing surface tokens.

        ``index`` gates the EXPENSIVE embed half of the pre-search corpus (re)index.
        The agent recall path passes ``index=False`` so a turn embeds only its
        query, never the corpus — embedding is the background maintainer's job
        (:meth:`refresh_index`). A conforming Brain still writes the CHEAP lexical
        half (no embedder needed) regardless of ``index``, so a just-captured item
        is searchable the same turn it lands; a Brain that keeps no index at all
        simply ignores the parameter.
        """
        ...

    async def consolidate(self, *, session_id: str | None = None) -> Mapping[str, object]:
        """Slow "sleep" consolidation; returns mutation counts + ``episode_summary``."""
        ...

    async def holdings(self, *, limit: int = 200, session_id: str | None = None) -> list[str]:
        """Publishable pointers to durable knowledge held — proper nouns, never bodies.

        The seam that lets a teammate find the agent holding a topic *without waking
        it*: a channel router ranks published pointers, and that index has to be
        seedable from what memory already holds, not only from what is filed after
        the index was invented. Returns only what the agent may expose (unclassified).
        """
        ...

    async def rebuild_index(self, *, session_id: str | None = None) -> None:
        """Re-derive the disposable indices from the source-of-truth files."""
        ...

    async def refresh_index(self, *, session_id: str | None = None) -> None:
        """Incrementally index changed chunks off the turn path (background maintainer).

        The counterpart to ``retrieve(index=False)``: the recall hot path never
        embeds the corpus, and this refresh does, in the background. A Brain with no
        index no-ops it.
        """
        ...

    async def list_procedures(self, *, session_id: str | None = None) -> str:
        """The procedure index: every playbook's trigger, WITHOUT its steps.

        Procedures are how the operator wants work done, and a fleet accumulates
        dozens whose full step lists will not fit in a turn. Listing triggers only
        is what makes "is there already a way we do this?" an affordable question —
        without it the agent answers from a skill and the playbook is never reached.
        """
        ...

    async def get_procedure(self, slug: str, *, session_id: str | None = None) -> str:
        """One procedure in full, and RECORD that it was used.

        Reading is the use: it is the only moment the system learns which playbooks
        earn their keep. Counting writes instead left a live store of 36 procedures
        showing no evidence that any had ever been reached for.
        """
        ...

    async def on_moment(
        self,
        kind: str,
        *,
        cues: list[str] | None = None,
        text: str = "",
        clearance: str = "unclassified",
        top_k: int = 3,
        budget: int = 512,
        session_id: str | None = None,
        index: bool = True,
    ) -> str:
        """Detected-loop-moment signal; returns injectable text or ``""``.

        ``kind`` names where the loop is (``task_start`` / ``entity_seen`` /
        ``topic_shift`` / ``decision_point``); the Brain alone decides whether
        that moment warrants an unprompted recall and, if so, returns bounded,
        clearance-gated text ready to inject. An unfired moment, an unknown
        ``kind``, or nothing worth surfacing all return ``""`` — never raise.
        Primitives only, so arcmemory need not import arcagent to satisfy this.
        """
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
        index: bool = True,
    ) -> str:
        return ""

    async def consolidate(self, *, session_id: str | None = None) -> Mapping[str, object]:
        return {}

    async def holdings(self, *, limit: int = 200, session_id: str | None = None) -> list[str]:
        return []

    async def rebuild_index(self, *, session_id: str | None = None) -> None:
        return None

    async def refresh_index(self, *, session_id: str | None = None) -> None:
        return None

    async def list_procedures(self, *, session_id: str | None = None) -> str:
        return ""

    async def get_procedure(self, slug: str, *, session_id: str | None = None) -> str:
        return ""

    async def on_moment(
        self,
        kind: str,
        *,
        cues: list[str] | None = None,
        text: str = "",
        clearance: str = "unclassified",
        top_k: int = 3,
        budget: int = 512,
        session_id: str | None = None,
        index: bool = True,
    ) -> str:
        return ""


__all__ = ["Brain", "NullBrain"]
