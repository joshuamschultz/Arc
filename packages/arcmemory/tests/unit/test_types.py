"""T-003 — typed models round-trip and carry the SDD fields."""

from __future__ import annotations

from arcmemory.types import (
    Bundle,
    Confidence,
    ConsolidationResult,
    Entity,
    Event,
    Fact,
    Insight,
    Procedure,
    Recall,
    Scope,
    Situation,
)


def test_scope_key_isolation() -> None:
    assert Scope(agent_did="did:a").key == "did:a"
    assert Scope(agent_did="did:a", session_id="s1").key == "did:a:s1"


def test_event_round_trip() -> None:
    ev = Event(event_id="e1", scope="did:a", kind="tool", text="hi", entities=["alice"])
    assert Event.model_validate(ev.model_dump()) == ev


def test_fact_and_entity_round_trip() -> None:
    fact = Fact(predicate="works_at", value="Acme", confidence=0.9, was_value="Beta")
    ent = Entity(slug="alice", name="Alice", facts=[fact], links_to=["[[acme]]"])
    assert Entity.model_validate(ent.model_dump()) == ent


def test_insight_carries_trigger_cues_instances() -> None:
    ins = Insight(
        id="i1",
        statement="producers unwired",
        trigger="predicate exists but producer never traced -> silent no-op",
        cues=["claims-property", "predicate-without-producer"],
        instances=["event:1", "event:2"],
        confidence=0.8,
        salience=0.5,
        status=Confidence.KNOWN,
    )
    dumped = ins.model_dump()
    assert Insight.model_validate(dumped) == ins
    assert dumped["status"] == "known"


def test_remaining_models_round_trip() -> None:
    for model in (
        Procedure(slug="p", title="Deploy", steps=["a", "b"], use_count=3),
        Situation(text="now", summary="s", cues=["c1"]),
        Recall(source="x", content="y", score=1.2, confidence=Confidence.GUESSED),
        Bundle(recalls=[], degraded=True, budget=100),
        ConsolidationResult(facts_updated=2, insights_minted=1),
    ):
        assert type(model).model_validate(model.model_dump()) == model
