"""SPEC-083 T-1212 — memory promotion abuse battery (arcmemory side).

Each test is one attacker move against the nightly promotion sweep. The
README "Abuse cases" 1-6 plus the bounded-input and key-leak cases:

1. A secret in memory never egresses (zero bytes reach the classifier).
2. An out-of-distribution item that lands on ``unclear`` at 0.99 stays private,
   as does an internally inconsistent ``company`` verdict.
3. A classifier outage mid-batch promotes nothing that night, including items
   already classified ``company`` earlier in the same sweep.
4. Federal cannot be enabled by config, and a hand-composed federal sweep makes
   zero classifier calls.
5. A forged origin DID is refused by the exporter the publisher pulls from.
6. A replayed, stale, tampered, unsigned or foreign-signed ledger row is never
   read as a ``promote`` verdict.
7. The classifier key never appears in any audit event, ledger row, log record,
   result or console output across a full sweep (canary key).
8. Item count and item bytes stay bounded; a hung classifier is cut off.

Real objects: the card stores on a tmp workspace, the signed ledger, the
exporter, the secret gate, ``decide`` and — for 3 and 7 — the real
``ArcllmPromotionClassifier`` -> ``arcllm.classify`` -> Jev drop-in path.
Faked only at the boundary: the third-party ``typesafe_sdk`` module (planted in
``sys.modules``), the classifier (elsewhere), the shared-store publisher and the
audit sink. The shared-side cases (forged DID at the backend, entity block
copying, refused promote leaving no ``allow`` record) live in
``packages/arcteam/tests/security/test_memory_promotion_abuse_shared.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcprompt import StockPromptSource
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent
from arctrust.signer import InProcessSigner

from arcmemory import build_brain
from arcmemory.adapters.memory_export import ConsolidatedMemoryExporter
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.promotion.arcllm_classifier import ArcllmPromotionClassifier
from arcmemory.promotion.classifier import (
    ClassifierCallError,
    ClassifierInput,
    ClassifierVerdict,
    question_version,
)
from arcmemory.promotion.config import PromotionConfig, PromotionForbiddenAtTierError
from arcmemory.promotion.ledger import LedgerRow, PromotionLedger
from arcmemory.promotion.question import load_promotion_question
from arcmemory.promotion.render import render_candidate
from arcmemory.promotion.sweep import PromotionSweep, PromotionSweepResult
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Insight, Procedure, Scope, Step

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_DID = "did:arc:test-agent"
_MODEL = "jev-1.13.0"
_QV = question_version(PROMOTION_QUESTION)
_NIGHT_1 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
_NIGHT_2 = _NIGHT_1 + timedelta(days=1)
_LABELS = ("company", "personal", "agent_only", "unclear")

#: A GitHub-token-shaped value (matches ``arctrust.secrets`` GITHUB_TOKEN).
_GITHUB_TOKEN = "ghp_" + "Zq7Rk2Lm9Xw4Tb8Nc3Vd6Hf1Js5Pg0Ya2Ue7Io"
#: The canary API key. It must never be written anywhere Arc writes.
_CANARY_KEY = "tsk_live_CANARY_7f3e9a1b2c4d6e8f0a1b3c5d"


# -- boundary fakes -----------------------------------------------------------


def _verdict(
    label: str,
    *,
    confidence: float = 0.97,
    probabilities: dict[str, float] | None = None,
    personal: float = 0.02,
    version: str = _MODEL,
) -> ClassifierVerdict:
    if probabilities is None:
        probabilities = {lbl: 0.01 for lbl in _LABELS}
        probabilities[label] = 0.97
    return ClassifierVerdict(
        label=label,  # type: ignore[arg-type]  # reason: labels built from str in tests
        confidence=confidence,
        probabilities=probabilities,  # type: ignore[arg-type]  # reason: same
        personal_probability=personal,
        classifier_id="jev",
        classifier_version=version,
        request_id="req-1",
        input_tokens=12,
    )


class FakeClassifier:
    """Stand-in third-party classifier: records every input it is handed."""

    classifier_id = "jev"
    question_version = _QV
    host: str | None = "api.typesafe.test"

    def __init__(
        self,
        verdicts: dict[str, ClassifierVerdict] | None = None,
        *,
        fail_on_call: int | None = None,
        hang: bool = False,
    ) -> None:
        self._verdicts = verdicts or {}
        self._fail_on_call = fail_on_call
        self._hang = hang
        self.inputs: list[ClassifierInput] = []

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.inputs.append(item)
        if self._hang:
            await asyncio.Event().wait()
        if self._fail_on_call is not None and len(self.inputs) == self._fail_on_call:
            raise ClassifierCallError("529 overloaded")
        return self._verdicts.get(item.item_id, _verdict("company"))

    @property
    def sent_ids(self) -> list[str]:
        return [item.item_id for item in self.inputs]

    @property
    def sent_text(self) -> str:
        return "\n".join(item.content for item in self.inputs)


class FakePublisher:
    def __init__(self) -> None:
        self.references: list[str] = []

    async def publish(
        self,
        reference: str,
        *,
        content_sha256: str,
        confidence: float,
        classifier_version: str,
        classification: str,
    ) -> str:
        self.references.append(reference)
        return f"shared:{reference}"

    async def demotions(self) -> dict[str, object]:
        return {}


class RecordingSink:
    """``AuditSink`` + ``DurableAuditSink`` recorder."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]

    def dump(self) -> str:
        return "\n".join(event.model_dump_json() for event in self.events)


@dataclass
class _FakeTypeSafe:
    """A stand-in ``typesafe_sdk`` module with the surface the Jev drop-in uses.

    ``respond(state)`` returns ``(label, probabilities, noul)`` or raises. Like
    the real SDK it logs request bodies at debug on the ``typesafe_sdk`` logger
    — here including the key — so a regression in the drop-in's silencing is
    caught rather than assumed.
    """

    respond: Callable[[str], tuple[str, dict[str, float], float]]
    api_keys: list[str | None] = field(default_factory=list)
    states: list[str] = field(default_factory=list)

    def module(self) -> types.ModuleType:
        fake = self
        sdk = types.ModuleType("typesafe_sdk")

        class TypeSafeAPIError(Exception):
            def __init__(self, status: int, request_id: str | None = None) -> None:
                self.status = status
                self.request_id = request_id
                super().__init__(f"TypeSafe API error {status}")

        class RetryPolicy:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        class AsyncTypeSafeClient:
            def __init__(self, *, api_key: str | None = None, model: str | None = None, **_: Any):
                fake.api_keys.append(api_key)
                self._api_key = api_key
                self._model = model

            async def __aenter__(self) -> AsyncTypeSafeClient:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

            async def system_one(self, state: str, questions: dict[str, Any]) -> Any:
                fake.states.append(state)
                logging.getLogger("typesafe_sdk").debug(
                    "POST /v1/systemone key=%s body=%s", self._api_key, state
                )
                label, probabilities, noul = fake.respond(state)
                return SimpleNamespace(
                    model=self._model,
                    usage=SimpleNamespace(input_tokens=11),
                    request_id="req_fake_1",
                    answers={
                        "scope": SimpleNamespace(
                            type="choice",
                            choice=label,
                            confidence=probabilities[label],
                            probabilities=probabilities,
                        ),
                        "personal_check": SimpleNamespace(type="noul", noul=noul),
                    },
                )

        sdk.TypeSafeAPIError = TypeSafeAPIError  # type: ignore[attr-defined]  # reason: fake module
        sdk.RetryPolicy = RetryPolicy  # type: ignore[attr-defined]  # reason: fake module
        sdk.AsyncTypeSafeClient = AsyncTypeSafeClient  # type: ignore[attr-defined]  # reason: same
        return sdk


def _company(_: str) -> tuple[str, dict[str, float], float]:
    return (
        "company",
        {"company": 0.985, "personal": 0.005, "agent_only": 0.005, "unclear": 0.005},
        0.02,
    )


# -- environment --------------------------------------------------------------


@dataclass
class Env:
    workspace: Path
    insights: InsightStore
    procedures: ProceduralStore
    entities: SemanticStore
    ledger: PromotionLedger
    exporter: ConsolidatedMemoryExporter
    stores: Any

    def sweep(
        self,
        classifier: Any,
        *,
        publisher: Any = None,
        sink: RecordingSink | None = None,
        cfg: PromotionConfig | None = None,
        tier: str = "personal",
    ) -> PromotionSweep:
        return PromotionSweep(
            cfg=cfg if cfg is not None else PromotionConfig(enabled=True),
            tier=tier,
            stores=self.stores,
            ledger=self.ledger,
            classifier=classifier,
            publisher=publisher if publisher is not None else FakePublisher(),
            audit_sink=sink if sink is not None else RecordingSink(),
            clearance="unclassified",
            agent_did=_DID,
        )

    def ledger_path(self) -> Path:
        return self.workspace / "memory" / "promotion" / "ledger.jsonl"

    def ledger_text(self) -> str:
        path = self.ledger_path()
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def add_insight(self, item_id: str, statement: str) -> str:
        self.insights.write(Insight(id=item_id, statement=statement, trigger="a renewal"))
        return item_id

    def digest(self, item_id: str) -> str:
        item = self.insights.read(item_id)
        assert item is not None
        return render_candidate(item).content_sha256


@pytest.fixture
def env(workspace: Path, db: MemoryDB, scope: Scope) -> Env:
    insights = InsightStore(workspace)
    procedures = ProceduralStore(workspace)
    entities = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    stores = SimpleNamespace(insights=insights, procedures=procedures, entities=entities)
    return Env(
        workspace=workspace,
        insights=insights,
        procedures=procedures,
        entities=entities,
        ledger=PromotionLedger(workspace, InProcessSigner(b"\x01" * 32)),
        exporter=ConsolidatedMemoryExporter(workspace, _DID, stores),
        stores=stores,
    )


def _row(
    item_id: str,
    digest: str,
    *,
    decision: str = "promote",
    label: str = "company",
    publish_state: str = "pending",
    classifier_version: str = _MODEL,
    question: str = _QV,
) -> LedgerRow:
    return LedgerRow(
        item_kind="insight",
        item_id=item_id,
        content_sha256=digest,
        classifier_id="jev",
        classifier_version=classifier_version,
        question_version=question,
        decision=decision,  # type: ignore[arg-type]  # reason: built from str
        label=label,  # type: ignore[arg-type]  # reason: built from str
        confidence=0.99,
        personal_probability=0.01,
        evaluated_at=_NIGHT_1,
        publish_state=publish_state,  # type: ignore[arg-type]  # reason: built from str
        shared_ref=None,
    )


def _append_raw(env: Env, payload: dict[str, Any]) -> None:
    path = env.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


# ============================================================================
# 1. A secret in memory never egresses
# ============================================================================


@pytest.mark.parametrize(
    "statement",
    [
        f"Deploy the Acme sync with {_GITHUB_TOKEN} on the build box.",
        "The Acme portal api_key lives in the ops runbook.",
    ],
    ids=["github-token-value", "api_key-keyword"],
)
async def test_secret_insight_sends_zero_bytes_and_is_blocked(env: Env, statement: str) -> None:
    env.add_insight("acme-secret", statement)
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr, net-60.")
    classifier = FakeClassifier()
    publisher = FakePublisher()
    sink = RecordingSink()

    result = await env.sweep(classifier, publisher=publisher, sink=sink).run(_NIGHT_1)

    assert classifier.sent_ids == ["acme-renewal"]
    assert statement not in classifier.sent_text
    assert _GITHUB_TOKEN not in classifier.sent_text
    assert result.blocked_secret == 1
    row = env.ledger.latest("insight", "acme-secret")
    assert row is not None
    assert (row.decision, row.publish_state) == ("blocked_secret", "none")
    assert publisher.references == ["insight:acme-renewal"]
    egress = sink.actions("memory.promotion.egress")
    assert [event.extra["item_ids"] for event in egress] == [["acme-renewal"]]
    assert _GITHUB_TOKEN not in sink.dump() and _GITHUB_TOKEN not in env.ledger_text()


async def test_secret_in_entity_fact_and_procedure_step_never_egresses(env: Env) -> None:
    env.entities.write_fact(
        "acme-db", "connection", "postgres://ops:hunter2@db.acme/prod", name="Acme DB"
    )
    env.procedures.write(
        Procedure(
            slug="rotate-acme",
            title="Rotate the Acme deploy key",
            when_to_use="When the deploy key expires.",
            steps=[Step(text=f"Paste {_GITHUB_TOKEN} into the vault")],
        )
    )
    classifier = FakeClassifier()
    publisher = FakePublisher()

    result = await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert classifier.inputs == []
    assert result.blocked_secret == 2
    assert publisher.references == []


async def test_blocked_secret_stays_blocked_on_later_nights(env: Env) -> None:
    env.add_insight("acme-secret", f"Token {_GITHUB_TOKEN}")
    await env.sweep(FakeClassifier()).run(_NIGHT_1)
    classifier = FakeClassifier()

    await env.sweep(classifier).run(_NIGHT_2)

    assert classifier.inputs == []


# ============================================================================
# 2. OOD / inconsistent verdicts stay private
# ============================================================================


@pytest.mark.parametrize(
    "verdict",
    [
        _verdict("unclear", confidence=0.99),
        _verdict(
            "company",
            confidence=0.99,
            probabilities={"company": 0.02, "personal": 0.01, "agent_only": 0.01, "unclear": 0.96},
        ),
        _verdict("company", confidence=0.93),
        _verdict("company", confidence=0.99, personal=0.40),
        _verdict("personal", confidence=0.99),
        _verdict("agent_only", confidence=0.99),
        _verdict("company", confidence=0.99, version="jev-latest-shadow"),
    ],
    ids=[
        "unclear@0.99",
        "company-label-but-company-prob-0.02",
        "company@0.93-below-threshold",
        "company-but-noul-personal-0.40",
        "personal@0.99",
        "agent_only@0.99",
        "company-from-unpinned-model",
    ],
)
async def test_non_company_or_untrustworthy_verdict_stays_private(
    env: Env, verdict: ClassifierVerdict
) -> None:
    env.add_insight("odd-item", "Zorblax quintessence glimmers at dawn.")
    publisher = FakePublisher()

    result = await env.sweep(FakeClassifier({"odd-item": verdict}), publisher=publisher).run(
        _NIGHT_1
    )

    assert (result.status, result.promoted, result.kept_private) == ("completed", 0, 1)
    assert publisher.references == []
    row = env.ledger.latest("insight", "odd-item")
    assert row is not None
    assert (row.decision, row.publish_state) == ("keep_private", "none")


# ============================================================================
# 3. A classifier outage promotes nothing that night
# ============================================================================


async def test_outage_mid_batch_publishes_nothing_then_next_night_publishes_once(
    env: Env,
) -> None:
    ids = [env.add_insight(f"deal-{n}", f"Deal {n} closes at ${n}0k/yr.") for n in range(1, 6)]
    failing = FakeClassifier(fail_on_call=3)
    publisher = FakePublisher()

    night_1 = await env.sweep(failing, publisher=publisher).run(_NIGHT_1)

    assert night_1.status == "classifier_error"
    assert night_1.promoted == 0
    assert publisher.references == []
    earlier = failing.sent_ids[:2]
    for item_id in earlier:
        row = env.ledger.latest("insight", item_id)
        assert row is not None and (row.decision, row.publish_state) == ("promote", "pending")

    healthy = FakeClassifier()
    night_2 = await env.sweep(healthy, publisher=publisher).run(_NIGHT_2)

    assert night_2.status == "completed"
    assert sorted(publisher.references) == sorted(f"insight:{i}" for i in ids)
    assert not set(earlier) & set(healthy.sent_ids), "judged items were re-sent"


async def test_real_jev_path_529_mid_batch_publishes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abuse 3 over the real arcllm -> Jev drop-in path: the SDK raises 529 on call 3."""
    calls: list[str] = []
    fake = _FakeTypeSafe(respond=_company)
    sdk = fake.module()

    def respond(state: str) -> tuple[str, dict[str, float], float]:
        calls.append(state)
        if len(calls) == 3:
            raise sdk.TypeSafeAPIError(529, request_id="req_overloaded")
        return _company(state)

    fake.respond = respond
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    monkeypatch.setenv("TYPESAFE_API_KEY", _CANARY_KEY)
    for n in range(1, 6):
        env.add_insight(f"deal-{n}", f"Deal {n} closes at ${n}0k/yr.")
    publisher = FakePublisher()
    classifier = ArcllmPromotionClassifier(provider="jev", model=_MODEL, timeout=5.0)

    result = await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert result.status == "classifier_error"
    assert len(calls) == 3
    assert publisher.references == []


# ============================================================================
# 4. Federal cannot be enabled
# ============================================================================


@pytest.mark.parametrize("tier", ["federal", "FEDERAL", " Federal "])
def test_federal_promotion_config_is_refused(tier: str) -> None:
    with pytest.raises(PromotionForbiddenAtTierError):
        PromotionConfig.for_tier(tier, enabled=True)


@pytest.mark.parametrize("tier", ["federal", " Federal "])
def test_build_brain_refuses_enabled_promotion_at_federal(tmp_path: Path, tier: str) -> None:
    identity = AgentIdentity.generate("test", "fed")
    context = {
        "workspace": tmp_path / "fed-agent",
        "agent_did": identity.did,
        "tier": tier,
        "audit_sink": RecordingSink(),
        "identity": identity,
        "policy_pipeline": None,
        "backend_config": {"embed_backend": "none"},
        "promotion_config": {"enabled": True},
        "promotion_publisher": FakePublisher(),
    }

    with pytest.raises(PromotionForbiddenAtTierError):
        build_brain(context)


@pytest.mark.parametrize("tier", ["federal", "FEDERAL", " federal "])
async def test_hand_composed_federal_sweep_makes_zero_classifier_calls(
    env: Env, tier: str
) -> None:
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr.")
    classifier = FakeClassifier()
    publisher = FakePublisher()
    sink = RecordingSink()

    result = await env.sweep(classifier, publisher=publisher, sink=sink, tier=tier).run(_NIGHT_1)

    assert result == PromotionSweepResult(status="tier_forbidden")
    assert classifier.inputs == []
    assert publisher.references == []
    assert sink.actions("memory.promotion.egress") == []
    assert not env.ledger_path().exists()


# ============================================================================
# 5. Forged origin DID (arcmemory side: the exporter the publisher pulls from)
# ============================================================================


async def test_exporter_refuses_a_caller_claiming_another_agents_did(env: Env) -> None:
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr.")
    forged = SimpleNamespace(caller_did="did:arc:someone-else", clearance="unclassified")

    with pytest.raises(PermissionError):
        await env.exporter.export_for_promotion("insight:acme-renewal", forged)


async def test_exporter_serves_the_owning_agent(env: Env) -> None:
    """Narrowness pair for the refusal above: the owner still exports."""
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr.")
    owner = SimpleNamespace(caller_did=_DID, clearance="unclassified")

    source = await env.exporter.export_for_promotion("insight:acme-renewal", owner)

    assert source.digest == env.digest("acme-renewal")


# ============================================================================
# 6. Replayed / stale / forged ledger verdicts are never reused
# ============================================================================


async def test_pending_row_for_old_bytes_does_not_publish_edited_bytes(env: Env) -> None:
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr.")
    env.ledger.append(_row("acme-renewal", env.digest("acme-renewal")))
    env.add_insight("acme-renewal", "My daughter's recital is Friday.")  # H -> H'
    classifier = FakeClassifier({"acme-renewal": _verdict("personal")})
    publisher = FakePublisher()

    await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert classifier.sent_ids == ["acme-renewal"], "edited bytes were not re-judged"
    assert publisher.references == []


async def test_tampered_row_flipped_to_promote_is_ignored(env: Env) -> None:
    env.add_insight("diary", "My daughter's recital is Friday.")
    env.ledger.append(
        _row(
            "diary",
            env.digest("diary"),
            decision="keep_private",
            label="personal",
            publish_state="none",
        )
    )
    line = json.loads(env.ledger_text().splitlines()[-1])
    line.update(decision="promote", label="company", publish_state="pending")
    env.ledger_path().write_text(json.dumps(line, sort_keys=True) + "\n", encoding="utf-8")
    classifier = FakeClassifier({"diary": _verdict("personal")})
    publisher = FakePublisher()

    await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert publisher.references == []
    assert classifier.sent_ids == ["diary"], "tampered row was trusted as a verdict"


@pytest.mark.parametrize(
    "signature", ["", "00" * 64, "not-hex"], ids=["empty", "zeros", "garbage"]
)
async def test_unsigned_or_garbage_signed_promote_row_is_ignored(env: Env, signature: str) -> None:
    env.add_insight("diary", "My daughter's recital is Friday.")
    payload = _row("diary", env.digest("diary")).model_dump(mode="json")
    payload["signature"] = signature
    _append_raw(env, payload)
    classifier = FakeClassifier({"diary": _verdict("personal")})
    publisher = FakePublisher()

    await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert publisher.references == []
    assert classifier.sent_ids == ["diary"]


async def test_foreign_signed_pending_promote_row_is_never_published(env: Env) -> None:
    env.add_insight("diary", "My daughter's recital is Friday.")
    PromotionLedger(env.workspace, InProcessSigner(b"\x09" * 32)).append(
        _row("diary", env.digest("diary"))
    )
    classifier = FakeClassifier({"diary": _verdict("personal")})
    publisher = FakePublisher()

    await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert publisher.references == []
    assert classifier.sent_ids == ["diary"]


async def test_valid_row_replayed_onto_another_item_is_ignored(env: Env) -> None:
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr.")
    env.add_insight("diary", "Acme renewal closes at $42k/yr.")  # same bytes, other item
    env.ledger.append(_row("acme-renewal", env.digest("acme-renewal")))
    line = json.loads(env.ledger_text().splitlines()[-1])
    line["item_id"] = "diary"
    _append_raw(env, line)
    classifier = FakeClassifier({"diary": _verdict("personal")})
    publisher = FakePublisher()

    await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert "diary" in classifier.sent_ids
    assert "insight:diary" not in publisher.references


@pytest.mark.parametrize(
    "stale",
    [{"classifier_version": "jev-1.12"}, {"question": "sha256:" + "0" * 64}],
    ids=["other-classifier-version", "other-question-version"],
)
async def test_verdict_from_other_classifier_or_question_version_holds_for_same_bytes(
    env: Env, stale: dict[str, str]
) -> None:
    """Alpha-2 item 16: one durable decision per card version.

    A model or question bump is not new facts, so the card's bytes are never
    re-sent to the third-party classifier and a private card never flips public.
    """
    env.add_insight("diary", "My daughter's recital is Friday.")
    env.ledger.append(
        _row(
            "diary",
            env.digest("diary"),
            decision="keep_private",
            label="personal",
            publish_state="none",
            **stale,
        )
    )
    classifier = FakeClassifier({"diary": _verdict("company")})
    publisher = FakePublisher()

    await env.sweep(classifier, publisher=publisher).run(_NIGHT_1)

    assert classifier.sent_ids == []
    assert publisher.references == []


# ============================================================================
# 7. The classifier key never leaks (canary key, full sweep, real Jev path)
# ============================================================================


async def _canary_sweep(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[str], tuple[str, dict[str, float], float]],
    caplog: pytest.LogCaptureFixture,
) -> tuple[_FakeTypeSafe, PromotionSweepResult, RecordingSink]:
    fake = _FakeTypeSafe(respond=respond)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", fake.module())
    monkeypatch.setenv("TYPESAFE_API_KEY", _CANARY_KEY)
    # Also listen on the SDK logger itself: a regression in the drop-in's
    # silencing would surface here even though it stops propagation.
    sdk_logger = logging.getLogger("typesafe_sdk")
    monkeypatch.setattr(sdk_logger, "handlers", [*sdk_logger.handlers, caplog.handler])
    caplog.set_level(logging.DEBUG)
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr, net-60.")
    env.add_insight("acme-secret", f"Deploy with {_GITHUB_TOKEN}.")
    env.entities.write_fact("acme", "renewal", "net-60", name="Acme Corp")
    sink = RecordingSink()
    classifier = ArcllmPromotionClassifier(provider="jev", model=_MODEL, timeout=5.0)
    result = await env.sweep(classifier, sink=sink).run(_NIGHT_1)
    return fake, result, sink


def _assert_canary_absent(
    env: Env,
    result: PromotionSweepResult,
    sink: RecordingSink,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    out, err = capfd.readouterr()
    logs = "\n".join(f"{record.getMessage()} {record.exc_text or ''}" for record in caplog.records)
    surfaces = {
        "audit": sink.dump(),
        "ledger": env.ledger_text(),
        "logs": logs,
        "result": repr(result),
        "stdout": out,
        "stderr": err,
    }
    leaked = [name for name, text in surfaces.items() if _CANARY_KEY in text]
    assert not leaked, f"classifier key leaked into: {leaked}"


async def test_canary_key_never_written_across_a_successful_sweep(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    fake, result, sink = await _canary_sweep(env, monkeypatch, _company, caplog)

    assert result.status == "completed" and result.promoted == 2
    assert fake.api_keys and set(fake.api_keys) == {_CANARY_KEY}, "key never reached the SDK"
    _assert_canary_absent(env, result, sink, caplog, capfd)


async def test_canary_key_never_written_when_the_sdk_error_echoes_it(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def respond(_: str) -> tuple[str, dict[str, float], float]:
        raise RuntimeError(f"401 unauthorized for api_key={_CANARY_KEY}")

    fake, result, sink = await _canary_sweep(env, monkeypatch, respond, caplog)

    assert result.status == "classifier_error"
    assert fake.api_keys == [_CANARY_KEY]
    _assert_canary_absent(env, result, sink, caplog, capfd)


# ============================================================================
# 8. Bounded inputs: item count, item bytes, hung classifier
# ============================================================================


async def test_item_count_is_capped_per_sweep_and_the_rest_deferred(env: Env) -> None:
    for n in range(25):
        env.add_insight(f"deal-{n:02d}", f"Deal {n} closes at ${n}k/yr.")
    classifier = FakeClassifier()
    sink = RecordingSink()
    cfg = PromotionConfig(enabled=True, max_items_per_sweep=4)

    result = await env.sweep(classifier, sink=sink, cfg=cfg).run(_NIGHT_1)

    assert len(classifier.inputs) == 4
    assert (result.evaluated, result.deferred) == (4, 21)
    (egress,) = sink.actions("memory.promotion.egress")
    assert egress.extra["count"] == 4


async def test_multibyte_item_over_the_byte_cap_is_never_sent(env: Env) -> None:
    """The cap is UTF-8 bytes, not characters: 3000 x 'é' is 3000 chars, 6000 bytes."""
    env.add_insight("wide", "é" * 3000)
    env.add_insight("huge", "Acme " * 300_000)  # ~1.5 MB, under the 2 MB store cap
    classifier = FakeClassifier()
    cfg = PromotionConfig(enabled=True, max_item_bytes=4096)

    result = await env.sweep(classifier, cfg=cfg).run(_NIGHT_1)

    assert classifier.inputs == []
    assert result.too_large == 2


async def test_item_exactly_at_the_byte_cap_is_sent(env: Env) -> None:
    """Narrowness pair: the cap refuses only what is over it."""
    env.add_insight("edge", "a" * 4096)
    classifier = FakeClassifier()
    cfg = PromotionConfig(enabled=True, max_item_bytes=4096)

    await env.sweep(classifier, cfg=cfg).run(_NIGHT_1)

    assert classifier.sent_ids == ["edge"]


async def test_hung_classifier_is_cut_off_by_the_request_timeout(env: Env) -> None:
    env.add_insight("acme-renewal", "Acme renewal closes at $42k/yr.")
    env.add_insight("acme-terms", "Acme pays net-60.")
    classifier = FakeClassifier(hang=True)
    publisher = FakePublisher()
    cfg = PromotionConfig(enabled=True, request_timeout_seconds=0.05)

    result = await asyncio.wait_for(
        env.sweep(classifier, publisher=publisher, cfg=cfg).run(_NIGHT_1), timeout=5.0
    )

    assert result.status == "classifier_error"
    assert len(classifier.inputs) == 1, "sweep kept sending after a hang"
    assert publisher.references == []
