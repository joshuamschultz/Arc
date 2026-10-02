"""T-1197 (SPEC-083 COMP-018 / COMP-020 / COMP-022) — the nightly promotion sweep.

RED intent: ``arcmemory.promotion.sweep`` and ``arcmemory.promotion.publisher`` do
not exist yet, so collection fails with ``ModuleNotFoundError`` — the feature is
absent.

Real objects: the insight / procedure / semantic stores on a tmp workspace, the
signed ``PromotionLedger`` (real Ed25519 ``InProcessSigner``),
``render_candidate``, ``decide`` and the secret
gate. Faked at the boundary only: the third-party classifier, the shared-store
publisher and the audit sink (a durable-capable recorder with the arctrust
``AuditSink`` + ``DurableAuditSink`` shape, plus a failing-durable variant).

Contract assumed (SDD COMP-018/020/022):

- ``PromotionSweep(cfg=, tier=, stores=, ledger=, classifier=, publisher=,
  audit_sink=, clearance=)`` — keyword construction. ``stores`` is the
  ``ConsolidatedStores`` shape the exporter already takes (``.insights``,
  ``.procedures``, ``.entities``), enumerated via ``InsightStore.all_ids()``,
  ``ProceduralStore.slugs()`` and ``SemanticStore.slugs()``.
- ``await sweep.run(now) -> PromotionSweepResult`` with ``status, evaluated,
  promoted, kept_private, blocked_secret, too_large, deferred``.
- ``PromotionPublisher.publish(reference, *, content_sha256, confidence,
  classifier_version) -> str``; ``reference`` is ``"<kind>:<id>"`` (the exporter's
  reference form). ``PublisherUnavailableError`` = pre-write refusal (nothing was
  written); ``PublishOutcomeUnknownError`` = uncertain effect (never retried).
- Publish state is tracked by appending signed ledger rows; ``ledger.latest()``
  is the source of truth for ``publish_state`` / ``shared_ref``.
- A sweep built with no publisher reports ``publisher_unavailable`` and sends
  nothing (no point disclosing bytes that cannot be published — LLM02).
- ``evaluated`` counts items actually sent to the classifier this sweep.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcprompt import StockPromptSource
from arctrust.audit import AuditEvent
from arctrust.signer import InProcessSigner

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.promotion.classifier import (
    ClassifierCallError,
    ClassifierInput,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.config import PromotionConfig
from arcmemory.promotion.ledger import LedgerRow, PromotionLedger
from arcmemory.promotion.publisher import (
    Demotion,
    PublisherUnavailableError,
    PublishOutcomeUnknownError,
)
from arcmemory.promotion.question import load_promotion_question
from arcmemory.promotion.render import content_digest, render_candidate
from arcmemory.promotion.sweep import PromotionSweep
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Insight, Procedure, Scope, Step

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_DID = "did:arc:test-agent"
_NIGHT_1 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
_NIGHT_2 = _NIGHT_1 + timedelta(days=1)
_NIGHT_3 = _NIGHT_2 + timedelta(days=1)
_MODEL = "jev-1.13.0"
_QV = question_version(PROMOTION_QUESTION)

Timeline = list[tuple[str, str]]


# -- boundary fakes -----------------------------------------------------------


def _verdict(label: str) -> ClassifierVerdict:
    others = [lbl for lbl in ("company", "personal", "agent_only", "unclear") if lbl != label]
    probabilities = {label: 0.97, **{other: 0.01 for other in others}}
    return ClassifierVerdict(
        label=label,  # type: ignore[arg-type]  # reason: test builds labels from str
        confidence=0.97,
        probabilities=probabilities,  # type: ignore[arg-type]  # reason: same
        personal_probability=0.95 if label == "personal" else 0.02,
        classifier_id="jev",
        classifier_version=_MODEL,
        request_id="req-1",
        input_tokens=42,
    )


class FakeClassifier:
    """The third-party transport stand-in: records every input it is handed."""

    classifier_id = "jev"
    question_version = _QV
    host: str | None = "api.typesafe.test"

    def __init__(
        self,
        timeline: Timeline,
        *,
        labels: dict[str, str] | None = None,
        fail_on_call: int | None = None,
    ) -> None:
        self._timeline = timeline
        self._labels = labels or {}
        self._fail_on_call = fail_on_call
        self.inputs: list[Any] = []

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.inputs.append(item)
        self._timeline.append(("classify", item.item_id))
        if self._fail_on_call is not None and len(self.inputs) == self._fail_on_call:
            raise ClassifierCallError("529 overloaded")
        return _verdict(self._labels.get(item.item_id, "company"))

    @property
    def sent_ids(self) -> list[str]:
        return [item.item_id for item in self.inputs]


@dataclass
class PublishCall:
    reference: str
    content_sha256: str
    confidence: float
    classifier_version: str


class FakePublisher:
    """The shared-store stand-in. ``mode``: ok | refuse | unknown."""

    def __init__(
        self,
        timeline: Timeline,
        *,
        mode: str = "ok",
        before: Callable[[str], None] | None = None,
        demoted: dict[str, Demotion] | None = None,
    ) -> None:
        self._timeline = timeline
        self._mode = mode
        self._before = before
        self.calls: list[PublishCall] = []
        self.operator_calls: list[tuple[str, str, str]] = []
        #: The shared side's verified demotions; ``None`` -> unreadable.
        self.demoted: dict[str, Demotion] | None = demoted if demoted is not None else {}

    async def publish(
        self, reference: str, *, content_sha256: str, confidence: float, classifier_version: str
    ) -> str:
        self.calls.append(PublishCall(reference, content_sha256, confidence, classifier_version))
        return self._outcome(reference)

    async def publish_by_operator(
        self, reference: str, *, content_sha256: str, decided_by: str
    ) -> str:
        self.operator_calls.append((reference, content_sha256, decided_by))
        return self._outcome(reference)

    async def demotions(self) -> dict[str, Demotion]:
        if self.demoted is None:
            raise PublisherUnavailableError("shared store unreadable")
        return self.demoted

    def _outcome(self, reference: str) -> str:
        self._timeline.append(("publish", reference))
        if self._before is not None:
            self._before(reference)
        if self._mode == "refuse":
            raise PublisherUnavailableError("source digest does not match")
        if self._mode == "unknown":
            raise PublishOutcomeUnknownError("connection dropped after send")
        return f"shared:{reference}"

    @property
    def references(self) -> list[str]:
        return [call.reference for call in self.calls]


class RecordingSink:
    """``AuditSink`` + ``DurableAuditSink`` recorder sharing the fakes' timeline."""

    def __init__(self, timeline: Timeline, *, fail_durable: bool = False) -> None:
        self._timeline = timeline
        self._fail_durable = fail_durable
        self.events: list[AuditEvent] = []
        self.durable: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)
        self._timeline.append(("audit", event.action))

    def write_durable(self, event: AuditEvent) -> None:
        if self._fail_durable:
            raise OSError("audit volume full")
        self.events.append(event)
        self.durable.append(event)
        self._timeline.append(("durable", event.action))

    def actions(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]


class WriteOnlySink:
    """A sink that cannot write durably (e.g. ``NullSink``-shaped)."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class CountingProxy:
    """Delegates to a real store, counting every method the sweep touches."""

    def __init__(self, target: object, counter: list[str]) -> None:
        self._target = target
        self._counter = counter

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def _counted(*args: Any, **kwargs: Any) -> Any:
            self._counter.append(name)
            return attr(*args, **kwargs)

        return _counted


# -- environment --------------------------------------------------------------


@dataclass
class Env:
    workspace: Path
    insights: InsightStore
    procedures: ProceduralStore
    entities: SemanticStore
    ledger: PromotionLedger
    stores: Any
    store_reads: list[str]
    timeline: Timeline = field(default_factory=list)

    def sweep(
        self,
        *,
        cfg: PromotionConfig | None = None,
        tier: str = "personal",
        classifier: Any = "default",
        publisher: Any = "default",
        sink: Any = None,
    ) -> PromotionSweep:
        return PromotionSweep(
            cfg=cfg if cfg is not None else PromotionConfig(enabled=True),
            tier=tier,
            stores=self.stores,
            ledger=self.ledger,
            classifier=FakeClassifier(self.timeline) if classifier == "default" else classifier,
            publisher=FakePublisher(self.timeline) if publisher == "default" else publisher,
            audit_sink=sink if sink is not None else RecordingSink(self.timeline),
            clearance="unclassified",
        )

    def ledger_text(self) -> str:
        path = self.workspace / "memory" / "promotion" / "ledger.jsonl"
        return path.read_text(encoding="utf-8") if path.is_file() else ""


@pytest.fixture
def env(workspace: Path, db: MemoryDB, scope: Scope) -> Env:
    insights = InsightStore(workspace)
    procedures = ProceduralStore(workspace)
    entities = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    reads: list[str] = []
    stores = SimpleNamespace(
        insights=CountingProxy(insights, reads),
        procedures=CountingProxy(procedures, reads),
        entities=CountingProxy(entities, reads),
    )
    return Env(
        workspace=workspace,
        insights=insights,
        procedures=procedures,
        entities=entities,
        ledger=PromotionLedger(workspace, InProcessSigner(b"\x01" * 32)),
        stores=stores,
        store_reads=reads,
    )


def _add_insight(env: Env, insight_id: str, statement: str) -> str:
    env.insights.write(Insight(id=insight_id, statement=statement, trigger="a renewal"))
    return insight_id


def _add_procedure(env: Env, slug: str, marker: str) -> str:
    env.procedures.write(
        Procedure(
            slug=slug,
            title="Close the monthly books",
            when_to_use=f"At month end {marker}.",
            steps=[Step(text="Reconcile the bank feeds"), Step(text="Post accruals")],
        )
    )
    return slug


def _add_entity(env: Env, slug: str, marker: str) -> str:
    env.entities.write_fact(slug, "renewal", f"net-60 {marker}", name="Acme Corp")
    return slug


def _content_of(env: Env, kind: str, item_id: str) -> str:
    readers = {"insight": env.insights, "procedure": env.procedures, "entity": env.entities}
    item = readers[kind].read(item_id)
    assert item is not None
    return render_candidate(item).content


def _seed_five_insights(env: Env) -> list[str]:
    return [
        _add_insight(env, f"deal-{n}", f"Deal {n} closes at ${n}0k/yr ZEBRA-QUOKKA-{n}.")
        for n in range(1, 6)
    ]


# -- gates: disabled / federal / no classifier / no publisher ------------------


async def test_sweep_disabled_returns_disabled_and_reads_nothing(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)
    sink = RecordingSink(env.timeline)

    result = await env.sweep(
        cfg=PromotionConfig(), classifier=classifier, publisher=publisher, sink=sink
    ).run(_NIGHT_1)

    assert result.status == "disabled"
    assert env.store_reads == []
    assert classifier.inputs == []
    assert publisher.calls == []
    assert sink.actions("memory.promotion.egress") == []
    assert env.ledger_text() == ""


@pytest.mark.parametrize("tier", ["federal", " FEDERAL "])
async def test_sweep_federal_tier_forbidden_even_with_classifier_composed(
    env: Env, tier: str
) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)
    sink = RecordingSink(env.timeline)

    # Hand-built enabled config: bypasses PromotionConfig.for_tier on purpose.
    result = await env.sweep(
        cfg=PromotionConfig(enabled=True),
        tier=tier,
        classifier=classifier,
        publisher=publisher,
        sink=sink,
    ).run(_NIGHT_1)

    assert result.status == "tier_forbidden"
    assert classifier.inputs == []
    assert publisher.calls == []
    assert sink.actions("memory.promotion.egress") == []
    assert env.store_reads == []


async def test_sweep_without_classifier_is_unavailable_with_zero_egress(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    publisher = FakePublisher(env.timeline)
    sink = RecordingSink(env.timeline)

    result = await env.sweep(classifier=None, publisher=publisher, sink=sink).run(_NIGHT_1)

    assert result.status == "classifier_unavailable"
    assert sink.actions("memory.promotion.egress") == []
    assert publisher.calls == []
    assert env.ledger.latest("insight", "acme-renewal") is None


async def test_sweep_without_publisher_is_publisher_unavailable_and_sends_nothing(
    env: Env,
) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    sink = RecordingSink(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=None, sink=sink).run(_NIGHT_1)

    assert result.status == "publisher_unavailable"
    assert classifier.inputs == []
    assert sink.actions("memory.promotion.egress") == []


# -- happy path + the classifier seam ------------------------------------------


async def test_sweep_promotes_company_items_of_all_three_kinds(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr ZEBRA-QUOKKA-1.")
    _add_procedure(env, "close-monthly-books", "ZEBRA-QUOKKA-2")
    _add_entity(env, "acme-corp", "ZEBRA-QUOKKA-3")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_1)

    assert result.status == "completed"
    assert (result.evaluated, result.promoted, result.kept_private) == (3, 3, 0)
    assert (result.blocked_secret, result.too_large, result.deferred) == (0, 0, 0)
    expected = {
        "insight:acme-renewal": ("insight", "acme-renewal"),
        "procedure:close-monthly-books": ("procedure", "close-monthly-books"),
        "entity:acme-corp": ("entity", "acme-corp"),
    }
    assert sorted(publisher.references) == sorted(expected)
    for call in publisher.calls:
        kind, item_id = expected[call.reference]
        text = render_candidate(
            {"insight": env.insights, "procedure": env.procedures, "entity": env.entities}[
                kind
            ].read(item_id)
        )
        assert call.content_sha256 == text.content_sha256
        assert call.confidence == pytest.approx(0.97)
        assert call.classifier_version == _MODEL
        row = env.ledger.latest(kind, item_id)
        assert row is not None
        assert (row.decision, row.publish_state, row.shared_ref) == (
            "promote",
            "published",
            f"shared:{call.reference}",
        )


async def test_sweep_keeps_personal_item_private_and_never_publishes_it(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    _add_insight(env, "kid-recital", "The recital is on Friday at six.")
    classifier = FakeClassifier(env.timeline, labels={"kid-recital": "personal"})
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_1)

    assert result.status == "completed"
    assert (result.promoted, result.kept_private) == (1, 1)
    assert publisher.references == ["insight:acme-renewal"]
    row = env.ledger.latest("insight", "kid-recital")
    assert row is not None
    assert (row.decision, row.label, row.publish_state) == ("keep_private", "personal", "none")


async def test_classifier_receives_only_classifier_input_with_content_and_no_did(
    env: Env,
) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    _add_entity(env, "acme-corp", "ZEBRA-QUOKKA-3")
    classifier = FakeClassifier(env.timeline)

    await env.sweep(classifier=classifier).run(_NIGHT_1)

    assert len(classifier.inputs) == 2
    for item in classifier.inputs:
        assert type(item) is ClassifierInput
        dumped = item.model_dump()
        assert set(dumped) == {"item_kind", "item_id", "content"}
        assert dumped["content"] == _content_of(env, item.item_kind, item.item_id)
        assert _DID not in json.dumps(dumped)
        assert str(env.workspace) not in json.dumps(dumped)


# -- pre-egress gates: secret, size, count cap ---------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "Deploy uses ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8 for the release job.",
        "The vendor portal api_key lives in the team wiki.",
    ],
    ids=["github-token", "api-key-word"],
)
async def test_secret_item_never_reaches_classifier_and_is_blocked(
    env: Env, statement: str
) -> None:
    _add_insight(env, "leaky", statement)
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)
    sink = RecordingSink(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher, sink=sink).run(_NIGHT_1)

    assert result.blocked_secret == 1
    assert classifier.sent_ids == ["acme-renewal"]
    assert all(statement not in item.content for item in classifier.inputs)
    assert "insight:leaky" not in publisher.references
    row = env.ledger.latest("insight", "leaky")
    assert row is not None
    assert (row.decision, row.classifier_id, row.label) == ("blocked_secret", None, None)
    (egress,) = sink.actions("memory.promotion.egress")
    assert all("leaky" not in str(item_id) for item_id in egress.extra["item_ids"])


async def test_oversize_item_is_too_large_and_never_sent(env: Env) -> None:
    _add_insight(env, "huge", "Acme pricing detail. " * 20)  # ~420 bytes
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(
        cfg=PromotionConfig(enabled=True, max_item_bytes=200),
        classifier=classifier,
        publisher=publisher,
    ).run(_NIGHT_1)

    assert result.too_large == 1
    assert classifier.sent_ids == ["acme-renewal"]
    assert "insight:huge" not in publisher.references
    row = env.ledger.latest("insight", "huge")
    assert row is not None
    assert row.decision == "too_large"


async def test_too_large_item_is_rechecked_when_the_byte_cap_is_raised(env: Env) -> None:
    _add_insight(env, "huge", "Acme pricing detail. " * 20)  # ~420 bytes
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    def night(max_item_bytes: int) -> PromotionSweep:
        return env.sweep(
            cfg=PromotionConfig(enabled=True, max_item_bytes=max_item_bytes),
            classifier=classifier,
            publisher=publisher,
        )

    await night(200).run(_NIGHT_1)
    await night(200).run(_NIGHT_2)
    assert classifier.sent_ids == []

    await night(4096).run(_NIGHT_3)
    assert classifier.sent_ids == ["huge"]


async def test_over_cap_items_are_deferred_and_sent_next_night(env: Env) -> None:
    ids = {_add_insight(env, f"deal-{n}", f"Deal {n} closes at ${n}0k/yr.") for n in (1, 2, 3)}
    cfg = PromotionConfig(enabled=True, max_items_per_sweep=2)
    first = FakeClassifier(env.timeline)

    night_1 = await env.sweep(cfg=cfg, classifier=first).run(_NIGHT_1)

    assert (night_1.evaluated, night_1.deferred) == (2, 1)
    assert len(first.inputs) == 2

    second = FakeClassifier(env.timeline)
    night_2 = await env.sweep(cfg=cfg, classifier=second).run(_NIGHT_2)

    assert (night_2.evaluated, night_2.deferred) == (1, 0)
    assert set(first.sent_ids) | set(second.sent_ids) == ids
    assert set(first.sent_ids).isdisjoint(second.sent_ids)


# -- egress + decision audit ---------------------------------------------------


async def test_egress_audit_is_durable_before_first_classify_with_required_fields(
    env: Env,
) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    _add_procedure(env, "close-monthly-books", "ZEBRA-QUOKKA-2")
    sink = RecordingSink(env.timeline)
    classifier = FakeClassifier(env.timeline)

    await env.sweep(classifier=classifier, sink=sink).run(_NIGHT_1)

    durable_egress = [e for e in sink.durable if e.action == "memory.promotion.egress"]
    assert len(durable_egress) == 1
    assert len(sink.actions("memory.promotion.egress")) == 1
    egress_at = env.timeline.index(("durable", "memory.promotion.egress"))
    first_classify = next(i for i, (kind, _) in enumerate(env.timeline) if kind == "classify")
    assert egress_at < first_classify
    extra = durable_egress[0].extra
    assert {
        "count",
        "item_ids",
        "total_bytes",
        "classifier_id",
        "classifier_version",
        "host",
        "question_version",
    } <= set(extra)
    assert extra["count"] == 2
    assert sorted(extra["item_ids"]) == sorted(classifier.sent_ids)
    assert extra["total_bytes"] == sum(len(i.content.encode("utf-8")) for i in classifier.inputs)
    assert (extra["classifier_id"], extra["classifier_version"]) == ("jev", _MODEL)
    assert extra["question_version"] == _QV


async def test_failing_durable_egress_write_makes_zero_classifier_calls(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(
        classifier=classifier,
        publisher=publisher,
        sink=RecordingSink(env.timeline, fail_durable=True),
    ).run(_NIGHT_1)

    assert result.status == "classifier_error"
    assert classifier.inputs == []
    assert publisher.calls == []
    assert env.ledger.latest("insight", "acme-renewal") is None


async def test_sink_without_durable_write_refuses_egress(env: Env) -> None:
    """Adversarial: a non-durable sink cannot carry the pre-egress record."""
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher, sink=WriteOnlySink()).run(
        _NIGHT_1
    )

    assert result.status == "classifier_error"
    assert classifier.inputs == []
    assert publisher.calls == []


async def test_one_decision_audit_per_classified_item_and_one_sweep_event(env: Env) -> None:
    ids = _seed_five_insights(env)
    sink = RecordingSink(env.timeline)
    classifier = FakeClassifier(env.timeline, labels={"deal-2": "personal"})

    result = await env.sweep(classifier=classifier, sink=sink).run(_NIGHT_1)

    decisions = sink.actions("memory.promotion.decision")
    decided_ids = [event.extra["item_id"] for event in decisions]
    assert sorted(decided_ids) == sorted(ids)
    by_id = {event.extra["item_id"]: event.extra for event in decisions}
    assert by_id["deal-2"]["decision"] == "keep_private"
    assert by_id["deal-1"]["decision"] == "promote"
    for extra in by_id.values():
        assert {"item_kind", "content_sha256", "label", "confidence", "classifier_version"} <= set(
            extra
        )
    (sweep_event,) = sink.actions("memory.promotion.sweep")
    assert result.status == "completed"
    assert sweep_event.extra.get("evaluated") == 5


async def test_no_audit_event_or_ledger_row_carries_item_content(env: Env) -> None:
    _seed_five_insights(env)
    _add_procedure(env, "close-monthly-books", "ZEBRA-QUOKKA-P")
    _add_entity(env, "acme-corp", "ZEBRA-QUOKKA-E")
    _add_insight(env, "leaky", "The vendor api_key is ZEBRA-QUOKKA-S.")
    sink = RecordingSink(env.timeline)
    classifier = FakeClassifier(env.timeline, labels={"deal-3": "personal"})

    await env.sweep(classifier=classifier, sink=sink).run(_NIGHT_1)

    assert sink.events, "the sweep must audit"
    blobs = [event.model_dump_json() for event in sink.events] + [env.ledger_text()]
    for blob in blobs:
        assert "ZEBRA-QUOKKA" not in blob
        for item in classifier.inputs:
            assert item.content not in blob


# -- ledger: once per item -----------------------------------------------------


async def test_second_sweep_over_unchanged_memory_sends_nothing(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    _add_insight(env, "kid-recital", "The recital is on Friday at six.")
    _add_entity(env, "acme-corp", "ZEBRA-QUOKKA-3")
    labels = {"kid-recital": "personal"}
    await env.sweep(classifier=FakeClassifier(env.timeline, labels=labels)).run(_NIGHT_1)
    classifier = FakeClassifier(env.timeline, labels=labels)
    publisher = FakePublisher(env.timeline)
    sink = RecordingSink(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher, sink=sink).run(_NIGHT_2)

    assert classifier.inputs == []
    assert publisher.calls == []
    assert sink.actions("memory.promotion.egress") == []
    assert result.status == "completed"
    assert result.evaluated == 0


async def test_edited_item_is_resent_alone(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    _add_insight(env, "beta-renewal", "Beta renewal closes at $12k/yr.")
    await env.sweep().run(_NIGHT_1)
    _add_insight(env, "acme-renewal", "Acme renewal closes at $48k/yr after the uplift.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_2)

    assert classifier.sent_ids == ["acme-renewal"]
    assert "48k" in classifier.inputs[0].content
    assert publisher.references == ["insight:acme-renewal"]


async def test_forged_ledger_row_is_ignored_and_item_is_classified(env: Env) -> None:
    """Abuse: a row signed by a foreign key claiming 'already judged' is not a verdict."""
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    text = render_candidate(env.insights.read("acme-renewal"))  # type: ignore[arg-type]
    forger = PromotionLedger(env.workspace, InProcessSigner(b"\x09" * 32))
    forger.append(
        LedgerRow(
            item_kind="insight",
            item_id="acme-renewal",
            content_sha256=text.content_sha256,
            classifier_id="jev",
            classifier_version=_MODEL,
            question_version=_QV,
            decision="keep_private",
            label="personal",
            confidence=0.99,
            personal_probability=0.99,
            evaluated_at=_NIGHT_1,
            publish_state="none",
            shared_ref=None,
        )
    )
    classifier = FakeClassifier(env.timeline)

    await env.sweep(classifier=classifier).run(_NIGHT_1)

    assert classifier.sent_ids == ["acme-renewal"]


# -- classifier error: nothing publishes that night ----------------------------


async def test_classifier_error_on_third_of_five_publishes_nothing(env: Env) -> None:
    ids = _seed_five_insights(env)
    classifier = FakeClassifier(env.timeline, fail_on_call=3)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_1)

    assert result.status == "classifier_error"
    assert publisher.calls == []
    assert len(classifier.inputs) == 3
    recorded, failed = classifier.sent_ids[:2], classifier.sent_ids[2]
    for item_id in recorded:
        row = env.ledger.latest("insight", item_id)
        assert row is not None
        assert (row.decision, row.publish_state) == ("promote", "pending")
    for item_id in [failed, *(i for i in ids if i not in classifier.sent_ids)]:
        assert env.ledger.latest("insight", item_id) is None


async def test_next_successful_night_publishes_pending_without_resending(env: Env) -> None:
    ids = _seed_five_insights(env)
    failing = FakeClassifier(env.timeline, fail_on_call=3)
    await env.sweep(classifier=failing).run(_NIGHT_1)
    pending = failing.sent_ids[:2]
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_2)

    assert result.status == "completed"
    assert set(classifier.sent_ids) == set(ids) - set(pending)
    assert sorted(publisher.references) == sorted(f"insight:{i}" for i in ids)
    for item_id in ids:
        row = env.ledger.latest("insight", item_id)
        assert row is not None
        assert row.publish_state == "published"


# -- publisher outcomes --------------------------------------------------------


async def test_publisher_refusal_on_hash_mismatch_leaves_row_unpublished_and_reevaluates(
    env: Env,
) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")

    def _edit_underneath(reference: str) -> None:
        # The card changes between classify and publish; the shared side refuses.
        _add_insight(env, "acme-renewal", "Acme renewal now closes at $51k/yr.")

    refusing = FakePublisher(env.timeline, mode="refuse", before=_edit_underneath)
    await env.sweep(publisher=refusing).run(_NIGHT_1)

    row = env.ledger.latest("insight", "acme-renewal")
    assert row is not None
    assert row.publish_state != "published"
    assert row.shared_ref is None

    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)
    await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_2)

    assert classifier.sent_ids == ["acme-renewal"]
    assert "51k" in classifier.inputs[0].content
    new_hash = render_candidate(env.insights.read("acme-renewal")).content_sha256  # type: ignore[arg-type]
    assert [(c.reference, c.content_sha256) for c in publisher.calls] == [
        ("insight:acme-renewal", new_hash)
    ]
    latest = env.ledger.latest("insight", "acme-renewal")
    assert latest is not None
    assert latest.publish_state == "published"


async def test_publisher_outcome_unknown_is_recorded_and_never_retried(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    uncertain = FakePublisher(env.timeline, mode="unknown")
    await env.sweep(publisher=uncertain).run(_NIGHT_1)

    row = env.ledger.latest("insight", "acme-renewal")
    assert row is not None
    assert row.publish_state == "outcome_unknown"
    assert len(uncertain.calls) == 1

    for night in (_NIGHT_2, _NIGHT_3):
        classifier = FakeClassifier(env.timeline)
        publisher = FakePublisher(env.timeline)
        await env.sweep(classifier=classifier, publisher=publisher).run(night)
        assert classifier.inputs == []
        assert publisher.calls == []


async def test_stale_pending_row_is_never_published_after_the_item_changes(env: Env) -> None:
    """Adversarial: a pending row from an earlier night must not publish unjudged bytes.

    The item is edited and then deferred by the count cap, so its newest ledger row
    is still the old ``pending`` one. Publishing it now would ship either the old
    digest (which the card no longer has) or the new bytes under the old verdict.
    Every published digest must be one the classifier actually judged.
    """
    _add_insight(env, "c-deal", "Deal C closes at $30k/yr.")
    first = FakeClassifier(env.timeline)
    await env.sweep(classifier=first, publisher=FakePublisher(env.timeline, mode="refuse")).run(
        _NIGHT_1
    )
    stale = env.ledger.latest("insight", "c-deal")
    assert stale is not None
    assert stale.publish_state == "pending"
    _add_insight(env, "a-deal", "Deal A closes at $10k/yr.")
    _add_insight(env, "b-deal", "Deal B closes at $20k/yr.")
    _add_insight(env, "c-deal", "Deal C now closes at $35k/yr.")
    second = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(
        cfg=PromotionConfig(enabled=True, max_items_per_sweep=1),
        classifier=second,
        publisher=publisher,
    ).run(_NIGHT_2)

    assert result.deferred == 2
    judged = {
        (item.item_id, content_digest(item.content)) for item in first.inputs + second.inputs
    }
    for call in publisher.calls:
        assert (call.reference.partition(":")[2], call.content_sha256) in judged
        current = render_candidate(env.insights.read(call.reference.partition(":")[2]))  # type: ignore[arg-type]
        assert call.content_sha256 == current.content_sha256


# -- sticky decisions + operator decisions (alpha-2 item 16) -------------------

_OPERATOR = "did:arc:operator:approver/abcd1234"


def _demoted(shared_ref: str, reason: str = "stale pricing") -> dict[str, Demotion]:
    return {shared_ref: Demotion(shared_ref=shared_ref, decided_by=_OPERATOR, reason=reason)}


async def test_classifier_version_bump_does_not_rejudge(env: Env) -> None:
    """One durable decision per card version: a new model is not new facts."""
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    await env.sweep().run(_NIGHT_1)
    classifier = FakeClassifier(env.timeline)

    await env.sweep(
        cfg=PromotionConfig(enabled=True, classifier_model="jev-1.14"), classifier=classifier
    ).run(_NIGHT_2)

    assert classifier.inputs == []


async def test_operator_decided_card_is_skipped_on_second_pass(env: Env) -> None:
    """An operator share is final for the classifier — even after the card changes."""
    _add_insight(env, "kid-recital", "The recital is on Friday at six.")
    labels = {"kid-recital": "personal"}
    await env.sweep(classifier=FakeClassifier(env.timeline, labels=labels)).run(_NIGHT_1)
    shared = await env.sweep().share("insight", "kid-recital", decided_by=_OPERATOR, now=_NIGHT_1)
    assert shared.status == "published"
    _add_insight(env, "kid-recital", "The recital moved to Saturday at five.")
    classifier = FakeClassifier(env.timeline, labels=labels)
    publisher = FakePublisher(env.timeline)

    await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_2)

    assert classifier.inputs == []
    assert publisher.calls == []
    row = env.ledger.latest("insight", "kid-recital")
    assert row is not None
    assert (row.decision, row.decided_by) == ("promoted_by_operator", _OPERATOR)


async def test_operator_share_publishes_a_kept_private_card_without_the_classifier(
    env: Env,
) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    await env.sweep(
        classifier=FakeClassifier(env.timeline, labels={"acme-renewal": "personal"})
    ).run(_NIGHT_1)
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline)
    sink = RecordingSink(env.timeline)

    result = await env.sweep(classifier=classifier, publisher=publisher, sink=sink).share(
        "insight", "acme-renewal", decided_by=_OPERATOR, now=_NIGHT_2
    )

    assert (result.status, result.shared_ref) == ("published", "shared:insight:acme-renewal")
    assert classifier.inputs == []
    digest = render_candidate(env.insights.read("acme-renewal")).content_sha256  # type: ignore[arg-type]
    assert publisher.operator_calls == [("insight:acme-renewal", digest, _OPERATOR)]
    assert publisher.calls == []
    row = env.ledger.latest("insight", "acme-renewal")
    assert row is not None
    assert (row.decision, row.publish_state, row.shared_ref) == (
        "promoted_by_operator",
        "published",
        "shared:insight:acme-renewal",
    )
    [audit] = sink.actions("memory.promotion.operator_promote")
    assert (audit.outcome, audit.extra["decided_by"]) == ("published", _OPERATOR)


async def test_operator_share_of_a_secret_card_is_blocked_with_no_override(env: Env) -> None:
    """The secret gate has no operator override: nothing is published, ever."""
    _add_insight(env, "deploy-key", "The deploy password is hunter2 for the staging box.")
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(publisher=publisher).share(
        "insight", "deploy-key", decided_by=_OPERATOR, now=_NIGHT_1
    )

    assert result.status == "blocked_secret"
    assert publisher.operator_calls == []
    assert publisher.calls == []
    row = env.ledger.latest("insight", "deploy-key")
    assert row is not None
    assert row.decision == "blocked_secret"


async def test_operator_share_refused_by_the_shared_side_records_no_decision(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    refusing = FakePublisher(env.timeline, mode="refuse")

    result = await env.sweep(publisher=refusing).share(
        "insight", "acme-renewal", decided_by=_OPERATOR, now=_NIGHT_1
    )

    assert result.status == "refused"
    assert env.ledger.latest("insight", "acme-renewal") is None


async def test_operator_share_at_federal_tier_is_forbidden(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    publisher = FakePublisher(env.timeline)

    result = await env.sweep(tier="federal", publisher=publisher).share(
        "insight", "acme-renewal", decided_by=_OPERATOR, now=_NIGHT_1
    )

    assert result.status == "tier_forbidden"
    assert publisher.operator_calls == []


@pytest.mark.parametrize(("kind", "item_id"), [("event", "x"), ("insight", "../escape")])
async def test_operator_share_refuses_a_bad_reference(env: Env, kind: str, item_id: str) -> None:
    with pytest.raises(ValueError):
        await env.sweep().share(kind, item_id, decided_by=_OPERATOR, now=_NIGHT_1)


async def test_revoked_shared_ref_counts_as_demoted(env: Env) -> None:
    """A verified operator demotion of our shared copy becomes a sticky ledger row."""
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    await env.sweep().run(_NIGHT_1)
    publisher = FakePublisher(env.timeline, demoted=_demoted("shared:insight:acme-renewal"))
    sink = RecordingSink(env.timeline)

    result = await env.sweep(publisher=publisher, sink=sink).run(_NIGHT_2)

    assert result.demoted == 1
    row = env.ledger.latest("insight", "acme-renewal")
    assert row is not None
    assert (row.decision, row.decided_by, row.reason) == (
        "demoted_by_operator",
        _OPERATOR,
        "stale pricing",
    )
    decision = [e for e in sink.actions("memory.promotion.decision") if e.outcome == row.decision]
    assert decision and decision[0].extra["decided_by"] == _OPERATOR


async def test_demoted_card_is_never_resent_or_republished_after_an_edit(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    await env.sweep().run(_NIGHT_1)
    demoted = _demoted("shared:insight:acme-renewal")
    await env.sweep(publisher=FakePublisher(env.timeline, demoted=demoted)).run(_NIGHT_2)
    _add_insight(env, "acme-renewal", "Acme renewal now closes at $51k/yr.")
    classifier = FakeClassifier(env.timeline)
    # The tombstone is gone from the shared side: the ledger decision still holds.
    publisher = FakePublisher(env.timeline)

    await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_3)
    share = await env.sweep(publisher=publisher).share(
        "insight", "acme-renewal", decided_by=_OPERATOR, now=_NIGHT_3
    )

    assert classifier.inputs == []
    assert publisher.calls == []
    assert share.status == "demoted"
    assert publisher.operator_calls == []


async def test_unreadable_demotions_decide_nothing(env: Env) -> None:
    """Fail closed: a sweep that cannot see demotions sends and publishes nothing."""
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier(env.timeline)
    publisher = FakePublisher(env.timeline, demoted=None)
    publisher.demoted = None

    result = await env.sweep(classifier=classifier, publisher=publisher).run(_NIGHT_1)

    assert result.status == "publisher_unavailable"
    assert classifier.inputs == []
    assert env.ledger_text() == ""


async def test_history_lists_the_cards_decisions_oldest_first(env: Env) -> None:
    _add_insight(env, "acme-renewal", "Acme renewal closes at $42k/yr.")
    await env.sweep().run(_NIGHT_1)
    await env.sweep(
        publisher=FakePublisher(env.timeline, demoted=_demoted("shared:insight:acme-renewal"))
    ).run(_NIGHT_2)

    history = env.sweep().history("insight", "acme-renewal")

    assert [(row.decision, row.publish_state) for row in history] == [
        ("promote", "pending"),
        ("promote", "published"),
        ("demoted_by_operator", "none"),
    ]
