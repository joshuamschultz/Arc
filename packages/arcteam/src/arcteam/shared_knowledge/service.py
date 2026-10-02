"""Fleet promotion and lifecycle orchestration over an optional ArcMemory collection."""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from arctrust.audit import AuditEvent, emit
from arctrust.identity import did_from_public_key

from arcteam.shared_knowledge.backend import (
    FleetSharedKnowledgeBackend,
    SharedKnowledgeDemotion,
    SharedKnowledgeProvenance,
    SharedKnowledgeReference,
    SharedKnowledgeSummary,
)

_logger = logging.getLogger(__name__)

_CLASSIFIER_DECISION = "classifier_promote"
#: An operator shared the card by hand (alpha-2 item 16): no classifier verdict,
#: a named operator DID, and the same durable decision record.
_OPERATOR_DECISION = "operator_promote"
#: Provenance decision for a promotion made with no recorded decision (the
#: agent's own ``shared_knowledge_promote`` tool path).
_DIRECT_DECISION = "direct"
_MIN_CONFIDENCE = 0.90
_MAX_DID_CHARS = 256


class _Access(Protocol):
    @property
    def caller_did(self) -> str: ...

    @property
    def clearance(self) -> str: ...


class _PersonalKnowledge(Protocol):
    async def export_for_promotion(self, reference: str, access: _Access) -> object: ...


class _Source(Protocol):
    @property
    def reference(self) -> object: ...

    @property
    def digest(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def classification(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def tags(self) -> tuple[str, ...]: ...

    @property
    def document_type(self) -> str: ...


class _Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class _Draft:
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    document_type: str


@dataclass(frozen=True)
class _OperatorAccess:
    """The operator's read context for a demote: the anchored DID at a clearance."""

    caller_did: str
    clearance: str


class SharedKnowledgeUnavailableError(RuntimeError):
    """The optional ArcMemory collection mechanics are not installed."""


class SharedKnowledgePromotionRefusedError(PermissionError, ValueError):
    """A promotion was refused before any shared write; nothing was shared.

    A ``PermissionError`` because that is the shared-knowledge seam's contract for
    "refused, nothing written" (``arcagent.knowledge.SharedKnowledgePort.promote``),
    so a caller that must not import arcteam still tells it apart. Also a
    ``ValueError`` so callers that validated the decision shape keep catching it.
    """


class SharedKnowledgePromotionOutcomeUnknownError(RuntimeError):
    """The shared write was attempted and may or may not have landed.

    Never retry automatically: a retry could share the same item twice.
    """


class FleetSharedKnowledgeService:
    """Own fleet promotion policy while delegating collection mechanics to ArcMemory."""

    def __init__(
        self,
        backend: FleetSharedKnowledgeBackend,
        *,
        promotable_document_types: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._backend = backend
        # None → no operator restriction (personal zero-config); a set → only those
        # document types may be promoted, so the operator controls WHAT is shared
        # rather than every personal note becoming fleet-wide by default.
        self._promotable_document_types = (
            frozenset(promotable_document_types) if promotable_document_types is not None else None
        )

    @classmethod
    def for_arc_team(
        cls,
        base: Path | str | None = None,
        *,
        promotable_document_types: frozenset[str] | set[str] | None = None,
        operator_public_key: bytes | None = None,
    ) -> FleetSharedKnowledgeService:
        return cls(
            FleetSharedKnowledgeBackend.for_arc_team(
                base=base, operator_public_key=operator_public_key
            ),
            promotable_document_types=promotable_document_types,
        )

    @classmethod
    def for_team_root(
        cls,
        team_root: Path | str,
        *,
        promotable_document_types: frozenset[str] | set[str] | None = None,
        operator_public_key: bytes | None = None,
    ) -> FleetSharedKnowledgeService:
        """Bind the collection to a concrete operator team root (``<root>/shared/knowledge``).

        The always-on fleet writes and the dashboard reads through the SAME root,
        so a custom ``--team-root`` deployment keeps its shared knowledge beside the
        agents that produced it instead of the default home. ``operator_public_key``
        is the deployment operator's trust anchor for demotions; without it no
        demote is accepted and no tombstone counts as one.
        """
        root = Path(team_root) / "shared" / "knowledge"
        return cls(
            FleetSharedKnowledgeBackend(root, operator_public_key=operator_public_key),
            promotable_document_types=promotable_document_types,
        )

    @property
    def backend(self) -> FleetSharedKnowledgeBackend:
        return self._backend

    async def promote(
        self,
        personal: Any,
        reference: str,
        access: _Access,
        signer: _Signer,
        *,
        audit_sink: Any = None,
        decision: str | None = None,
        confidence: float | None = None,
        classifier_version: str | None = None,
        decided_by: str | None = None,
    ) -> object:
        """Promote one owned personal export through the fleet's authorization gate.

        Two recorded decisions exist. ``decision="classifier_promote"`` carries the
        classifier's ``confidence`` and ``classifier_version``;
        ``decision="operator_promote"`` carries the operator's DID in ``decided_by``
        and no classifier verdict at all (alpha-2 item 16). Either is written
        durably BEFORE the shared write, so a decided promotion can never land
        without its audit record, and only AFTER the write's owner, clearance,
        signer and pin checks pass, so a refused promotion records no decision.
        Entities merge into the shared canonical entity as this contributor's
        signed provenance block. After the write, a contributor-signed provenance
        record names the source card, the decision and the time.

        Raises:
            SharedKnowledgePromotionRefusedError: any failure before the shared
                save — nothing was written.
            SharedKnowledgePromotionOutcomeUnknownError: the save failed or returned
                a reference to other bytes — the write may have landed.
        """
        decided = any(
            value is not None for value in (decision, confidence, classifier_version, decided_by)
        )
        decision_extra = {
            "item_id": reference,
            "confidence": confidence,
            "classifier_version": classifier_version,
            "decision": decision,
            "decided_by": decided_by,
        }
        try:
            source, collection, draft = await self._authorize_promotion(
                personal, reference, access, signer, audit_sink, decided, decision_extra
            )
        except Exception as error:  # reason: nothing is written before the save — a refusal
            raise SharedKnowledgePromotionRefusedError(str(error)) from error
        try:
            result = await collection.save(draft, access)
        except Exception as error:  # reason: the write may have landed; never report a refusal
            raise SharedKnowledgePromotionOutcomeUnknownError(
                f"shared write outcome unknown ({type(error).__name__})"
            ) from error
        if result.scope != "shared" or result.digest != source.digest:
            raise SharedKnowledgePromotionOutcomeUnknownError(
                "fleet shared backend returned an invalid reference"
            )
        await self._record_provenance(result, signer, access, source, reference, decision_extra)
        if decided:
            emit(
                AuditEvent(
                    actor_did=access.caller_did,
                    action="knowledge.promotion_completed",
                    target=result.identifier,
                    outcome="allow",
                    classification=source.classification,
                    extra=decision_extra,
                ),
                audit_sink,
            )
        return result

    async def _authorize_promotion(
        self,
        personal: Any,
        reference: str,
        access: _Access,
        signer: _Signer,
        audit_sink: Any,
        decided: bool,
        decision_extra: dict[str, Any],
    ) -> tuple[Any, Any, _Draft]:
        """Every pre-write step: decision shape, export, validation, authorize, decision audit.

        Returns the verified source, the collection and the draft to save.
        """
        if decided:
            _require_decision(decision_extra, audit_sink)
        source: Any = await personal.export_for_promotion(reference, access)
        self._validate_promotion(source)
        self._enforce_promotable_type(source)
        collection = self._collection(access.caller_did, signer, audit_sink)
        draft = _Draft(
            title=source.title,
            content=source.content.strip(),
            classification=source.classification,
            tags=source.tags,
            document_type=source.document_type,
        )
        # Owner, clearance, signer-vs-DID, TOFU pin and "not demoted" are checked
        # BEFORE the decision is recorded, so a refused promotion never leaves an
        # "allow" decision behind. The save re-checks everything (no trust across
        # the gap).
        await collection.authorize_save(draft, access)
        if decided:
            audit_sink.write_durable(
                AuditEvent(
                    actor_did=access.caller_did,
                    action="knowledge.promotion_decision",
                    target=reference,
                    outcome="allow",
                    classification=source.classification,
                    extra=decision_extra,
                )
            )
        return source, collection, draft

    async def _record_provenance(
        self,
        result: SharedKnowledgeReference,
        signer: _Signer,
        access: _Access,
        source: Any,
        reference: str,
        decision_extra: dict[str, Any],
    ) -> None:
        """Best-effort signed provenance; the write already landed and was audited.

        Provenance is display metadata — the durable decision audit is the record
        of authority — so a failure here is logged and never reported as an
        unknown outcome (which would stop the item from ever being retried).
        """
        fields = {
            "contributor_did": access.caller_did,
            "source_ref": reference,
            "kind": source.document_type,
            "decision": decision_extra["decision"] or _DIRECT_DECISION,
            "confidence": decision_extra["confidence"],
            "classifier_version": decision_extra["classifier_version"],
            "decided_by": decision_extra["decided_by"],
            "promoted_at": datetime.now(UTC).isoformat(),
        }
        try:
            await self._backend.record_provenance(result, signer, fields)
        except Exception as error:  # reason: metadata only; never undo or mislabel the write
            _logger.warning("shared knowledge provenance not recorded (%s)", type(error).__name__)

    async def read(self, reference: str, access: _Access) -> Any:
        return await self._backend.read(reference, access)

    async def list_documents(
        self, access: _Access, *, include_demoted: bool = False
    ) -> list[SharedKnowledgeSummary]:
        """Every promoted document the caller may read, with kind, contributors and time.

        ``include_demoted`` adds the operator-demoted documents, each carrying its
        demotion (the dashboard's "show demoted" view).
        """
        return await self._backend.list_documents(access, include_demoted=include_demoted)

    async def search(self, query: str, access: _Access) -> list[Any]:
        return await self._backend.search(query, access)

    async def revoke(self, reference: str, access: _Access) -> None:
        await self._backend.revoke(reference, access)

    async def provenance(
        self, reference: str, access: _Access
    ) -> tuple[SharedKnowledgeProvenance, ...]:
        """The verified provenance of a live document's bytes (who, when, what decision)."""
        return await self._backend.provenance(reference, access)

    async def demote(
        self,
        reference: str,
        *,
        operator_signer: _Signer,
        reason: str,
        clearance: str,
    ) -> SharedKnowledgeDemotion:
        """Demote one shared document under the operator's signed tombstone.

        Only the anchored deployment operator key may demote, and only a document
        it can read at ``clearance``. The bytes are retired, never erased; every
        contributing agent's next sweep turns the verified tombstone into its own
        sticky ``demoted_by_operator`` decision, so the card never re-promotes.

        Raises ``PermissionError`` (not the operator, or above clearance),
        ``FileNotFoundError`` (unknown or already revoked) and ``ValueError``
        (bad identifier or reason).
        """
        access = _OperatorAccess(_operator_did(operator_signer), clearance)
        return await self._backend.demote(reference, access, operator_signer, reason)

    async def demotions(self) -> dict[str, SharedKnowledgeDemotion]:
        """Every verified operator demotion, keyed by shared identifier."""
        return await self._backend.demotions()

    def _collection(self, owner_did: str, signer: _Signer, audit_sink: Any) -> Any:
        try:
            from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter
        except ImportError as error:
            raise SharedKnowledgeUnavailableError(
                "ArcMemory shared collection mechanics are unavailable"
            ) from error
        backend: Any = self._backend
        return SharedKnowledgeAdapter(
            backend,
            owner_did=owner_did,
            signer=signer,
            audit_sink=audit_sink,
        )

    def _enforce_promotable_type(self, source: _Source) -> None:
        """Fail closed when the operator's allowlist does not cover this type."""
        allowed = self._promotable_document_types
        if allowed is not None and source.document_type not in allowed:
            raise PermissionError(
                f"document type {source.document_type!r} is not promotable to shared knowledge"
            )

    @staticmethod
    def _validate_promotion(source: _Source) -> None:
        if getattr(source.reference, "scope", "") != "personal":
            raise ValueError("only personal knowledge can be promoted")
        content = source.content.strip()
        if not source.title.strip() or not content:
            raise ValueError("knowledge promotion requires title and content")
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        if digest != source.digest:
            raise ValueError("knowledge promotion digest mismatch")


def _operator_did(signer: _Signer) -> str:
    return did_from_public_key(signer.public_key, org="operator", agent_type="approver")


def _require_decision(extra: dict[str, Any], audit_sink: Any) -> None:
    """Refuse any recorded promotion decision outside its contract.

    ``classifier_promote``: ``confidence`` a real finite float (``bool``/``int``/
    ``str`` refused) in [0.90, 1.0], a non-empty ``classifier_version``, and no
    operator named. ``operator_promote``: a DID in ``decided_by`` and no
    classifier verdict — an operator cannot smuggle a fake confidence. Either
    must be recordable durably: an unauditable decided promotion fails closed.
    """
    decision = extra["decision"]
    if decision == _CLASSIFIER_DECISION:
        _require_classifier_verdict(extra)
    elif decision == _OPERATOR_DECISION:
        _require_operator_decider(extra)
    else:
        raise ValueError("unknown promotion decision")
    if not callable(getattr(audit_sink, "write_durable", None)):
        raise ValueError("promotion decision requires a durable audit sink")


def _require_classifier_verdict(extra: dict[str, Any]) -> None:
    confidence, version = extra["confidence"], extra["classifier_version"]
    if (
        not isinstance(confidence, float)
        or not math.isfinite(confidence)
        or not _MIN_CONFIDENCE <= confidence <= 1.0
    ):
        raise ValueError("classifier confidence must be a finite float in [0.90, 1.0]")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("classifier promotion requires a classifier version")
    if extra["decided_by"] is not None:
        raise ValueError("a classifier promotion names no operator")


def _require_operator_decider(extra: dict[str, Any]) -> None:
    decider = extra["decided_by"]
    if extra["confidence"] is not None or extra["classifier_version"] is not None:
        raise ValueError("an operator promotion carries no classifier verdict")
    if (
        not isinstance(decider, str)
        or not decider.startswith("did:")
        or len(decider) > _MAX_DID_CHARS
    ):
        raise ValueError("an operator promotion requires the operator's DID")


__all__ = [
    "FleetSharedKnowledgeService",
    "SharedKnowledgePromotionOutcomeUnknownError",
    "SharedKnowledgePromotionRefusedError",
    "SharedKnowledgeUnavailableError",
]
