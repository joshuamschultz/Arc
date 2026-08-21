"""Tests for the deterministic detected-moment detector registry (SPEC-071, COMP-004).

``arcmemory.detectors`` decides, for one of four fixed ``kind`` values
(``task_start``, ``entity_seen``, ``topic_shift``, ``decision_point``), whether a
proactive recall should fire and — if so — which cues to query with. This is the
gate in front of ``ArcMemoryBrain.on_moment`` (COMP-005): it must be cheap and
100% deterministic, because it runs on every user turn / task start / plan step.
No embedder, no LLM — that is a hard non-negotiable (REQ-342-adjacent), pinned
here by the "no-LLM guarantee" tests at the bottom of this file.

Pinned API (the builder must match this shape exactly):

    DETECTORS: dict[str, DetectorFn]   # one entry per valid kind, no shared branching
    Decision(fire: bool, query_cues: list[str])   # frozen
    evaluate_moment(
        kind: str,
        *,
        cues: list[str],
        text: str,
        session_state: <duck type with .prior_cues: list[str]>,
        store: <duck type with .slugs() -> list[str], .read(slug) -> Entity | None>,
    ) -> Decision

Pinned behavioral contract (design decisions made here, for the builder to match):

* task_start / decision_point: fire on presence (cues non-empty OR text non-empty).
  ``query_cues`` echoes the given cues verbatim when cues are supplied; when only
  ``text`` is supplied, cues are derived from it (non-empty, deterministic — same
  text always yields the same query_cues, since nothing here may sample a model).
* entity_seen: fires only when at least one cue exactly matches (by literal
  string, matching how ``SemanticStore.write_fact(..., name=...)`` stored it) a
  known entity card's ``name``. ``query_cues`` echoes the full input ``cues`` on
  fire, ``[]`` on no-fire.
* topic_shift: fires on LOW cue-set overlap with ``session_state.prior_cues``.
  With ZERO prior cues there is no baseline to compare against, so this is
  pinned as NO FIRE (a session's first turn is not itself a "shift") — this is
  a deliberate design choice flagged for the orchestrator, not an obvious
  reading of the spec.
* Unknown kind: no fire, empty query_cues, no exception — fail-open per the
  cross-seam contract in SDD COMP-004.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from arcmemory.detectors import DETECTORS, evaluate_moment
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore

_SCOPE = "did:arc:test-agent"


@dataclass
class FakeSessionState:
    """Session-scoped seam: the prior turn's cues, for topic_shift comparison.

    Deliberately a plain duck-typed object (no import from arcmemory needed) —
    proves the detector depends on an attribute contract, not a concrete class.
    """

    prior_cues: list[str] = field(default_factory=list)


class LockedStore:
    """Wraps a real SemanticStore but blows up on any attribute besides slugs/read.

    Proves the entity_seen detector's contract surface is exactly
    ``{slugs, read}`` — nothing else is ever reached, so there is no path by
    which an embedder-backed store implementation could get its embedding
    method called from inside a detector.
    """

    def __init__(self, inner: SemanticStore) -> None:
        self._inner = inner

    def slugs(self) -> list[str]:
        return self._inner.slugs()

    def read(self, slug: str) -> Any:
        return self._inner.read(slug)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"detector reached for store.{name} — not a deterministic surface")


class PoisonObject:
    """Any attribute access raises — proves a detector never even LOOKS at this object.

    Used as a stand-in for both "an object that happens to be an
    embedder/model" and "a store a given kind has no business touching."
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f".{name} must never be touched by a deterministic detector")


def _store(workspace: Any, db: Any) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope=_SCOPE)


# --- registry shape (COMP-004: one detector per kind, no shared branching) --


def test_registry_has_exactly_one_detector_per_valid_kind() -> None:
    assert set(DETECTORS.keys()) == {
        "task_start",
        "entity_seen",
        "topic_shift",
        "decision_point",
    }


def test_unknown_kind_is_absent_from_the_registry() -> None:
    assert "sneeze" not in DETECTORS


# --- task_start ---------------------------------------------------------


def test_task_start_fires_when_cues_present(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "task_start",
        cues=["deploy", "auth-service"],
        text="",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is True
    assert decision.query_cues == ["deploy", "auth-service"]


def test_task_start_fires_on_text_alone_and_is_deterministic(workspace: Any, db: Any) -> None:
    store = _store(workspace, db)
    first = evaluate_moment(
        "task_start",
        cues=[],
        text="Let's start the migration task",
        session_state=FakeSessionState(),
        store=store,
    )
    second = evaluate_moment(
        "task_start",
        cues=[],
        text="Let's start the migration task",
        session_state=FakeSessionState(),
        store=store,
    )
    assert first.fire is True
    assert first.query_cues != []  # derived from text, not left empty
    assert first.query_cues == second.query_cues  # same input -> same output, always


def test_task_start_does_not_fire_on_empty_cues_and_text(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "task_start",
        cues=[],
        text="",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is False
    assert decision.query_cues == []


# --- decision_point -------------------------------------------------------


def test_decision_point_fires_on_cue_presence(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "decision_point",
        cues=["choose-provider"],
        text="",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is True
    assert decision.query_cues == ["choose-provider"]


def test_decision_point_fires_on_text_presence(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "decision_point",
        cues=[],
        text="Should we use Postgres or SQLite here?",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is True
    assert decision.query_cues != []


def test_decision_point_does_not_fire_on_empty_cues_and_text(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "decision_point",
        cues=[],
        text="",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is False
    assert decision.query_cues == []


# --- entity_seen ----------------------------------------------------------


def test_entity_seen_fires_when_a_cue_matches_a_known_entity_name(
    workspace: Any, db: Any
) -> None:
    store = _store(workspace, db)
    store.write_fact("alice", "role", "engineer", name="Alice", entity_type="person")

    decision = evaluate_moment(
        "entity_seen",
        cues=["Alice", "unrelated-topic"],
        text="",
        session_state=FakeSessionState(),
        store=store,
    )
    assert decision.fire is True
    assert decision.query_cues == ["Alice", "unrelated-topic"]


def test_entity_seen_does_not_fire_when_no_cue_matches_a_known_entity(
    workspace: Any, db: Any
) -> None:
    store = _store(workspace, db)
    store.write_fact("alice", "role", "engineer", name="Alice", entity_type="person")

    decision = evaluate_moment(
        "entity_seen",
        cues=["Bob", "unrelated"],
        text="",
        session_state=FakeSessionState(),
        store=store,
    )
    assert decision.fire is False
    assert decision.query_cues == []


def test_entity_seen_does_not_fire_against_an_empty_store(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "entity_seen",
        cues=["Alice"],
        text="",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is False
    assert decision.query_cues == []


# --- topic_shift -----------------------------------------------------------


def test_topic_shift_fires_on_low_overlap_with_prior_turn_cues(workspace: Any, db: Any) -> None:
    session_state = FakeSessionState(prior_cues=["deploy", "auth-service", "migration"])
    decision = evaluate_moment(
        "topic_shift",
        cues=["billing", "invoice", "refund"],
        text="",
        session_state=session_state,
        store=_store(workspace, db),
    )
    assert decision.fire is True
    assert decision.query_cues == ["billing", "invoice", "refund"]


def test_topic_shift_does_not_fire_on_high_overlap_with_prior_turn_cues(
    workspace: Any, db: Any
) -> None:
    session_state = FakeSessionState(prior_cues=["deploy", "auth-service", "migration"])
    decision = evaluate_moment(
        "topic_shift",
        cues=["deploy", "auth-service"],
        text="",
        session_state=session_state,
        store=_store(workspace, db),
    )
    assert decision.fire is False
    assert decision.query_cues == []


def test_topic_shift_does_not_fire_with_no_prior_turn_cues(workspace: Any, db: Any) -> None:
    """No baseline to compare against — a session's first turn is not a 'shift.'"""
    session_state = FakeSessionState(prior_cues=[])
    decision = evaluate_moment(
        "topic_shift",
        cues=["billing", "invoice"],
        text="",
        session_state=session_state,
        store=_store(workspace, db),
    )
    assert decision.fire is False
    assert decision.query_cues == []


# --- unknown kind -----------------------------------------------------------


def test_unknown_kind_does_not_fire_and_raises_nothing(workspace: Any, db: Any) -> None:
    decision = evaluate_moment(
        "sneeze",
        cues=["achoo"],
        text="",
        session_state=FakeSessionState(),
        store=_store(workspace, db),
    )
    assert decision.fire is False
    assert decision.query_cues == []


# --- no-LLM guarantee -------------------------------------------------------


def test_evaluate_moment_signature_accepts_no_embedder_model_or_llm_param() -> None:
    """The decision path cannot call a model it was never even handed."""
    params = inspect.signature(evaluate_moment).parameters
    for forbidden in ("embedder", "model", "llm"):
        assert forbidden not in params


def test_task_start_never_touches_the_store_object() -> None:
    decision = evaluate_moment(
        "task_start",
        cues=["deploy"],
        text="",
        session_state=FakeSessionState(),
        store=PoisonObject(),
    )
    assert decision.fire is True


def test_decision_point_never_touches_the_store_object() -> None:
    decision = evaluate_moment(
        "decision_point",
        cues=["choose"],
        text="",
        session_state=FakeSessionState(),
        store=PoisonObject(),
    )
    assert decision.fire is True


def test_topic_shift_never_touches_the_store_object() -> None:
    session_state = FakeSessionState(prior_cues=["a", "b"])
    decision = evaluate_moment(
        "topic_shift",
        cues=["c", "d"],
        text="",
        session_state=session_state,
        store=PoisonObject(),
    )
    assert decision.fire is True


def test_entity_seen_touches_only_slugs_and_read_on_the_store(workspace: Any, db: Any) -> None:
    real_store = _store(workspace, db)
    real_store.write_fact("alice", "role", "engineer", name="Alice", entity_type="person")
    locked = LockedStore(real_store)

    decision = evaluate_moment(
        "entity_seen",
        cues=["Alice"],
        text="",
        session_state=FakeSessionState(),
        store=locked,
    )
    assert decision.fire is True
