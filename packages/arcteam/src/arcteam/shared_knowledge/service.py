"""Fleet promotion and lifecycle orchestration over an optional ArcMemory collection."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from arctrust.audit import AuditEvent, emit

from arcteam.shared_knowledge.backend import FleetSharedKnowledgeBackend

_CLASSIFIER_DECISION = "classifier_promote"
_MIN_CONFIDENCE = 0.90


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
    ) -> FleetSharedKnowledgeService:
        return cls(
            FleetSharedKnowledgeBackend.for_arc_team(base=base),
            promotable_document_types=promotable_document_types,
        )

    @classmethod
    def for_team_root(
        cls,
        team_root: Path | str,
        *,
        promotable_document_types: frozenset[str] | set[str] | None = None,
    ) -> FleetSharedKnowledgeService:
        """Bind the collection to a concrete operator team root (``<root>/shared/knowledge``).

        The always-on fleet writes and the dashboard reads through the SAME root,
        so a custom ``--team-root`` deployment keeps its shared knowledge beside the
        agents that produced it instead of the default home.
        """
        root = Path(team_root) / "shared" / "knowledge"
        return cls(
            FleetSharedKnowledgeBackend(root),
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
    ) -> object:
        """Promote one owned personal export through the fleet's authorization gate.

        A classifier-driven promotion passes ``decision="classifier_promote"`` with
        the classifier's ``confidence`` and ``classifier_version``; that decision is
        written durably BEFORE the shared write so an automated promotion can never
        land without its audit record, and only AFTER the write's owner, clearance,
        signer and pin checks pass, so a refused promotion records no decision.
        Entities merge into the shared canonical entity as this contributor's
        signed provenance block.

        Raises:
            SharedKnowledgePromotionRefusedError: any failure before the shared
                save — nothing was written.
            SharedKnowledgePromotionOutcomeUnknownError: the save failed or returned
                a reference to other bytes — the write may have landed.
        """
        classified = (
            decision is not None or confidence is not None or classifier_version is not None
        )
        decision_extra = {
            "item_id": reference,
            "confidence": confidence,
            "classifier_version": classifier_version,
            "decision": decision,
        }
        try:
            source, collection, draft = await self._authorize_promotion(
                personal, reference, access, signer, audit_sink, classified, decision_extra
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
        if classified:
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
        classified: bool,
        decision_extra: dict[str, Any],
    ) -> tuple[Any, Any, _Draft]:
        """Every pre-write step: decision shape, export, validation, authorize, decision audit.

        Returns the verified source, the collection and the draft to save.
        """
        if classified:
            _require_classifier_decision(
                decision_extra["decision"],
                decision_extra["confidence"],
                decision_extra["classifier_version"],
                audit_sink,
            )
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
        # Owner, clearance, signer-vs-DID and TOFU pin are checked BEFORE the
        # decision is recorded, so a refused promotion never leaves an "allow"
        # decision behind. The save re-checks everything (no trust across the gap).
        await collection.authorize_save(draft, access)
        if classified:
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

    async def read(self, reference: str, access: _Access) -> Any:
        return await self._backend.read(reference, access)

    async def list_documents(self, access: _Access) -> list[Any]:
        """Every promoted document the caller may read, attributed to its owner DID."""
        return await self._backend.list_documents(access)

    async def search(self, query: str, access: _Access) -> list[Any]:
        return await self._backend.search(query, access)

    async def revoke(self, reference: str, access: _Access) -> None:
        await self._backend.revoke(reference, access)

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


def _require_classifier_decision(
    decision: str | None,
    confidence: float | None,
    classifier_version: str | None,
    audit_sink: Any,
) -> None:
    """Refuse any automated promotion decision outside the classifier contract.

    ``confidence`` must be a real finite float (``bool``/``int``/``str`` refused)
    at or above the 0.90 promotion floor, and the decision must be recordable
    durably — an unauditable automated promotion fails closed.
    """
    if decision != _CLASSIFIER_DECISION:
        raise ValueError("unknown promotion decision")
    if (
        not isinstance(confidence, float)
        or not math.isfinite(confidence)
        or not _MIN_CONFIDENCE <= confidence <= 1.0
    ):
        raise ValueError("classifier confidence must be a finite float in [0.90, 1.0]")
    if not isinstance(classifier_version, str) or not classifier_version.strip():
        raise ValueError("classifier promotion requires a classifier version")
    if not callable(getattr(audit_sink, "write_durable", None)):
        raise ValueError("promotion decision requires a durable audit sink")


__all__ = [
    "FleetSharedKnowledgeService",
    "SharedKnowledgePromotionOutcomeUnknownError",
    "SharedKnowledgePromotionRefusedError",
    "SharedKnowledgeUnavailableError",
]
