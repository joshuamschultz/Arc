"""SPEC-083 T-1224 (COMP-029, REQ-512) — "Run now": a manual promotion sweep.

RED intent: ``ArcMemoryBrain.run_promotion`` does not exist yet, so every test
that calls it fails with ``AttributeError`` — the feature is absent.

Operator intent (README decision 14): run promotion ONCE NOW over an agent's
EXISTING memory (backfill), then the nightly runs send only new or changed items.

Contract assumed (names from the task brief; SDD COMP-029 names only the method):

- ``ArcMemoryBrain.run_promotion(*, max_items: int | None = None)
  -> PromotionSweepResult`` runs the same ``PromotionSweep`` the nightly hygiene
  runs, outside the nightly hook.
- It shares ONE per-agent lock with the nightly hook: a manual run that overlaps
  a nightly sweep waits for it (it does not fail and does not run in parallel),
  so the classifier is never asked about the same item twice. Two agents never
  wait on each other.
- ``max_items`` caps THIS run only (1..5000, else ``ValueError``; a non-int is
  refused too). It is never persisted: the next run uses ``max_items_per_sweep``.
- Federal -> ``tier_forbidden``; promotion disabled or not configured ->
  ``disabled``; both with zero classifier calls.
- A manual run does not write, advance or consult the nightly hygiene stamp, so
  that night's hygiene still runs.

Real objects: the brain, its card stores on a tmp workspace, the signed ledger,
the real ``PromotionSweep`` and the nightly ``consolidate()`` path. Faked only at
the boundary: the third-party classifier, the shared-store publisher, the
distiller (an LLM) and the audit sink.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcprompt import StockPromptSource
from arctrust.audit import AuditEvent
from arctrust.signer import InProcessSigner

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.consolidate import _HYGIENE_LAST_NAME
from arcmemory.distill import (
    DaySummaryDraft,
    EventExtraction,
    FactExtraction,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.promotion.classifier import (
    ClassifierInput,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.question import load_promotion_question
from arcmemory.promotion.sweep import PromotionSweepResult
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Event, Insight, Procedure

_QV = question_version(load_promotion_question(StockPromptSource()))
_DID = "did:arc:test-agent"
_OTHER_DID = "did:arc:other-agent"
#: Bound on any await that could deadlock (e.g. a non-reentrant lock taken twice).
_DEADLOCK_GUARD_S = 10.0
_MAX_ITEMS_CEILING = 5000


# -- boundary fakes -----------------------------------------------------------


class _NullDistiller:
    """The consolidation LLM, faked: distills nothing so hygiene acts on disk only."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction()

    async def mint_insights(self, events: list[Event], facts: list[Any]) -> InsightMint:
        return InsightMint()

    async def extract_procedures(
        self, events: list[Event], existing: list[Procedure]
    ) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def extract_events(self, episodes: list[Event]) -> EventExtraction:
        return EventExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft()

    async def disambiguate_entity(
        self, name: str, entity_type: str, candidates: list[str]
    ) -> str | None:
        return None


class _Classifier:
    """The third-party classifier: records inputs and how many calls overlap.

    With ``gate`` set, EVERY classify call parks on it until the test releases
    it, and ``entered`` fires on the first call — so a test can hold one sweep
    mid-egress and observe what a second sweep does meanwhile.
    """

    classifier_id = "jev"
    question_version = _QV
    host: str | None = "api.typesafe.test"

    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.inputs: list[ClassifierInput] = []
        self.gate = gate
        self.entered = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.inputs.append(item)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.entered.set()
            if self.gate is not None:
                await self.gate.wait()
            return ClassifierVerdict(
                label="company",
                confidence=0.97,
                probabilities={
                    "company": 0.97,
                    "personal": 0.01,
                    "agent_only": 0.01,
                    "unclear": 0.01,
                },
                personal_probability=0.02,
                classifier_id="jev",
                classifier_version="jev-1.13.0",
                request_id=None,
                input_tokens=None,
            )
        finally:
            self.in_flight -= 1

    @property
    def sent_ids(self) -> list[str]:
        return [item.item_id for item in self.inputs]


class _Publisher:
    def __init__(self) -> None:
        self.references: list[str] = []

    async def publish(
        self, reference: str, *, content_sha256: str, confidence: float, classifier_version: str
    ) -> str:
        self.references.append(reference)
        return f"shared:{reference}"

    async def demotions(self) -> dict[str, object]:
        return {}


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]


# -- helpers ------------------------------------------------------------------


def _brain(
    workspace: Path,
    classifier: _Classifier,
    publisher: _Publisher | None = None,
    *,
    tier: str = "personal",
    did: str = _DID,
    cfg: PromotionConfig | None = None,
    configured: bool = True,
    sink: _Sink | None = None,
) -> ArcMemoryBrain:
    return ArcMemoryBrain(
        workspace,
        did,
        config=MemoryConfig.for_tier(tier),  # type: ignore[arg-type]  # reason: literal tier
        distiller=_NullDistiller(),
        audit_sink=sink if sink is not None else _Sink(),
        promotion_config=(
            (cfg if cfg is not None else PromotionConfig(enabled=True)) if configured else None
        ),
        promotion_classifier=classifier,
        promotion_publisher=publisher if publisher is not None else _Publisher(),
        promotion_signer=InProcessSigner(b"\x01" * 32) if configured else None,
    )


def _existing_memory(workspace: Path, count: int, *, prefix: str = "deal") -> list[str]:
    """Cards already on disk before promotion was ever run (the backfill set)."""
    store = InsightStore(workspace)
    ids = [f"{prefix}-{index}" for index in range(count)]
    for index, item_id in enumerate(ids):
        store.write(
            Insight(
                id=item_id,
                statement=f"Acme renewal #{index} closes at ${40 + index}k per year.",
                trigger="a renewal",
            )
        )
    return ids


def _hygiene_stamp(workspace: Path) -> Path:
    return workspace / "memory" / _HYGIENE_LAST_NAME


def _next_night(workspace: Path) -> None:
    """Make the nightly hygiene due again (as if the local date rolled over)."""
    _hygiene_stamp(workspace).unlink(missing_ok=True)


async def _bounded(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, timeout=_DEADLOCK_GUARD_S)


async def _spin(turns: int = 100) -> None:
    """Give every runnable task many chances to advance (no wall-clock sleeps)."""
    for _ in range(turns):
        await asyncio.sleep(0)


# ============================================================================
# Backfill, then new-only
# ============================================================================


async def test_run_promotion_backfills_existing_memory_immediately(workspace: Path) -> None:
    ids = _existing_memory(workspace, 3)
    classifier, publisher = _Classifier(), _Publisher()

    result = await _brain(workspace, classifier, publisher).run_promotion()

    assert isinstance(result, PromotionSweepResult)
    assert result.status == "completed"
    assert result.evaluated == 3
    assert result.promoted == 3
    assert sorted(classifier.sent_ids) == sorted(ids)
    assert sorted(publisher.references) == sorted(f"insight:{i}" for i in ids)


async def test_second_manual_run_right_after_sends_zero_requests(workspace: Path) -> None:
    _existing_memory(workspace, 3)
    classifier = _Classifier()
    brain = _brain(workspace, classifier)
    await brain.run_promotion()

    again = await brain.run_promotion()

    assert again.status == "completed"
    assert again.evaluated == 0
    assert len(classifier.inputs) == 3


async def test_nightly_after_backfill_sends_only_new_and_changed_items(workspace: Path) -> None:
    ids = _existing_memory(workspace, 3)
    classifier = _Classifier()
    brain = _brain(workspace, classifier)
    await brain.run_promotion()

    quiet = await brain.consolidate()
    assert quiet["promotion_status"] == "completed"
    assert quiet["promotion_evaluated"] == 0

    store = InsightStore(workspace)
    store.write(Insight(id="brand-new", statement="Globex signed a pilot.", trigger="a pilot"))
    store.write(Insight(id=ids[0], statement="Acme renewal moved to $55k.", trigger="a renewal"))
    classifier.inputs.clear()
    _next_night(workspace)

    night = await brain.consolidate()

    assert night["promotion_evaluated"] == 2
    assert sorted(classifier.sent_ids) == sorted(["brand-new", ids[0]])


# ============================================================================
# max_items: one run only, bounded, never persisted
# ============================================================================


async def test_max_items_caps_this_run_and_defers_the_rest(workspace: Path) -> None:
    _existing_memory(workspace, 5)
    classifier = _Classifier()

    result = await _brain(workspace, classifier).run_promotion(max_items=2)

    assert result.evaluated == 2
    assert result.deferred == 3
    assert len(classifier.inputs) == 2


async def test_max_items_can_exceed_the_nightly_cap_for_a_backfill(workspace: Path) -> None:
    _existing_memory(workspace, 6)
    classifier = _Classifier()
    brain = _brain(workspace, classifier, cfg=PromotionConfig(enabled=True, max_items_per_sweep=1))

    result = await brain.run_promotion(max_items=4)

    assert result.evaluated == 4
    assert result.deferred == 2


async def test_max_items_is_never_persisted(workspace: Path) -> None:
    """The next run — manual without a cap, and nightly — uses the configured cap."""
    _existing_memory(workspace, 6)
    classifier = _Classifier()
    cfg = PromotionConfig(enabled=True, max_items_per_sweep=1)
    brain = _brain(workspace, classifier, cfg=cfg)
    await brain.run_promotion(max_items=3)

    uncapped_manual = await brain.run_promotion()
    night = await brain.consolidate()

    assert uncapped_manual.evaluated == 1
    assert night["promotion_evaluated"] == 1
    assert night["promotion_deferred"] == 1
    assert cfg.max_items_per_sweep == 1


async def test_max_items_at_the_ceiling_is_accepted(workspace: Path) -> None:
    _existing_memory(workspace, 2)
    classifier = _Classifier()

    result = await _brain(workspace, classifier).run_promotion(max_items=_MAX_ITEMS_CEILING)

    assert result.status == "completed"
    assert result.evaluated == 2


@pytest.mark.parametrize("cap", [0, -1, _MAX_ITEMS_CEILING + 1, 10**9])
async def test_max_items_out_of_bounds_is_refused_before_any_egress(
    workspace: Path, cap: int
) -> None:
    _existing_memory(workspace, 2)
    classifier = _Classifier()
    sink = _Sink()
    brain = _brain(workspace, classifier, sink=sink)

    with pytest.raises(ValueError):
        await brain.run_promotion(max_items=cap)

    assert classifier.inputs == []
    assert sink.actions("memory.promotion.egress") == []


@pytest.mark.parametrize("cap", [True, 2.5, "10"], ids=["bool", "float", "str"])
async def test_max_items_of_the_wrong_type_is_refused(workspace: Path, cap: Any) -> None:
    _existing_memory(workspace, 2)
    classifier = _Classifier()

    with pytest.raises((TypeError, ValueError)):
        await _brain(workspace, classifier).run_promotion(max_items=cap)

    assert classifier.inputs == []


# ============================================================================
# Gates: federal, disabled, not configured
# ============================================================================


async def test_federal_manual_run_is_tier_forbidden_with_zero_classifier_calls(
    workspace: Path,
) -> None:
    _existing_memory(workspace, 3)
    classifier, publisher = _Classifier(), _Publisher()

    result = await _brain(workspace, classifier, publisher, tier="federal").run_promotion(
        max_items=10
    )

    assert result.status == "tier_forbidden"
    assert result.evaluated == 0
    assert classifier.inputs == []
    assert publisher.references == []


async def test_disabled_manual_run_reports_disabled_with_zero_calls(workspace: Path) -> None:
    _existing_memory(workspace, 3)
    classifier = _Classifier()

    result = await _brain(
        workspace, classifier, cfg=PromotionConfig(enabled=False)
    ).run_promotion()

    assert result.status == "disabled"
    assert classifier.inputs == []


async def test_manual_run_on_a_brain_without_promotion_reports_disabled(
    workspace: Path,
) -> None:
    _existing_memory(workspace, 3)
    classifier = _Classifier()

    result = await _brain(workspace, classifier, configured=False).run_promotion()

    assert result.status == "disabled"
    assert classifier.inputs == []


# ============================================================================
# The nightly hygiene stamp is not touched
# ============================================================================


async def test_manual_run_does_not_create_the_hygiene_stamp(workspace: Path) -> None:
    _existing_memory(workspace, 2)
    brain = _brain(workspace, _Classifier())

    await brain.run_promotion()

    assert not _hygiene_stamp(workspace).exists()


async def test_manual_run_leaves_an_existing_hygiene_stamp_byte_identical(
    workspace: Path,
) -> None:
    _existing_memory(workspace, 2)
    stamp = _hygiene_stamp(workspace)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("2000-01-01", encoding="utf-8")
    before = stamp.read_bytes()

    await _brain(workspace, _Classifier()).run_promotion()

    assert stamp.read_bytes() == before


async def test_nightly_hygiene_still_runs_the_same_day_after_a_manual_run(
    workspace: Path,
) -> None:
    """A manual run must not stamp or skip that night's hygiene (and its sweep)."""
    _existing_memory(workspace, 2)
    brain = _brain(workspace, _Classifier())
    await brain.run_promotion()

    night = await brain.consolidate()

    assert night["promotion_status"] == "completed"
    assert _hygiene_stamp(workspace).exists()


# ============================================================================
# Serialization with the nightly sweep (one sweep at a time per agent)
# ============================================================================


async def test_manual_run_during_nightly_sweep_waits_and_never_double_sends(
    workspace: Path,
) -> None:
    ids = _existing_memory(workspace, 4)
    gate = asyncio.Event()
    classifier, publisher = _Classifier(gate=gate), _Publisher()
    brain = _brain(workspace, classifier, publisher)

    nightly = asyncio.create_task(brain.consolidate())
    await _bounded(classifier.entered.wait())
    manual = asyncio.create_task(brain.run_promotion())
    await _spin()

    # The nightly sweep is parked mid-egress on item 1; the manual run must be
    # waiting on the per-agent lock, not planning or classifying in parallel.
    assert len(classifier.inputs) == 1
    assert not manual.done()

    gate.set()
    night, manual_result = await _bounded(asyncio.gather(nightly, manual))

    assert len(classifier.inputs) == len(ids)
    assert sorted(classifier.sent_ids) == sorted(ids)
    assert classifier.max_in_flight == 1
    assert night["promotion_evaluated"] == len(ids)
    assert manual_result.status == "completed"
    assert manual_result.evaluated == 0
    assert sorted(publisher.references) == sorted(f"insight:{i}" for i in ids)


async def test_nightly_sweep_during_manual_run_waits_and_never_double_sends(
    workspace: Path,
) -> None:
    ids = _existing_memory(workspace, 4)
    gate = asyncio.Event()
    classifier, publisher = _Classifier(gate=gate), _Publisher()
    brain = _brain(workspace, classifier, publisher)

    manual = asyncio.create_task(brain.run_promotion())
    await _bounded(classifier.entered.wait())
    nightly = asyncio.create_task(brain.consolidate())
    await _spin()

    assert len(classifier.inputs) == 1
    assert not nightly.done()

    gate.set()
    manual_result, night = await _bounded(asyncio.gather(manual, nightly))

    assert len(classifier.inputs) == len(ids)
    assert classifier.max_in_flight == 1
    assert manual_result.evaluated == len(ids)
    assert night["promotion_evaluated"] == 0
    assert sorted(publisher.references) == sorted(f"insight:{i}" for i in ids)


async def test_two_overlapping_manual_runs_send_each_item_once(workspace: Path) -> None:
    """A double-clicked "Run now" is serialized too."""
    ids = _existing_memory(workspace, 3)
    gate = asyncio.Event()
    classifier = _Classifier(gate=gate)
    brain = _brain(workspace, classifier)

    first = asyncio.create_task(brain.run_promotion())
    await _bounded(classifier.entered.wait())
    second = asyncio.create_task(brain.run_promotion())
    await _spin()
    assert len(classifier.inputs) == 1

    gate.set()
    results = await _bounded(asyncio.gather(first, second))

    assert len(classifier.inputs) == len(ids)
    assert classifier.max_in_flight == 1
    assert sorted(r.evaluated for r in results) == [0, len(ids)]


async def test_the_lock_is_per_agent_so_another_agent_is_never_blocked(tmp_path: Path) -> None:
    """Narrowness: a global lock would stall every agent behind one slow sweep."""
    blocked_ws, free_ws = tmp_path / "blocked-agent", tmp_path / "free-agent"
    _existing_memory(blocked_ws, 2)
    _existing_memory(free_ws, 2)
    blocked_classifier = _Classifier(gate=asyncio.Event())
    free_classifier = _Classifier()
    blocked = _brain(blocked_ws, blocked_classifier)
    free = _brain(free_ws, free_classifier, did=_OTHER_DID)

    parked = asyncio.create_task(blocked.run_promotion())
    await _bounded(blocked_classifier.entered.wait())

    result = await _bounded(free.run_promotion())

    assert result.evaluated == 2
    assert not parked.done()
    assert blocked_classifier.gate is not None
    blocked_classifier.gate.set()
    await _bounded(parked)
