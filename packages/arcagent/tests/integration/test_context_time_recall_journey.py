"""SPEC-072 — one real-path journey per capability (only the LLM wire faked).

Reuses the SPEC-071 boot harness (real identity, real signed memory bundle, real bus,
real ArcMemoryBrain, embedder off → BM25+graph): the links are proven CONNECTED on the
real path, not just in isolation.

* (A) Working set: an entity named a PRIOR turn surfaces a NET-NEW card the current
  message's (empty-query) recall would never pull. Falsified with the toggle off.
* (B) Mid-loop: a decision-point recall, routed through the real subscriber, reaches the
  model between loop steps via the real ``ContextManager.transform_context``. Falsified
  with ``proactive_decision_point`` off.
* (C) Temporal: a superseded fact shows current + marked-old in recall; the what-changed
  timeline returns ordered dated changes; a classified change stays gated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.arcagent.tests.integration.test_proactive_recall_journey import (
    _booted,
    _config,
    _deployment,
    _install,
    _memory_brain,
    _proactive_recall_section,
    _seed_entity,
    _seed_fact,
)

from arcagent.core import midloop_recall


async def _emit_moment(agent: object, **payload: object) -> None:
    await agent._bus.emit("agent:moment", payload)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# (A) Working-set recall — a prior-turn entity surfaces a net-new card
# --------------------------------------------------------------------------


async def test_working_set_surfaces_prior_turn_entity_as_net_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, _rec):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Vortex")
        await _seed_fact(brain, "Vortex deployment note: the marker is WS_MARKER_9.")

        # Turn 1 names Vortex via a topic_shift that does NOT fire (no prior baseline),
        # so the card is never surfaced/deduped now — but Vortex enters the working set.
        await _proactive_recall_section(
            agent,
            text="let us switch to vortex",
            cues=["vortex"],
            kind="topic_shift",
            session_id="wsA",
        )
        # Turn 2 is about something else entirely; Vortex is in neither cues nor text.
        # The empty-query drain removes the query-recall confound, so a Vortex card here
        # can ONLY have come from the working set (net-new relative to the literal message).
        recall = await _proactive_recall_section(
            agent,
            text="now about the sparrow schedule",
            cues=["sparrow"],
            kind="entity_seen",
            session_id="wsA",
        )

    assert "Vortex" in recall, (
        "the prior-turn working-set entity did not surface a net-new card on a later turn"
    )


async def test_working_set_disabled_yields_no_prior_turn_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsify (A): with the working set off, the prior-turn entity does not leak."""
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    cfg = _config(deployment)
    cfg.modules["memory"].config.update({"dynamics": {"working_set_enabled": False}})

    async with _booted(deployment, cfg) as (agent, _rec):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Vortex")
        await _seed_fact(brain, "Vortex deployment note: the marker is WS_MARKER_9.")

        await _proactive_recall_section(
            agent,
            text="let us switch to vortex",
            cues=["vortex"],
            kind="topic_shift",
            session_id="wsB",
        )
        recall = await _proactive_recall_section(
            agent,
            text="now about the sparrow schedule",
            cues=["sparrow"],
            kind="entity_seen",
            session_id="wsB",
        )

    assert "Vortex" not in recall


# --------------------------------------------------------------------------
# (B) Mid-loop decision-point recall reaches the model via transform_context
# --------------------------------------------------------------------------


async def test_decision_point_recall_reaches_model_midloop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    cfg = _config(deployment)
    cfg.modules["memory"].config.update({"proactive_decision_point": True})

    async with _booted(deployment, cfg) as (agent, _rec):
        brain = _memory_brain(agent)
        did = agent._identity.did  # type: ignore[attr-defined]
        midloop_recall.drain(did)  # clean slate
        _seed_entity(agent, brain, "Comet")
        await _seed_fact(brain, "Comet decision note: the pitfall marker is DP_MARKER_7.")

        # Seed the working set with Comet via a non-firing topic_shift, then a cue-less
        # pre_plan decision point fires on the working set and routes to the mid-loop buffer.
        await _emit_moment(
            agent, kind="topic_shift", cues=["comet"], text="turning to comet", session_id="dp1"
        )
        await _emit_moment(
            agent, kind="decision_point", point="pre_plan", cues=[], text="", session_id="dp1"
        )

        # The real per-turn hook appends the staged block before the next model call.
        messages = [{"role": "user", "content": "proceeding"}]
        transformed = agent._context.transform_context(list(messages))  # type: ignore[attr-defined]

    assert transformed[: len(messages)] == messages  # append-only prefix preserved
    assert len(transformed) == len(messages) + 1
    assert "Comet" in str(transformed[-1]), (
        "the decision-point recall never reached the model via the mid-loop channel"
    )


async def test_decision_point_disabled_stages_nothing_midloop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsify (B): with proactive_decision_point off, nothing is staged mid-loop."""
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, _rec):
        brain = _memory_brain(agent)
        did = agent._identity.did  # type: ignore[attr-defined]
        midloop_recall.drain(did)
        _seed_entity(agent, brain, "Comet")
        await _seed_fact(brain, "Comet decision note: the pitfall marker is DP_MARKER_7.")

        await _emit_moment(
            agent, kind="topic_shift", cues=["comet"], text="turning to comet", session_id="dp2"
        )
        await _emit_moment(
            agent, kind="decision_point", point="pre_plan", cues=[], text="", session_id="dp2"
        )
        messages = [{"role": "user", "content": "proceeding"}]
        transformed = agent._context.transform_context(list(messages))  # type: ignore[attr-defined]

    assert transformed == messages  # decision point ignored → nothing appended


# --------------------------------------------------------------------------
# (C) Temporal — supersession + what-changed timeline, gated
# --------------------------------------------------------------------------


async def test_superseded_fact_shows_current_and_marked_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, _rec):
        brain = _memory_brain(agent)
        from arcmemory.index.graph import WeightedGraph
        from arcmemory.stores.semantic import SemanticStore, superseded_view

        did = agent._identity.did  # type: ignore[attr-defined]
        store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=did)
        store.write_fact("northwind", "status", "OLD_ACTIVE", name="Northwind", entity_type="org")
        store.write_fact(
            "northwind", "status", "NEW_WOUND_DOWN", name="Northwind", entity_type="org"
        )
        entity = store.read("northwind")

    assert entity is not None
    view = superseded_view(entity)
    assert any(
        "NEW_WOUND_DOWN" in v and "OLD_ACTIVE" in v and "superseded" in v.lower() for v in view
    )
    # The old value is retained on disk (mark-not-delete).
    fact = next(f for f in entity.facts if f.predicate == "status")
    assert fact.value == "NEW_WOUND_DOWN" and fact.was_value == "OLD_ACTIVE"


async def test_what_changed_timeline_ordered_and_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, _rec):
        brain = _memory_brain(agent)
        from arcmemory.stores.events import EventStore
        from arcmemory.timeline import read_timeline
        from arcmemory.types import TimeWindow

        ws = brain._workspace
        events = EventStore(ws)
        events.upsert("acme-signed", "Acme signed", date="2026-08-10", outcome="closed won")
        events.upsert("beta-ship", "Beta ship", date="2026-08-18", outcome="shipped")
        events.upsert(
            "op-eclipse", "Op Eclipse", date="2026-08-14", outcome="done", classification="SECRET"
        )

        window = TimeWindow(start="2026-08-01", end="2026-08-31")
        entries = read_timeline(ws, window=window, clearance="unclassified")

    dates = [e.date for e in entries]
    assert dates == sorted(dates), "timeline is not chronological"
    blob = " ".join(e.summary for e in entries)
    assert "Acme signed" in blob and "Beta ship" in blob
    assert "Eclipse" not in blob, "a SECRET change leaked to an unclassified reader"
