"""SPEC-071 detected-moment proactive recall — one end-to-end journey.

The anti-dead-producer proof for the whole chain. Every earlier task proved one
link in isolation (the emit fires, ``on_moment`` gates, the detector decides,
``inject_recall`` merges, the audit carries a trigger). None proved the links
are *connected* on the real path: a detected moment, over the real module bus,
through the real signed memory bundle and real :class:`~arcmemory.brain.ArcMemoryBrain`,
puts a gated, bounded, deduped card into the recall material the model receives.

So this file boots a real agent the way ``arc module install`` +
``ArcAgent.startup`` do — real identity, real operator key, real signed bundle,
real capability scan, real tool registry, real bus, real turn dispatch, real
brain (embedder off, BM25 + graph degrade). **Only the LLM wire is stubbed**,
and the stub is a recorder: it keeps both the system-prompt segments AND the
wired ``messages`` handed to ``arcrun.run_stream``, because that is where the
retrieved material actually reaches the model.

The chain under test:

    agent:moment (entity_seen / topic_shift / task_start ...)
        -> memory ``on_agent_moment`` subscriber
        -> ``brain.on_moment`` (deterministic detector gate + no-read-up recall + dedup)
        -> ``_State.proactive_buffer``
        -> ``inject_recall`` drains into ``sections["recall"]``
        -> the ``turn`` tier attached to the user message the model receives

Isolation matters here, and it is the whole reason tests 1-4 fire the moment and
then drain it under an EMPTY query. A live user turn always assembles with
``query=task``, and query-conditioned recall on that same text surfaces the same
card, so a card in a live turn's prompt is NOT attributable to the proactive
producer (verified: with ``proactive_enabled=False`` the card still lands via
query recall). Draining the buffer under an empty query removes that confound, so
recall text there can only have come through the proactive chain. Test 5 then
drives a genuine ``agent.run`` turn to prove the dispatcher actually emits the
moment into that chain (an audited proactive trigger a query recall can never set).

Five journeys, each its own boot so a failure names exactly one link:
(1) a gated, bounded card reaches the recall section; (2) an above-clearance card
does not; (3) a moment with no matching cue injects nothing; (4) a repeated signal
is deduped in-window; (5) a live turn emits the moment and audits its trigger kind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcbundle
import arcrun
import pytest
from arcrun import StreamEvent, TurnEndEvent
from arctrust import ValidatorsConfig, generate_keypair
from arctrust.paths import identity_dir, module_root, operator_dir

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.utils.moment import moment_cues

# The source tree the memory bundle is built FROM — resolved from the repo
# layout, never from ``arcagent.__file__`` (SPEC-066 strips ``modules/`` from
# the wheel). Mirrors ``test_live_modules_e2e``.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"

_ISSUER = "did:arc:test-operator"
#: One issuer keypair, minted once, so the agent config can pin it the way
#: ``arc module install`` pins a trusted issuer — a ``module:*`` root is VERIFIED,
#: so an unpinned issuer means the memory module materializes and registers nothing.
_ISSUER_KEYPAIR = generate_keypair()

#: A real Brain with the vector channel off: ``embed_backend = "none"`` keeps the
#: round trip real (BM25 + graph) while never fetching a sentence-transformers
#: model. ``proactive_enabled`` defaults True, so the ``agent:moment`` subscriber
#: is live without being switched on here.
_MEMORY_CONFIG: dict[str, Any] = {"brain": "arcmemory", "embed_backend": "none"}


# --------------------------------------------------------------------------
# Deployment construction — the real SPEC-066 install path (replicated from
# tests/integration/test_live_modules_e2e.py so this journey stands alone)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Deployment:
    """The on-disk shape a booted agent reads: arc home, agent dir, workspace."""

    arc_home: Path
    agent_dir: Path
    workspace: Path

    @property
    def modules_root(self) -> Path:
        return module_root(self.arc_home)

    @property
    def config_path(self) -> Path:
        return self.agent_dir / "arcagent.toml"


def _deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Deployment:
    """Lay out an empty deployment and point ``ARC_CONFIG_DIR`` at it."""
    arc_home = tmp_path / "arc"
    agent_dir = tmp_path / "agent"
    workspace = tmp_path / "ws"
    for path in (arc_home, agent_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    return Deployment(arc_home=arc_home, agent_dir=agent_dir, workspace=workspace)


def _install(deployment: Deployment, names: tuple[str, ...], tmp_path: Path) -> None:
    """Install ``names`` exactly as ``arc module install`` does: build → verify →
    materialize → copy capabilities. Any gate that would refuse an operator refuses here."""
    staging = tmp_path / "bundles"
    for name in names:
        bundle = arcbundle.build_bundle(
            _SOURCE_CATALOG / name,
            module=name,
            version="1.0.0",
            private_key=_ISSUER_KEYPAIR.private_key,
            issuer=_ISSUER,
            out=staging / name,
        )
        verified = arcbundle.verify_bundle(
            bundle,
            tier="personal",
            trusted_issuers={_ISSUER: _ISSUER_KEYPAIR.public_key},
        )
        installed = arcbundle.materialize(verified, deployment.modules_root)
        arcbundle.copy_capabilities(installed, deployment.agent_dir, module=name)


def _config(deployment: Deployment) -> ArcAgentConfig:
    """An agent config enabling exactly the memory module, all keys pinned in tmp."""
    return ArcAgentConfig(
        agent=AgentConfig(
            name="proactive-recall-agent",
            org="testorg",
            type="executor",
            workspace=str(deployment.workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(identity_dir(deployment.arc_home))),
        security=SecurityConfig(
            operator_key_dir=str(operator_dir(deployment.arc_home)),
            validators=ValidatorsConfig(trusted_keys=(_ISSUER_KEYPAIR.public_key.hex(),)),
        ),
        # Telemetry ON — the audit trail is one of the four pillars and journey (5)
        # reads it; a suite that switched it off could not tell "not emitted" from
        # "not recorded".
        telemetry=TelemetryConfig(enabled=True, export_traces=False),
        modules={"memory": ModuleEntry(enabled=True, config=dict(_MEMORY_CONFIG))},
    )


# --------------------------------------------------------------------------
# The only stub: a recording model wire, plus a spy on the memory audit emit
# --------------------------------------------------------------------------


@dataclass
class Recorders:
    """What the real run left behind: the model's input per turn and every audit event.

    ``inputs`` holds, per turn, the FULL text handed to ``arcrun.run_stream`` — the
    system-prompt cache segments AND the wired ``messages``. Both matter: retrieved
    recall is NOT a system-prompt segment. ``AssembledPrompt.segments`` is only
    ``[session, run]``; the per-turn ``turn`` tier that carries ``<recall>`` is
    re-attached to the user message by ``wire_messages`` and reaches the model in
    ``messages``. Capturing only ``system_prompt`` would miss the card entirely, so
    the recorder flattens both — the exact bytes the model would see, which is where
    a proactively-recalled card must appear for the producer to be alive. ``audit``
    holds every :class:`~arctrust.audit.AuditEvent` the brain emitted, so the
    ``memory.recall_attributed`` trigger can be read directly off the object.
    """

    inputs: list[str] = field(default_factory=list)
    audit: list[Any] = field(default_factory=list)

    def last_prompt_text(self) -> str:
        """The most recent turn's full model input (system prompt + messages)."""
        assert self.inputs, "no input was ever handed to the model"
        return self.inputs[-1]

    def proactive_attributions(self) -> list[Any]:
        """``memory.recall_attributed`` events that carry a proactive ``trigger``.

        The explicit query-recall path emits ``memory.recall_attributed`` with no
        ``trigger`` key; only the ``on_moment`` proactive path sets it. Filtering on
        it isolates proactive recall from ordinary query recall on the same turn.
        """
        return [
            e
            for e in self.audit
            if getattr(e, "action", "") == "memory.recall_attributed"
            and isinstance(getattr(e, "extra", None), dict)
            and e.extra.get("trigger")
        ]


@asynccontextmanager
async def _booted(deployment: Deployment, config: ArcAgentConfig) -> AsyncIterator[
    tuple[ArcAgent, Recorders]
]:
    """Start a real agent over ``deployment``; record the model wire + memory audit.

    The ``run_stream`` recorder replaces the LLM with a single-token turn AND keeps
    the ``system_prompt`` it was handed, because that prompt is the feature's output.
    ``arcmemory.brain.emit`` is spied (not replaced) so proactive attribution events
    are observable without changing what the brain does. Everything else is the real
    startup path.
    """
    recorders = Recorders()

    async def _recording_run_stream(*_args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        parts = list(kwargs.get("system_prompt") or [])
        for message in kwargs.get("messages") or []:
            content = getattr(message, "content", message)
            parts.append(arcrun.content_text(content))
        recorders.inputs.append("\n".join(parts))

        async def _events() -> AsyncIterator[StreamEvent]:
            yield TurnEndEvent(final_text="proactive-recall-ok", tool_calls_made=0)

        return _events()

    import arcmemory.brain as arcmemory_brain

    original_emit = arcmemory_brain.emit

    def _spy_emit(event: Any, sink: Any) -> Any:
        recorders.audit.append(event)
        return original_emit(event, sink)

    model = MagicMock()
    model.close = AsyncMock()
    stack: list[Any] = [
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
        patch(
            "arcagent.core.agent_dispatch.arcrun.run_stream",
            side_effect=_recording_run_stream,
        ),
        patch.object(arcmemory_brain, "emit", _spy_emit),
    ]
    for entered in stack:
        entered.__enter__()
    agent = ArcAgent(config=config, config_path=deployment.config_path)
    try:
        await agent.startup()
        yield agent, recorders
    finally:
        try:
            await agent.shutdown()
        finally:
            for entered in reversed(stack):
                entered.__exit__(None, None, None)


# --------------------------------------------------------------------------
# Reaching the real brain + driving a real turn
# --------------------------------------------------------------------------


def _memory_brain(agent: ArcAgent) -> Any:
    """The live ArcMemoryBrain the configured memory runtime selected.

    Read off the module runtime — not constructed here — so the seed below lands
    in the exact store the turn later recalls from. A ``NullBrain`` here would make
    every recall assertion a lie, so it is asserted active.
    """
    import sys

    runtime = sys.modules.get("arcagent.modules.memory._runtime")
    assert runtime is not None, "memory runtime never loaded — module did not install"
    state = runtime.state()
    assert state.active, "memory selected a NullBrain — recall below would prove nothing"
    return state.brain


def _seed_entity(agent: ArcAgent, brain: Any, name: str, *, classification: str = "unclassified") -> None:
    """Write an entity card so the ``entity_seen`` detector can fire on its name.

    ``entity_seen`` only fires when a turn cue overlaps a KNOWN entity term, and an
    entity card is created by consolidation, never by fast capture — so it must be
    seeded explicitly for the deterministic detector to have anything to match.
    """
    from arcmemory.index.graph import WeightedGraph
    from arcmemory.stores.semantic import SemanticStore

    assert agent._identity is not None
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=agent._identity.did)
    store.write_fact(
        name.lower(),
        "kind",
        "system under test",
        name=name,
        entity_type="system",
        classification=classification,
    )


async def _seed_fact(brain: Any, text: str, *, classification: str = "unclassified") -> None:
    """Capture a retrievable fact through the real zero-LLM fast path."""
    await brain.capture(text, kind="respond", classification=classification)


async def _proactive_recall_section(
    agent: ArcAgent,
    *,
    text: str,
    cues: list[str] | None = None,
    kind: str = "entity_seen",
    session_id: str | None = None,
) -> str:
    """Fire ONE real detected moment and drain it into a query-LESS assembly.

    This is the isolated proactive proof. Both steps ride the real module bus and
    the real registered subscribers — nothing is stubbed, faked, or bypassed:

    * ``agent:moment`` reaches the real ``on_agent_moment`` -> ``brain.on_moment``
      (real deterministic detector, real no-read-up recall, real dedup) -> the real
      ``_State.proactive_buffer``;
    * ``agent:assemble_prompt`` with an EMPTY query reaches the real ``inject_recall``,
      which drains the buffer into ``sections["recall"]`` while the query path stays
      dormant.

    The empty query is the whole point. A live dispatch always assembles with
    ``query=task``, and query-conditioned recall on that same text surfaces the same
    card, so a card in a live turn's prompt is NOT attributable to the proactive
    producer (verified: with ``proactive_enabled=False`` the card still lands via
    query recall). Draining the buffer under an empty query removes that confound, so
    text in ``sections["recall"]`` here can ONLY have come through the proactive chain.
    """
    assert agent._bus is not None
    payload = {
        "kind": kind,
        "cues": cues if cues is not None else moment_cues(text),
        "text": text,
        "session_id": session_id,
    }
    await agent._bus.emit("agent:moment", payload)
    sections: dict[str, str] = {}
    await agent._bus.emit("agent:assemble_prompt", {"sections": sections, "query": ""})
    return sections.get("recall", "")


async def _drive_turn(agent: ArcAgent, recorders: Recorders, text: str, *, key: str) -> str:
    """Run one real end-to-end turn; return the full model input, flattened.

    The whole dispatch runs unstubbed: the pre-assembly ``agent:moment`` emits, the
    memory subscriber, ``on_moment``, the buffer, ``inject_recall``, and the wiring of
    the ``turn`` tier onto the user message. Only the model at the end is the recorder.
    """
    session = await agent.session(key)
    events = [event async for event in agent.run(text, session=session)]
    assert isinstance(events[-1], TurnEndEvent), "the turn did not complete"
    return recorders.last_prompt_text()


# --------------------------------------------------------------------------
# 1. Moment -> gated, bounded card in the recall section (THE anti-dead-producer proof)
# --------------------------------------------------------------------------


async def test_a_detected_entity_injects_its_bounded_card_into_the_recall_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named known entity puts its card into ``sections["recall"]`` — proactively.

    Seed an UNCLASSIFIED entity ``Kestrel`` (so ``entity_seen`` can fire) and FIVE
    distinctive Kestrel facts. Fire the real moment, then drain it under an empty
    query. The card reaches the recall section (proving emit -> on_moment -> buffer ->
    inject_recall is fully wired, with NO query-recall confound), and the result is
    bounded to ``proactive_max_cards`` even though five facts matched — the gate and
    the bound both hold on the proactive path.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, _recorders):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Kestrel")
        markers = ["AAA111", "BBB222", "CCC333", "DDD444", "EEE555"]
        for i, marker in enumerate(markers):
            await _seed_fact(brain, f"Kestrel deployment note {i}: the marker is {marker}.")

        recall = await _proactive_recall_section(
            agent, text="give me the kestrel deployment notes"
        )
        bound = brain._cfg.proactive_max_cards

    assert any(marker in recall for marker in markers), (
        "no seeded Kestrel card reached the recall section — a producer in "
        "emit -> on_moment -> buffer -> inject_recall is dead"
    )
    blocks = recall.count("<memory-result")
    assert 0 < blocks <= bound, (
        f"proactive recall was not bounded to proactive_max_cards={bound}: "
        f"{blocks} cards rendered"
    )


# --------------------------------------------------------------------------
# 2. Above-clearance card excluded — the no-read-up gate holds on this path
# --------------------------------------------------------------------------


async def test_a_secret_card_is_excluded_from_the_proactive_recall_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proactive path never surfaces a card above the caller's clearance.

    Seed a known entity ``Falcon``, an UNCLASSIFIED fact (GREENLIGHT_UNCLASS) and a
    SECRET fact (REDTOKEN_SECRET), both matching the cue. Fire the moment at the
    unclassified clearance the subscriber uses and drain it. The unclassified token is
    recalled — proving recall actually ran and matched — while the SECRET token is
    dropped by ``gate_no_read_up``, so the exclusion is the gate, not an empty result.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, _recorders):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Falcon")
        await _seed_fact(brain, "The Falcon mission status is GREENLIGHT_UNCLASS today.")
        await _seed_fact(
            brain,
            "The Falcon mission launch code is REDTOKEN_SECRET.",
            classification="SECRET",
        )

        recall = await _proactive_recall_section(
            agent, text="what is the falcon mission status"
        )

    assert "GREENLIGHT_UNCLASS" in recall, (
        "proactive recall did not run at all — the exclusion below would be vacuous"
    )
    assert "REDTOKEN_SECRET" not in recall, (
        "a SECRET card reached an unclassified proactive recall — no-read-up failed "
        "on the proactive path"
    )


# --------------------------------------------------------------------------
# 3. A no-cue turn injects nothing proactive
# --------------------------------------------------------------------------


async def test_a_moment_with_no_matching_cue_injects_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A moment whose cues match no known entity yields no proactive recall.

    A known entity is seeded but never named. ``entity_seen`` cannot fire (no cue
    overlaps it), so nothing is buffered, the recall section is empty, and no proactive
    attribution is emitted. Both the empty section AND the absent attribution are
    asserted, so this cannot pass on a coincidentally-empty query path.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, recorders):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Kestrel")
        await _seed_fact(
            brain, "The Kestrel deployment root for Arc modules is XYZZY42QUUX."
        )

        recall = await _proactive_recall_section(agent, text="please compute two plus two")

    assert recall == "", f"a no-cue moment still injected a recall block: {recall!r}"
    assert not recorders.proactive_attributions(), (
        "a moment with no matching cue fired a proactive recall anyway: "
        f"{[e.extra for e in recorders.proactive_attributions()]}"
    )


# --------------------------------------------------------------------------
# 4. A repeated signal is deduped in-window
# --------------------------------------------------------------------------


async def test_the_same_entity_twice_in_a_session_injects_its_card_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming the same entity twice in one session injects its card once.

    Two moments in the same session both fire ``entity_seen``, but the in-window
    ``WindowDedup`` suppresses the card the second time: the first drain carries it,
    the second drains empty, and exactly one proactive attribution is recorded across
    the two. Asserted on the drained sections AND the attribution count, so the dedup
    is proven at both the injection point and the audit.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, recorders):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Kestrel")
        await _seed_fact(
            brain, "The Kestrel deployment root for Arc modules is XYZZY42QUUX."
        )

        # Both moments share one session key (the default the live user-turn emit
        # uses), so ``WindowDedup`` sees them as consecutive turns of one session —
        # and the seed lives in that same scope, so the recall can actually hit it.
        first = await _proactive_recall_section(
            agent, text="tell me about the kestrel deployment root"
        )
        second = await _proactive_recall_section(
            agent, text="the kestrel deployment root again please"
        )

    assert "XYZZY42QUUX" in first, "the first mention did not inject the card"
    assert "XYZZY42QUUX" not in second, (
        "the same card was re-injected on the second mention in one session — "
        "in-window dedup did not hold"
    )
    attributions = recorders.proactive_attributions()
    assert len(attributions) == 1, (
        "the same card was proactively attributed more than once across two moments "
        f"in one session: {[e.extra for e in attributions]}"
    )


# --------------------------------------------------------------------------
# 5. The live dispatch emits the moment AND the audit records its trigger
# --------------------------------------------------------------------------


async def test_a_live_turn_emits_the_moment_and_audits_its_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real turn naming an entity fires the proactive chain and audits its trigger.

    Tests 1-4 fire the moment themselves to isolate the chain; this one proves the
    producer is actually wired into live dispatch. Driving a genuine ``agent.run``
    turn that names Kestrel emits ``agent:moment`` from the dispatcher, so a
    ``memory.recall_attributed`` audit carrying ``extra["trigger"] == "entity_seen"``
    can only appear if the live emit reached ``on_moment`` — query recall shares the
    action but never sets a trigger. The turn's recall also reaches the model input,
    confirming the end-to-end read path (query + proactive both live) delivers.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    _install(deployment, ("memory",), tmp_path)

    async with _booted(deployment, _config(deployment)) as (agent, recorders):
        brain = _memory_brain(agent)
        _seed_entity(agent, brain, "Kestrel")
        await _seed_fact(
            brain, "The Kestrel deployment root for Arc modules is XYZZY42QUUX."
        )

        model_input = await _drive_turn(
            agent, recorders, "give me the kestrel deployment root please", key="j5"
        )

    assert "XYZZY42QUUX" in model_input, "the turn's recall never reached the model input"
    attributions = recorders.proactive_attributions()
    assert attributions, (
        "a live turn naming a known entity produced no proactive recall attribution — "
        "the dispatcher's agent:moment emit is not wired to on_moment"
    )
    triggers = {e.extra.get("trigger") for e in attributions}
    assert triggers == {"entity_seen"}, (
        f"proactive attribution carried the wrong trigger kind: {triggers}"
    )

