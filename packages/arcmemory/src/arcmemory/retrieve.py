"""Retrieve orchestration — fuse surface + structural, gate, bound (SDD 4.5).

This is the single, bounded read path (REQ-040..043, 060..062). It is deliberately
*not* an agentic loop: one pass, top-k + token budget, no re-query (LLM10). The
shape mirrors §4.5 exactly::

    surf = surface.search(situation.text)        # vec+bm25+graph RRF (Phase 4)
    stru = structural.match(situation)           # trigger-embed + cue-graph (Phase 6)
    cand = rrf_fuse(surf, stru)                   # REQ-040/051
    cand = confidence_gate(cand)                  # guessed -> "verify first" (REQ-053)
    cand = gate_no_read_up(cand, clearance)       # drop over-clearance (REQ-060)
    return boundary_mark(truncate(cand, k, b))    # data-not-instructions, bounded

The security seam is **reused, not reinvented**: ``gate_no_read_up`` maps each
memory's classification onto the ``arctrust`` ladder and calls
``arctrust.dominates`` — the same no-read-up predicate SPEC-038 established.
arcmemory owns no comparator (see ``tests/architecture``).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from arctrust.audit import AuditSink, NullSink
from arctrust.classification import Classification

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.fusion import rrf_fuse
from arcmemory.index.rebuild import Embedder
from arcmemory.index.structural import Reranker, StructuralIndex
from arcmemory.index.surface import SurfaceIndex
from arcmemory.security import enforce_budget, gate_no_read_up, render_recalls
from arcmemory.types import Bundle, Confidence, Recall, Scope, Situation

_DEFAULT_BUDGET = 1024


class Retriever:
    """One bounded retrieval path over the surface + structural indices for a scope.

    Holds the two channels (Phase 4 surface, Phase 6 structural) and the security
    gate. ``retrieve`` is the ``Brain.retrieve`` contract: single-pass, clearance-
    gated, boundary-marked, budget-bounded.
    """

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        scope: Scope,
        *,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        audit_sink: AuditSink | None = None,
        seed_vocabulary: Iterable[str] | None = None,
    ) -> None:
        self._scope = scope
        self._cfg = config or MemoryConfig()
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._surface = SurfaceIndex(
            db,
            workspace,
            scope,
            config=self._cfg,
            embedder=embedder,
            audit_sink=self._audit,
            seed_vocabulary=seed_vocabulary,
        )
        self._structural = StructuralIndex(
            db,
            workspace,
            scope,
            config=self._cfg,
            embedder=embedder,
            audit_sink=self._audit,
        )

    async def index(self) -> None:
        """Incrementally (re)build both derived indices (content-gated, LLM10)."""
        await self._surface.index_if_needed()
        await self._structural.trigger_index()

    async def retrieve(
        self,
        situation: Situation,
        *,
        clearance: Classification,
        top_k: int = 5,
        budget: int = _DEFAULT_BUDGET,
        reranker: Reranker | None = None,
    ) -> Bundle:
        """Fuse both channels, gate on clearance, and return a bounded bundle."""
        pool = max(top_k * 2, top_k)
        surf = await self._surface.search(situation.text, top_k=pool)
        stru = await self._structural.match(situation, top_k=pool, reranker=reranker)

        fused = _rrf_fuse([surf.recalls, stru.recalls])
        gated = _confidence_gate(fused)
        cleared = gate_no_read_up(
            gated,
            clearance=clearance,
            strict=self._cfg.tier == "federal",
            actor_did=self._scope.agent_did,
            tier=self._cfg.tier,
            audit_sink=self._audit,
        )
        bounded, truncated = enforce_budget(cleared, top_k=top_k, budget=budget)
        return Bundle(
            recalls=bounded,
            degraded=surf.degraded or stru.degraded,
            truncated=truncated,
            budget=budget,
            text=render_recalls(bounded),
        )


def _rrf_fuse(channels: list[list[Recall]]) -> list[Recall]:
    """Reciprocal-rank-fuse the channels into one descending recall list (REQ-040).

    Sources are namespace-disjoint (surface chunk ids vs insight ids), so each recall
    object is carried through once and restamped with its fused score (from the shared
    scale-free ``rrf_fuse``) for the downstream budget/margin logic.
    """
    objects: dict[str, Recall] = {}
    for ranked in channels:
        for recall in ranked:
            objects.setdefault(recall.source, recall)
    fused = rrf_fuse([[recall.source for recall in ranked] for ranked in channels])
    return [objects[source].model_copy(update={"score": score}) for source, score in fused]


def _confidence_gate(recalls: list[Recall]) -> list[Recall]:
    """Flag ``guessed`` recalls "verify first"; ``known`` stay actionable (REQ-053)."""
    return [
        recall.model_copy(update={"verify_first": recall.confidence is Confidence.GUESSED})
        for recall in recalls
    ]


__all__ = ["Retriever"]
