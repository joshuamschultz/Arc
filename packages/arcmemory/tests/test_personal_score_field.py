"""T-1114 (SPEC-083 COMP-001) — ``personal_score`` on promotable records.

RED intent: the promotable memory records (insight, procedure, entity) must
carry a persisted ``personal_score: int | None`` that defaults to ``None`` (the
fail-closed sentinel, treated as >=8 downstream) and round-trips through
``store.write`` -> disk -> ``store.read``.

These fail today because the field does not exist on the record models in
``arcmemory.types`` and is not rendered/parsed by the stores in
``arcmemory.stores``. The default-``None`` assertions raise ``AttributeError``
(field absent); the round-trip assertions prove the score is persisted on the
item, not dropped.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.types import Entity, Insight, Procedure


def test_insight_personal_score_defaults_to_none() -> None:
    """A freshly minted insight is unscored (``None``) — the fail-closed sentinel."""
    insight = Insight(id="unscored", statement="a claim", trigger="a situation")

    assert insight.personal_score is None


def test_procedure_personal_score_defaults_to_none() -> None:
    """A freshly minted procedure is unscored (``None``)."""
    procedure = Procedure(slug="how-to", title="How To")

    assert procedure.personal_score is None


def test_entity_personal_score_defaults_to_none() -> None:
    """A freshly minted entity is unscored (``None``)."""
    entity = Entity(slug="acme", name="Acme Corp")

    assert entity.personal_score is None


def test_insight_personal_score_round_trips_through_store(workspace: Path) -> None:
    """An explicit score survives write -> disk -> read on the insight store."""
    store = InsightStore(workspace)
    store.write(
        Insight(
            id="promotable-insight",
            statement="company knowledge",
            trigger="a recurring situation",
            personal_score=3,
        )
    )

    loaded = store.read("promotable-insight")

    assert loaded is not None
    assert loaded.personal_score == 3


def test_procedure_personal_score_round_trips_through_store(workspace: Path) -> None:
    """An explicit score survives write -> disk -> read on the procedural store."""
    store = ProceduralStore(workspace)
    store.write(
        Procedure(
            slug="quote-a-customer",
            title="Quote a customer",
            when_to_use="when a customer asks for a price",
            steps=["gather requirements", "apply the price book"],
            personal_score=6,
        )
    )

    loaded = store.read("quote-a-customer")

    assert loaded is not None
    assert loaded.personal_score == 6
