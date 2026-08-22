"""RED — ``ArcMemoryBrain.on_moment`` proactive detected-moment recall (SPEC-071).

Drives the REAL ``ArcMemoryBrain`` (no embedder -> BM25 + graph degrade). Nothing
here is faked except the audit sink (a recorder, per the shared brief's test
infrastructure pattern) — capture, entity writes, and the gated retrieval path are
all production code.

``on_moment`` does not exist yet (T-983). Every test below calls
``await brain.on_moment(...)`` directly (no ``pytest.raises`` wrapper) so the
suite fails with ``AttributeError: 'ArcMemoryBrain' object has no attribute
'on_moment'`` — the right RED reason: the feature is absent, not a broken
fixture. The seeding/setup lines that precede each call must themselves run
clean; that is verified in this file's own smoke test.
"""

from __future__ import annotations

from pathlib import Path

from arctrust.audit import AuditEvent

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore

_DID = "did:arc:moment-agent"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _seed_entities(brain: ArcMemoryBrain, *, count: int, token: str, classification: str) -> None:
    """Write ``count`` distinct entity cards whose names all carry ``token``."""
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=_DID)
    for i in range(count):
        store.write_fact(
            f"{token}-{i}",
            "kind",
            "proactive-candidate",
            name=f"{token.title()} {i}",
            entity_type="thing",
            classification=classification,
        )


async def test_on_moment_seeding_setup_runs_without_error(workspace: Path) -> None:
    """Smoke test: the seeding helpers used below never raise on their own.

    Proves any AttributeError raised by the tests in this file comes from the
    missing ``on_moment`` method and not from a broken fixture — capture() and
    SemanticStore.write_fact() both execute clean, unrelated to on_moment.
    """
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    await brain.capture(
        "the zephyr-token widget was mentioned in passing",
        kind="respond",
        classification="unclassified",
    )
    _seed_entities(brain, count=5, token="zephyr", classification="unclassified")

    # capture() and write_fact() both ran to completion (no exception): capture
    # audited its own event, and the 5 entity cards are readable back off disk.
    assert [e.action for e in sink.events] == ["memory.captured"]
    store = SemanticStore(brain._workspace, WeightedGraph(brain._db), scope=_DID)
    assert len(store.slugs()) == 5


async def test_on_moment_fires_bounded_to_proactive_max_cards(workspace: Path) -> None:
    """More matching UNCLASSIFIED memories than ``proactive_max_cards`` exist —
    the returned text must surface at most ``proactive_max_cards`` cards."""
    sink = RecordingSink()
    cfg = MemoryConfig()
    brain = ArcMemoryBrain(workspace, _DID, config=cfg, audit_sink=sink)

    # Seed strictly more matching UNCLASSIFIED memories than the bound allows.
    assert cfg.proactive_max_cards < 6
    _seed_entities(brain, count=6, token="nebula", classification="unclassified")

    text = await brain.on_moment(
        "entity_seen",
        cues=["nebula"],
        text="the nebula reading just came up again",
        clearance="unclassified",
    )

    assert text != ""
    assert text.count("<memory-result") <= cfg.proactive_max_cards


async def test_on_moment_gates_secret_card_from_unclassified_caller(workspace: Path) -> None:
    """A SECRET card the cues would otherwise match must never reach an
    UNCLASSIFIED caller (no-read-up, REQ-347)."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    _seed_entities(brain, count=1, token="vault-omega", classification="SECRET")

    text = await brain.on_moment(
        "entity_seen",
        cues=["vault-omega"],
        text="the vault-omega protocol was raised",
        clearance="unclassified",
    )

    assert "vault-omega" not in text.lower()
    assert "Vault-Omega" not in text
    dropped_or_absent = any(e.action == "recall.dropped" for e in sink.events) or text == ""
    assert dropped_or_absent


async def test_on_moment_emits_recall_attributed_with_trigger(workspace: Path) -> None:
    """A firing recall emits ``memory.recall_attributed`` with ``trigger`` set
    to the moment kind and a non-empty ``cards`` list (COMP-009)."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    _seed_entities(brain, count=1, token="griffin", classification="unclassified")

    await brain.on_moment(
        "entity_seen",
        cues=["griffin"],
        text="griffin came up again in the thread",
        clearance="unclassified",
    )

    attributed = [e for e in sink.events if e.action == "memory.recall_attributed"]
    assert attributed, "expected a memory.recall_attributed audit event"
    event = attributed[0]
    assert event.extra.get("trigger") == "entity_seen"
    assert event.extra.get("cards")


async def test_on_moment_no_cue_returns_empty_and_does_not_attribute(workspace: Path) -> None:
    """No cues and no text means nothing to match — REQ-343: no cue, no
    recall query. Returns empty and never emits attribution."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    text = await brain.on_moment("entity_seen", cues=[], text="", clearance="unclassified")

    assert text == ""
    assert not [e for e in sink.events if e.action == "memory.recall_attributed"]


async def test_on_moment_unknown_kind_returns_empty_and_does_not_attribute(
    workspace: Path,
) -> None:
    """An unrecognized moment kind never fires — fail-open, no recall."""
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    text = await brain.on_moment("banana", cues=["x"], text="y", clearance="unclassified")

    assert text == ""
    assert not [e for e in sink.events if e.action == "memory.recall_attributed"]
