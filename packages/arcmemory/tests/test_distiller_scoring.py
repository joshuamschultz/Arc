"""T-1120 (SPEC-083 COMP-002) — the distiller scores an item at mint time.

RED intent: when the distiller returns a rubric ``personal_score`` for a minted
insight, that score must land on the stored :class:`~arcmemory.types.Insight`.
It fails today because ``distill.mint_insights`` / ``_apply_insight`` never reads
or sets ``personal_score`` — a minted insight is always ``None``-scored, so the
scored assertion below sees ``None`` where it expects ``3``.

Intended contract (for the GREEN implementer, T-1121):

- The distiller's mint payload carries the rubric score on each candidate — the
  score comes back in the SAME structured mint call (SDD COMP-002: "batched into
  the existing distill LLM call ... no extra pass"). The candidate model
  ``arcmemory.distill.InsightCandidate`` gains ``personal_score: int | None = None``.
- ``mint_insights`` propagates ``getattr(cand, "personal_score", None)`` onto the
  minted ``Insight`` so a scored candidate persists its score through the store.
  Enablement (whether the distiller is *asked* to score at all) is governed one
  layer up by the Consolidator per REQ-447; this mint-level contract only says
  "if the mint payload carries a score, persist it; if it doesn't, stay ``None``
  (fail-closed)".

The distiller is an injected structural seam, so this test wires a fake that
returns a scored candidate and asserts the stored insight carries the score —
no real model is called (deterministic).
"""

from __future__ import annotations

from pathlib import Path

from arcmemory import distill
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import InsightCandidate
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Event, Scope


class _ScoredInsightCandidate(InsightCandidate):
    """An insight candidate that also carries the distiller's rubric score.

    Mirrors the field T-1121 must add to ``InsightCandidate`` itself. Kept local
    to the test so the RED run does not depend on the production field existing
    yet — the fake distiller returns these, and ``mint_insights`` must learn to
    read ``personal_score`` off the candidate.
    """

    personal_score: int | None = None


class _FakeMint:
    """A structural stand-in for ``InsightMint`` carrying scored candidates.

    ``mint_insights`` only reads ``.insights``; returning a plain object with
    scored candidates lets the score survive (a real ``InsightMint`` would
    re-validate each item against ``InsightCandidate`` and drop the extra field).
    """

    def __init__(self, insights: list[_ScoredInsightCandidate]) -> None:
        self.insights = insights


class _ScoringDistiller:
    """Fake distiller: its mint payload carries a fixed rubric ``personal_score``."""

    def __init__(self, insights: list[_ScoredInsightCandidate]) -> None:
        self._insights = insights

    async def mint_insights(self, events: list[Event], facts: list[object]) -> _FakeMint:
        return _FakeMint(self._insights)


async def test_mint_persists_the_distiller_rubric_score(
    workspace: Path, db: MemoryDB, config: MemoryConfig, scope: Scope
) -> None:
    """A scored mint candidate lands on the stored insight (RED: today it is None)."""
    graph = WeightedGraph(db, config)
    store = InsightStore(workspace)
    distiller = _ScoringDistiller(
        [
            _ScoredInsightCandidate(
                id="promotable-insight",
                statement="The month-end close runs on the third business day.",
                trigger="a recurring close-timing question",
                personal_score=3,
            )
        ]
    )

    await distill.mint_insights(
        [Event(event_id="e1", scope=scope.key, kind="respond", text="close timing")],
        [],
        distiller=distiller,
        store=store,
        graph=graph,
        scope=scope,
        config=config,
    )

    stored = store.read("promotable-insight")
    assert stored is not None
    assert stored.personal_score == 3


async def test_mint_leaves_unscored_candidate_none_fail_closed(
    workspace: Path, db: MemoryDB, config: MemoryConfig, scope: Scope
) -> None:
    """A candidate with no rubric score stays unscored (``None``) — fail-closed guard.

    A distiller that could not parse a confident score returns a plain candidate
    (no ``personal_score``); the minted insight must NOT invent a default score.
    """
    graph = WeightedGraph(db, config)
    store = InsightStore(workspace)
    distiller = _ScoringDistiller(
        [
            _ScoredInsightCandidate(
                id="unparseable-insight",
                statement="Some low-confidence observation.",
                trigger="an ambiguous situation",
                personal_score=None,
            )
        ]
    )

    await distill.mint_insights(
        [Event(event_id="e1", scope=scope.key, kind="respond", text="ambiguous")],
        [],
        distiller=distiller,
        store=store,
        graph=graph,
        scope=scope,
        config=config,
    )

    stored = store.read("unparseable-insight")
    assert stored is not None
    assert stored.personal_score is None
