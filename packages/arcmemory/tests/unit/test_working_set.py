"""RED — per-session WorkingSet feeds the detectors (SPEC-072 COMP-001).

Two levels:

* the ``WorkingSet`` value object — bounded, decaying, salience-filtered, model-free;
* the REAL ``ArcMemoryBrain.on_moment`` path — a later moment fires ``entity_seen`` on
  an entity named in a PRIOR turn, absent from the current cues/text, because the
  working set kept it in play (REQ-349/350). No embedder/LLM on the path (REQ-361).
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore

_DID = "did:arc:working-set-agent"


def test_working_set_bounds_size() -> None:
    """The set never grows past its configured max, however many cues arrive."""
    from arcmemory.detectors import WorkingSet

    ws = WorkingSet(max_size=2, decay_turns=10)
    members = ws.update(None, ["proteus", "quasar", "rigel", "sirius"])
    assert len(members) <= 2


def test_working_set_decays_by_turn() -> None:
    """A cue not refreshed within ``decay_turns`` turns is dropped (bounded memory)."""
    from arcmemory.detectors import WorkingSet

    ws = WorkingSet(max_size=32, decay_turns=2)
    ws.update(None, ["nebula", "orion"])  # turn 1
    ws.update(None, ["pulsar"])  # turn 2
    members = ws.update(None, ["quasar"])  # turn 3 -> turn-1 cues are now age 2 == decay
    assert "nebula" not in members
    assert "orion" not in members
    assert "pulsar" in members
    assert "quasar" in members


def test_working_set_salience_drops_trivial_tokens() -> None:
    """Generic/too-short tokens are filtered; a proper-noun-ish cue survives."""
    from arcmemory.detectors import WorkingSet

    ws = WorkingSet(max_size=32, decay_turns=10)
    members = ws.update(None, ["a", "the", "Andromeda"])
    assert "andromeda" in [m.lower() for m in members]
    assert "a" not in members


async def test_prior_turn_entity_surfaces_from_working_set(workspace: Path) -> None:
    """A known entity named a turn ago surfaces even when the latest message omits it."""
    brain = ArcMemoryBrain(workspace, _DID)  # working_set_enabled defaults on
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=_DID)
    store.write_fact(
        "nebula-reactor",
        "kind",
        "working-set-candidate",
        name="Nebula Reactor",
        entity_type="thing",
        classification="unclassified",
    )

    # Turn 1: a topic_shift names nebula. With no prior baseline it does not FIRE a
    # recall (so nebula is never surfaced/deduped now), but its cue enters the set.
    first = await brain.on_moment(
        "topic_shift", cues=["nebula"], text="let us discuss nebula", clearance="unclassified"
    )
    assert first == ""

    # Turn 2: the message is about something else; nebula is in neither cues nor text.
    second = await brain.on_moment(
        "entity_seen",
        cues=["sparrow"],
        text="switching over to the sparrow question now",
        clearance="unclassified",
    )

    assert "nebula" in second.lower(), "prior-turn working-set entity should surface"


def test_decision_point_fires_on_working_set_when_cueless() -> None:
    """A cue-less pre_plan decision_point recalls on the entities in the working set."""
    from dataclasses import dataclass, field

    from arcmemory.detectors import evaluate_moment

    @dataclass
    class _SS:
        prior_cues: list[str] = field(default_factory=list)
        working_set: list[str] = field(default_factory=list)

    class _PoisonStore:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("decision_point must not touch the store")

    fired = evaluate_moment(
        "decision_point",
        cues=[],
        text="",
        session_state=_SS(working_set=["nebula", "orion"]),
        store=_PoisonStore(),
    )
    assert fired.fire is True
    assert fired.query_cues == ["nebula", "orion"]

    quiet = evaluate_moment(
        "decision_point", cues=[], text="", session_state=_SS(), store=_PoisonStore()
    )
    assert quiet.fire is False


async def test_working_set_disabled_restores_prior_behavior(workspace: Path) -> None:
    """With the working set off, a prior-turn entity does NOT leak into a later turn."""
    from arcmemory.config import MemoryConfig

    cfg = MemoryConfig(working_set_enabled=False)
    brain = ArcMemoryBrain(workspace, _DID, config=cfg)
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=_DID)
    store.write_fact(
        "nebula-reactor",
        "kind",
        "working-set-candidate",
        name="Nebula Reactor",
        entity_type="thing",
        classification="unclassified",
    )

    await brain.on_moment(
        "topic_shift", cues=["nebula"], text="let us discuss nebula", clearance="unclassified"
    )
    second = await brain.on_moment(
        "entity_seen",
        cues=["sparrow"],
        text="switching over to the sparrow question now",
        clearance="unclassified",
    )

    assert "nebula" not in second.lower()
