"""Storage-neutral shared-knowledge promotion with integrity and clearance gates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from arctrust import AuditEvent, AuditSink, emit
from arctrust.classification import dominates, parse_classification


@dataclass(frozen=True)
class SharedKnowledgeDraft:
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    document_type: str
    digest: str


class _Access(Protocol):
    caller_did: str
    clearance: str


class _Reference(Protocol):
    scope: str
    digest: str


class _Source(Protocol):
    reference: _Reference
    digest: str
    content: str
    classification: str
    title: str
    tags: tuple[str, ...]
    document_type: str


class SharedKnowledgeBackend(Protocol):
    """The application-owned shared store; no persistence is bundled here."""

    async def save(self, draft: SharedKnowledgeDraft, access: _Access) -> _Reference: ...


class SharedKnowledgeAdapter:
    """Authorize and validate promotions before delegating to shared storage."""

    def __init__(
        self,
        backend: SharedKnowledgeBackend,
        *,
        agent_did: str,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._backend = backend
        self._agent_did = agent_did
        self._audit_sink = audit_sink

    async def promote(self, source: _Source, access: _Access) -> _Reference:
        """Promote one personal export after identity, clearance, and digest checks."""
        caller_did = access.caller_did
        clearance = access.clearance
        if caller_did != self._agent_did:
            raise PermissionError("shared knowledge belongs to a different agent")
        if source.reference.scope != "personal":
            raise ValueError("only personal knowledge can be promoted")
        classification = source.classification
        if not dominates(
            parse_classification(clearance, strict=True),
            parse_classification(classification, strict=True),
        ):
            raise PermissionError("knowledge classification exceeds caller clearance")
        content = source.content
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        if digest != source.digest:
            raise ValueError("knowledge promotion digest mismatch")
        if not content.strip() or not source.title.strip():
            raise ValueError("knowledge promotion requires title and content")
        draft = SharedKnowledgeDraft(
            title=source.title,
            content=content,
            classification=classification,
            tags=source.tags,
            document_type=source.document_type,
            digest=digest,
        )
        result = await self._backend.save(draft, access)
        if result.scope != "shared" or result.digest != digest:
            raise ValueError("shared backend returned an invalid reference")
        _audit(self._audit_sink, caller_did, digest)
        return result


def _audit(sink: AuditSink | None, actor_did: str, digest: str) -> None:
    if sink is None:
        return
    emit(
        AuditEvent(
            actor_did=actor_did,
            action="knowledge.promoted",
            target="shared",
            outcome="promoted",
            payload_hash=digest.removeprefix("sha256:"),
        ),
        sink,
    )


__all__ = ["SharedKnowledgeAdapter", "SharedKnowledgeBackend", "SharedKnowledgeDraft"]
