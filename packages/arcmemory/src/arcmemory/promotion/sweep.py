"""The nightly promotion sweep (SPEC-083 COMP-018 / COMP-020).

Runs at the end of ``Consolidator.run_hygiene``. For every insight, procedure and
entity card it decides — once per content version — whether the card may leave
this agent for the fleet's shared store. Episodic events and daily logs are never
enumerated (REQ-486).

Phases, in order (each a named helper below):

1. **gate** — federal tier → ``tier_forbidden``; disabled → ``disabled``; no
   classifier, or one whose network-free ``ensure_available`` preflight fails
   (drop-in, SDK or key missing; bounded by ``request_timeout_seconds``) →
   ``classifier_unavailable``; no publisher → ``publisher_unavailable``.
   A gated sweep reads nothing, sends nothing and writes no egress record.
2. **plan** — enumerate, render, skip items the signed ledger already judged,
   secret gate, size gate, clearance gate, count cap (oldest file first, so every
   item eventually runs; the overflow is ``deferred``).
3. **egress audit** — one durable ``memory.promotion.egress`` record BEFORE the
   first classifier call. No durable sink, or a failed write → zero calls.
4. **classify** — sequential, one item at a time, each bounded by
   ``request_timeout_seconds``. Every verdict is decided, ledgered and audited as
   it lands. The first failure stops the sweep and nothing is published that
   night; rows already recorded stay ``pending``. A call failure is
   ``classifier_error``. Availability lost after preflight (the item was not
   sent) is ``classifier_unavailable`` plus a durable
   ``memory.promotion.egress_aborted`` naming what was and was not sent, so the
   egress "allow" is never the last word.
5. **publish** — every ``pending`` row whose signature verifies and whose freshly
   re-rendered bytes still hash to the judged digest, including rows an earlier
   failed night left behind.

No audit event and no ledger row ever carries item content.

Scale: sequential classify is intended and bounded by ``max_items_per_sweep``.
Store and ledger reads are synchronous file I/O, so each phase's disk work runs in
a worker thread to keep the event loop free. The ledger is read twice per sweep
(:meth:`PromotionLedger.latest_index`, once to plan and once before publishing),
so a sweep is O(items + ledger rows), never items x rows.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.classification import Classification, dominates, parse_classification

from arcmemory.promotion.classifier import (
    ClassifierCallError,
    ClassifierInput,
    ClassifierUnavailableError,
    ClassifierVerdict,
    PromotionClassifier,
    PromotionQuestionInvalidError,
)
from arcmemory.promotion.config import PromotionConfig, is_federal_tier
from arcmemory.promotion.decide import decide
from arcmemory.promotion.ledger import LedgerDecision, LedgerRow, PromotionLedger, is_current
from arcmemory.promotion.publisher import (
    PromotionPublisher,
    PublisherUnavailableError,
    PublishOutcomeUnknownError,
)
from arcmemory.promotion.render import PromotableKind, PromotionText, render_candidate
from arcmemory.promotion.secret_gate import contains_secret
from arcmemory.types import Entity, Insight, Procedure

_log = logging.getLogger(__name__)

SweepStatus = Literal[
    "disabled",
    "tier_forbidden",
    "classifier_unavailable",
    "classifier_error",
    "publisher_unavailable",
    "completed",
]

#: Actor recorded on audit events when the integrator did not name the agent.
_COMPONENT_ACTOR = "arcmemory.promotion"

#: Classifier call failures that stop the sweep with ``classifier_error``.
_CALL_ERRORS = (ClassifierCallError, TimeoutError)

#: Largest per-run cap an operator may ask a manual ("Run now") sweep for.
MAX_ITEMS_CEILING = 5000


def validate_max_items(max_items: object) -> int | None:
    """Return a manual run's cap unchanged, or raise when it is not an int in 1..5000.

    ``bool`` is refused explicitly (it is an ``int`` subclass). ``None`` means "use
    the configured ``max_items_per_sweep``".
    """
    if max_items is None:
        return None
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise TypeError("max_items must be an integer")
    if not 1 <= max_items <= MAX_ITEMS_CEILING:
        raise ValueError(f"max_items must be between 1 and {MAX_ITEMS_CEILING}")
    return max_items


# -- store seams ---------------------------------------------------------------


class _CardSource(Protocol):
    def read(self, item_id: str, /) -> Insight | Procedure | Entity | None: ...

    def path_for(self, item_id: str, /) -> Path: ...


class _InsightSource(_CardSource, Protocol):
    def all_ids(self) -> list[str]: ...


class _SlugSource(_CardSource, Protocol):
    def slugs(self) -> list[str]: ...


class PromotionStores(Protocol):
    """The three consolidated stores the sweep enumerates (never episodic/daily)."""

    @property
    def insights(self) -> _InsightSource: ...

    @property
    def procedures(self) -> _SlugSource: ...

    @property
    def entities(self) -> _SlugSource: ...


# -- results -------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionSweepResult:
    """What one sweep did. ``evaluated`` counts items actually sent to the classifier."""

    status: SweepStatus
    evaluated: int = 0
    promoted: int = 0
    kept_private: int = 0
    blocked_secret: int = 0
    too_large: int = 0
    deferred: int = 0


@dataclass
class _Plan:
    """The plan phase's output: pre-egress verdicts, the batch to send, carried rows."""

    blocked_secret: list[PromotionText] = field(default_factory=list)
    too_large: list[PromotionText] = field(default_factory=list)
    batch: list[PromotionText] = field(default_factory=list)
    deferred: int = 0
    carried_pending: list[LedgerRow] = field(default_factory=list)


@dataclass
class _Classified:
    """The classify phase's tally."""

    evaluated: int = 0
    kept_private: int = 0
    pending: list[LedgerRow] = field(default_factory=list)
    failure: SweepStatus | None = None


@dataclass(frozen=True)
class _Candidate:
    text: PromotionText
    modified: float


# -- the sweep -----------------------------------------------------------------


class PromotionSweep:
    """Classify and publish promotable memory cards for one agent, once per version."""

    def __init__(
        self,
        *,
        cfg: PromotionConfig,
        tier: str,
        stores: PromotionStores,
        ledger: PromotionLedger,
        classifier: PromotionClassifier | None,
        publisher: PromotionPublisher | None,
        audit_sink: AuditSink,
        clearance: str,
        agent_did: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._tier = tier
        self._stores = stores
        self._ledger = ledger
        self._classifier = classifier
        self._publisher = publisher
        self._audit = audit_sink
        # Parsed strictly up front: a bad clearance label is a wiring error.
        self._clearance = parse_classification(clearance, strict=True)
        self._actor = agent_did or _COMPONENT_ACTOR
        # One sweep at a time for this agent: the nightly hook and a manual run
        # share this lock, so an overlapping run waits and then plans against the
        # ledger the first one wrote — no item is ever sent twice. One sweep object
        # per agent, so the lock never blocks another agent.
        self._lock = asyncio.Lock()

    def bind_publisher(self, publisher: PromotionPublisher | None) -> None:
        """Replace the publisher; ``None`` makes the next sweep ``publisher_unavailable``.

        The gate reads the publisher once per sweep, so a sweep already past its gate
        finishes with the publisher it started with.
        """
        self._publisher = publisher

    async def run(self, now: datetime, *, max_items: int | None = None) -> PromotionSweepResult:
        """Run one sweep at ``now`` and audit its result (``memory.promotion.sweep``).

        ``max_items`` caps THIS run only (a manual backfill); ``None`` uses the
        configured ``max_items_per_sweep``. It is never stored. Waits for any sweep
        of this agent already in progress.
        """
        cap = validate_max_items(max_items) or self._cfg.max_items_per_sweep
        async with self._lock:
            result = await self._run(now, cap)
        self._emit("memory.promotion.sweep", "memory", result.status, asdict(result))
        return result

    async def _run(self, now: datetime, cap: int) -> PromotionSweepResult:
        gate = self._gate()
        if isinstance(gate, str):
            return PromotionSweepResult(status=gate)
        classifier, publisher = gate
        if not await self._classifier_ready(classifier):
            return PromotionSweepResult(status="classifier_unavailable")
        plan = await asyncio.to_thread(self._plan, classifier, cap)
        await asyncio.to_thread(self._record_pre_egress, plan, now)
        if plan.batch and not self._write_egress_audit(classifier, plan.batch):
            return _result("classifier_error", plan, _Classified())
        classified = await self._classify_batch(classifier, plan.batch, now)
        if classified.failure == "classifier_unavailable":
            self._write_egress_aborted(plan.batch, classified.evaluated)
        if classified.failure is not None:
            return _result(classified.failure, plan, classified)
        pending = plan.carried_pending + classified.pending
        promoted = await self._publish_pending(classifier, publisher, pending)
        return _result("completed", plan, classified, promoted=promoted)

    # -- phase 1: gate ---------------------------------------------------------

    def _gate(self) -> SweepStatus | tuple[PromotionClassifier, PromotionPublisher]:
        """The fail-closed gates, federal first so no config can bypass the tier lock."""
        if is_federal_tier(self._tier):
            return "tier_forbidden"
        if not self._cfg.enabled:
            return "disabled"
        if self._classifier is None:
            return "classifier_unavailable"
        if self._publisher is None:
            # No point disclosing bytes that cannot be published (LLM02).
            return "publisher_unavailable"
        return self._classifier, self._publisher

    async def _classifier_ready(self, classifier: PromotionClassifier) -> bool:
        """The bounded, network-free preflight; ``False`` means send nothing tonight."""
        try:
            await asyncio.wait_for(
                classifier.ensure_available(), timeout=self._cfg.request_timeout_seconds
            )
        except PromotionQuestionInvalidError as exc:
            # Fail closed and say which prompt broke — never fall back to stock.
            _log.warning("promotion question %s invalid; sweep sends nothing", exc.prompt)
            self._emit(
                "memory.promotion.question_invalid",
                exc.prompt,
                "classifier_unavailable",
                {"prompt": exc.prompt, "reason": str(exc)},
            )
            return False
        except (ClassifierUnavailableError, TimeoutError) as exc:
            _log.warning(
                "promotion classifier unavailable (%s); sweep sends nothing", type(exc).__name__
            )
            return False
        return True

    # -- phase 2: plan (worker thread) ---------------------------------------------

    def _plan(self, classifier: PromotionClassifier, cap: int) -> _Plan:
        """Sort every unjudged card into blocked / oversize / batch / deferred."""
        plan = _Plan()
        eligible: list[_Candidate] = []
        ledger = self._ledger.latest_index()
        for kind, item_id in self._listing():
            candidate = self._candidate(kind, item_id)
            if candidate is None:
                continue
            row = ledger.get((kind, item_id))
            if row is not None and _already_judged(row, candidate.text, classifier, self._cfg):
                if row.publish_state == "pending":
                    plan.carried_pending.append(row)
                continue
            self._route(plan, eligible, candidate)
        eligible.sort(key=lambda c: (c.modified, c.text.item_kind, c.text.item_id))
        plan.batch = [candidate.text for candidate in eligible[:cap]]
        plan.deferred = max(0, len(eligible) - cap)
        return plan

    def _route(self, plan: _Plan, eligible: list[_Candidate], candidate: _Candidate) -> None:
        """The pre-egress gates: a secret or oversize item is never sent."""
        text = candidate.text
        if contains_secret(text.content):
            plan.blocked_secret.append(text)
        elif _content_bytes(text) > self._cfg.max_item_bytes:
            plan.too_large.append(text)
        else:
            eligible.append(candidate)

    def _listing(self) -> list[tuple[PromotableKind, str]]:
        listing: list[tuple[PromotableKind, str]] = []
        listing += [("insight", item_id) for item_id in self._stores.insights.all_ids()]
        listing += [("procedure", slug) for slug in self._stores.procedures.slugs()]
        listing += [("entity", slug) for slug in self._stores.entities.slugs()]
        return listing

    def _source(self, kind: PromotableKind) -> _CardSource:
        if kind == "insight":
            return self._stores.insights
        if kind == "procedure":
            return self._stores.procedures
        return self._stores.entities

    def _render(self, kind: PromotableKind, item_id: str) -> PromotionText | None:
        item = self._source(kind).read(item_id)
        return render_candidate(item) if item is not None else None

    def _candidate(self, kind: PromotableKind, item_id: str) -> _Candidate | None:
        """Render one card; ``None`` when it vanished or sits above our clearance."""
        text = self._render(kind, item_id)
        if text is None or not self._cleared(text):
            return None
        try:
            modified = self._source(kind).path_for(item_id).stat().st_mtime
        except FileNotFoundError:
            return None
        return _Candidate(text=text, modified=modified)

    def _cleared(self, text: PromotionText) -> bool:
        """Only cards the shared side could accept at our clearance are ever sent.

        The exporter refuses a card whose label our clearance does not dominate,
        so sending its bytes to the classifier would be disclosure for nothing.
        """
        label = _parse_label(text.classification)
        return label is not None and dominates(self._clearance, label)

    # -- pre-egress + verdict recording (worker thread) ------------------------------

    def _record_pre_egress(self, plan: _Plan, now: datetime) -> None:
        for text in plan.blocked_secret:
            self._record(_pre_egress_row(text, "blocked_secret", now))
        for text in plan.too_large:
            self._record(_pre_egress_row(text, "too_large", now))

    def _record_verdict(
        self,
        classifier: PromotionClassifier,
        text: PromotionText,
        verdict: ClassifierVerdict,
        now: datetime,
    ) -> LedgerRow:
        decision = decide(verdict, self._cfg)
        row = LedgerRow(
            item_kind=text.item_kind,
            item_id=text.item_id,
            content_sha256=text.content_sha256,
            classifier_id=classifier.classifier_id,
            classifier_version=verdict.classifier_version,
            question_version=classifier.question_version,
            decision=decision,
            label=verdict.label,
            confidence=verdict.confidence,
            personal_probability=verdict.personal_probability,
            evaluated_at=now,
            publish_state="pending" if decision == "promote" else "none",
            shared_ref=None,
        )
        return self._record(row)

    def _record(self, row: LedgerRow) -> LedgerRow:
        """Append the signed ledger row, then its content-free decision audit."""
        signed = self._ledger.append(row)
        extra = {
            "item_kind": row.item_kind,
            "item_id": row.item_id,
            "content_sha256": row.content_sha256,
            "label": row.label,
            "confidence": row.confidence,
            "personal_probability": row.personal_probability,
            "classifier_version": row.classifier_version,
            "decision": row.decision,
        }
        self._emit("memory.promotion.decision", _reference(row), row.decision, extra)
        return signed

    # -- phase 3: egress audit -----------------------------------------------------

    def _write_egress_audit(
        self, classifier: PromotionClassifier, batch: list[PromotionText]
    ) -> bool:
        """Durably record what is about to leave the box; ``False`` means send nothing."""
        event = self._event(
            "memory.promotion.egress",
            "classifier",
            "allow",
            {
                "count": len(batch),
                "item_ids": [text.item_id for text in batch],
                "total_bytes": sum(_content_bytes(text) for text in batch),
                "classifier_id": classifier.classifier_id,
                "classifier_version": self._cfg.classifier_model,
                "host": classifier.host,
                "question_version": classifier.question_version,
            },
        )
        return self._write_durable(event)

    def _write_egress_aborted(self, batch: list[PromotionText], sent: int) -> None:
        """Durably record that egress stopped: which batch items were and were not sent."""
        event = self._event(
            "memory.promotion.egress_aborted",
            "classifier",
            "classifier_unavailable",
            {
                "sent_item_ids": [text.item_id for text in batch[:sent]],
                "unsent_item_ids": [text.item_id for text in batch[sent:]],
            },
        )
        self._write_durable(event)

    def _write_durable(self, event: AuditEvent) -> bool:
        """Write ``event`` to the durable audit; ``False`` when it could not be."""
        write_durable = getattr(self._audit, "write_durable", None)
        if write_durable is None:
            _log.warning("%s not recorded: audit sink cannot write durably", event.action)
            return False
        try:
            write_durable(event)
        except Exception:  # reason: any durable-write failure is reported to the caller
            _log.warning(
                "%s not recorded: durable audit write failed", event.action, exc_info=True
            )
            return False
        return True

    # -- phase 4: classify ---------------------------------------------------------

    async def _classify_batch(
        self, classifier: PromotionClassifier, batch: list[PromotionText], now: datetime
    ) -> _Classified:
        """Classify sequentially; record each verdict as it lands; stop at the first failure.

        ``evaluated`` counts items that reached the classifier: an unavailable
        classifier sent nothing, so that item is not counted.
        """
        tally = _Classified()
        for text in batch:
            verdict = await self._classify_one(classifier, text)
            if verdict != "classifier_unavailable":
                tally.evaluated += 1
            if isinstance(verdict, str):
                tally.failure = verdict
                return tally
            row = await asyncio.to_thread(self._record_verdict, classifier, text, verdict, now)
            if row.publish_state == "pending":
                tally.pending.append(row)
            else:
                tally.kept_private += 1
        return tally

    async def _classify_one(
        self, classifier: PromotionClassifier, text: PromotionText
    ) -> ClassifierVerdict | SweepStatus:
        """One bounded classify call; the failure status when it did not answer."""
        item = ClassifierInput(
            item_kind=text.item_kind, item_id=text.item_id, content=text.content
        )
        try:
            return await asyncio.wait_for(
                classifier.classify(item), timeout=self._cfg.request_timeout_seconds
            )
        except ClassifierUnavailableError:
            _log.warning("promotion classifier became unavailable; sweep stops")
            return "classifier_unavailable"
        except _CALL_ERRORS as exc:
            # Type only: a transport error message is not trusted to be content-free.
            _log.warning("promotion classifier failed (%s); sweep stops", type(exc).__name__)
            return "classifier_error"

    # -- phase 5: publish ----------------------------------------------------------

    async def _publish_pending(
        self,
        classifier: PromotionClassifier,
        publisher: PromotionPublisher,
        pending: list[LedgerRow],
    ) -> int:
        if not pending:
            return 0
        # One read after this night's appends; each item is published at most once
        # below, so its own later ``_mark`` rows never need to be re-read.
        ledger = await asyncio.to_thread(self._ledger.latest_index)
        published = 0
        for row in pending:
            latest = ledger.get((row.item_kind, row.item_id))
            if await asyncio.to_thread(self._still_publishable, classifier, row, latest):
                published += await self._publish_one(publisher, row)
        return published

    def _still_publishable(
        self, classifier: PromotionClassifier, row: LedgerRow, latest: LedgerRow | None
    ) -> bool:
        """Re-read the card: publish only the exact bytes the classifier judged."""
        text = self._render(row.item_kind, row.item_id)
        return (
            text is not None
            and latest is not None
            and latest.publish_state == "pending"
            and latest.content_sha256 == row.content_sha256
            and is_current(latest, text, classifier, self._cfg)
        )

    async def _publish_one(self, publisher: PromotionPublisher, row: LedgerRow) -> int:
        """Publish one row; ledger the outcome. Returns 1 when the item became shared."""
        if row.confidence is None or row.classifier_version is None:
            return 0  # unreachable: a promote row always carries its verdict
        try:
            shared_ref = await publisher.publish(
                _reference(row),
                content_sha256=row.content_sha256,
                confidence=row.confidence,
                classifier_version=row.classifier_version,
            )
        except PublisherUnavailableError:
            self._emit_publish(row, "refused")  # nothing written; row stays pending
            return 0
        except PublishOutcomeUnknownError:
            await self._mark(row, {"publish_state": "outcome_unknown"})
            self._emit_publish(row, "outcome_unknown")  # never retried
            return 0
        await self._mark(row, {"publish_state": "published", "shared_ref": shared_ref})
        self._emit_publish(row, "published")
        return 1

    async def _mark(self, row: LedgerRow, update: dict[str, Any]) -> None:
        await asyncio.to_thread(self._ledger.append, row.model_copy(update=update))

    # -- audit ---------------------------------------------------------------------

    def _emit_publish(self, row: LedgerRow, outcome: str) -> None:
        extra = {"item_kind": row.item_kind, "item_id": row.item_id}
        extra["content_sha256"] = row.content_sha256
        self._emit("memory.promotion.publish", _reference(row), outcome, extra)

    def _emit(self, action: str, target: str, outcome: str, extra: dict[str, Any]) -> None:
        emit(self._event(action, target, outcome, extra), self._audit)

    def _event(self, action: str, target: str, outcome: str, extra: dict[str, Any]) -> AuditEvent:
        return AuditEvent(
            actor_did=self._actor,
            action=action,
            target=target,
            outcome=outcome,
            tier=self._tier.strip().casefold(),
            extra=extra,
        )


# -- pure helpers ----------------------------------------------------------------


def _already_judged(
    row: LedgerRow, text: PromotionText, classifier: PromotionClassifier, cfg: PromotionConfig
) -> bool:
    """A verified row settles the item until its bytes (or classifier/question) change.

    Pre-egress rows carry no classifier fields. ``blocked_secret`` is reused on the
    content hash alone. ``too_large`` is reused only while the same bytes still
    exceed the current ``max_item_bytes``, so raising the limit re-checks them.
    """
    if row.decision == "blocked_secret":
        return row.content_sha256 == text.content_sha256
    if row.decision == "too_large":
        return (
            row.content_sha256 == text.content_sha256 and _content_bytes(text) > cfg.max_item_bytes
        )
    return is_current(row, text, classifier, cfg)


def _content_bytes(text: PromotionText) -> int:
    """The rendered card's size as it would cross the wire (UTF-8 bytes)."""
    return len(text.content.encode("utf-8"))


def _pre_egress_row(text: PromotionText, decision: LedgerDecision, now: datetime) -> LedgerRow:
    return LedgerRow(
        item_kind=text.item_kind,
        item_id=text.item_id,
        content_sha256=text.content_sha256,
        classifier_id=None,
        classifier_version=None,
        question_version=None,
        decision=decision,
        label=None,
        confidence=None,
        personal_probability=None,
        evaluated_at=now,
        publish_state="none",
        shared_ref=None,
    )


def _result(
    status: SweepStatus, plan: _Plan, classified: _Classified, *, promoted: int = 0
) -> PromotionSweepResult:
    return PromotionSweepResult(
        status=status,
        evaluated=classified.evaluated,
        promoted=promoted,
        kept_private=classified.kept_private,
        blocked_secret=len(plan.blocked_secret),
        too_large=len(plan.too_large),
        deferred=plan.deferred,
    )


def _parse_label(value: str) -> Classification | None:
    """The card's own label; an unknown label is never cleared (fail closed)."""
    try:
        return parse_classification(value, strict=True)
    except ValueError:
        return None


def _reference(row: LedgerRow) -> str:
    """The exporter's reference form: ``"<kind>:<id>"``."""
    return f"{row.item_kind}:{row.item_id}"


__all__ = [
    "MAX_ITEMS_CEILING",
    "PromotionStores",
    "PromotionSweep",
    "PromotionSweepResult",
    "SweepStatus",
    "validate_max_items",
]
