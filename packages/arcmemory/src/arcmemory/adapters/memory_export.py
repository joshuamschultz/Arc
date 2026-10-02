"""Consolidated-memory exporter — the promotion source authority (SPEC-083 COMP-021).

``FleetSharedKnowledgeService.promote`` pulls the bytes it publishes from this
exporter, so the shared side re-verifies them itself. Each export re-reads the
card from disk and renders it with :func:`render_candidate`, so the published
bytes are exactly the bytes the classifier saw — or the digest no longer matches.

Only the owning agent may export (``access.caller_did == agent_did``, ASI03). The
shared label is the CARD's own stored label (alpha-2 Q16-a), never the caller's
clearance written over it: a card at or below the clearance is exported under its
own label (a "declassified-at-source share"; ``label_from_card`` says the label
came from the card), a card above it is refused, and a missing or unknown label
is the clearance (fail upward, never read as unclassified). The shared store
honours a below-clearance label only from this attested source.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from arctrust.classification import parse_classification

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.promotion.render import (
    PromotionText,
    render_candidate,
    require_card_id,
    shared_label,
)
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Entity, Insight, Procedure


class _Access(Protocol):
    caller_did: str
    clearance: str


class _InsightReader(Protocol):
    def read(self, insight_id: str) -> Insight | None: ...


class _ProcedureReader(Protocol):
    def read(self, slug: str) -> Procedure | None: ...


class _EntityReader(Protocol):
    def read(self, slug: str) -> Entity | None: ...


class ConsolidatedStores(Protocol):
    """The three consolidated stores the exporter reads from."""

    @property
    def insights(self) -> _InsightReader: ...

    @property
    def procedures(self) -> _ProcedureReader: ...

    @property
    def entities(self) -> _EntityReader: ...


@dataclass(frozen=True)
class _Reference:
    scope: str
    identifier: str
    digest: str


@dataclass(frozen=True)
class _PromotionSource:
    reference: _Reference
    digest: str
    content: str
    classification: str
    title: str
    tags: tuple[str, ...]
    document_type: str
    #: True: the label is the card's own stored label, read by this exporter. The
    #: shared side lets only such a source publish below the writer's clearance.
    label_from_card: bool = False


@dataclass(frozen=True)
class _WorkspaceStores:
    insights: InsightStore
    procedures: ProceduralStore
    entities: SemanticStore


class ConsolidatedMemoryExporter:
    """Export insight / procedure / entity cards for promotion to shared knowledge."""

    @classmethod
    def for_workspace(cls, workspace: Path, agent_did: str) -> ConsolidatedMemoryExporter:
        """The exporter over one agent's on-disk card stores.

        For an integrator that must hand a publisher an exporter without reaching
        into store construction. Card reads never touch the index graph, and
        :class:`MemoryDB` connects lazily, so this opens no database.
        """
        root = Path(workspace)
        stores = _WorkspaceStores(
            insights=InsightStore(root),
            procedures=ProceduralStore(root),
            entities=SemanticStore(root, WeightedGraph(MemoryDB(root)), scope=agent_did),
        )
        return cls(root, agent_did, stores)

    def __init__(self, workspace: Path, agent_did: str, stores: ConsolidatedStores) -> None:
        # ``workspace`` is the agent home the stores are rooted at (SDD COMP-021
        # signature). The exporter reads only through the stores, which confine
        # every path to that home, so it keeps no second path of its own.
        del workspace
        self._agent_did = agent_did
        self._stores = stores

    async def export_for_promotion(self, reference: str, access: _Access) -> _PromotionSource:
        """Re-read and render ``reference`` (``<kind>:<id>``) under its own label.

        Raises ``PermissionError`` for a foreign caller or a card whose label is
        above the caller's clearance, ``ValueError`` for a non-promotable or malformed
        reference, and ``LookupError`` for a missing card.
        """
        if access.caller_did != self._agent_did:
            raise PermissionError("consolidated memory belongs to a different agent")
        clearance = parse_classification(access.clearance, strict=True)
        text = await asyncio.to_thread(self._render, reference)
        label = shared_label(text.classification, clearance)
        if label is None:
            raise PermissionError("memory card label is above the caller's clearance")
        return _PromotionSource(
            reference=_Reference("personal", reference, text.content_sha256),
            digest=text.content_sha256,
            content=text.content,
            classification=label.name,
            title=text.title,
            tags=(),
            document_type=text.item_kind,
            label_from_card=True,
        )

    def _render(self, reference: str) -> PromotionText:
        kind, _, item_id = reference.partition(":")
        require_card_id(item_id)
        item: Insight | Procedure | Entity | None
        if kind == "insight":
            item = self._stores.insights.read(item_id)
        elif kind == "procedure":
            item = self._stores.procedures.read(item_id)
        elif kind == "entity":
            item = self._stores.entities.read(item_id)
        else:
            raise ValueError(f"memory kind {kind!r} is not promotable")
        if item is None:
            raise LookupError(f"no {kind} card named {item_id!r}")
        return render_candidate(item)


__all__ = ["ConsolidatedMemoryExporter", "ConsolidatedStores"]
