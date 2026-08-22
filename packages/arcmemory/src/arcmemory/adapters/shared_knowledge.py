"""Storage-neutral shared-knowledge promotion with integrity and clearance gates."""

from __future__ import annotations

import hashlib
from base64 import b64encode
from dataclasses import dataclass
from typing import Protocol

from arctrust import AuditEvent, AuditSink, canonical_json, emit
from arctrust.classification import dominates, parse_classification


@dataclass(frozen=True)
class SharedKnowledgeDraft:
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    document_type: str
    digest: str
    owner_did: str
    signature: str
    public_key: str
    algorithm: str


class _Access(Protocol):
    caller_did: str
    clearance: str


class _Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


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


class _Draft(Protocol):
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    document_type: str


class SharedKnowledgeBackend(Protocol):
    """The application-owned shared store; no persistence is bundled here."""

    async def save(self, draft: SharedKnowledgeDraft, access: _Access) -> _Reference: ...

    async def read(self, reference: str, access: _Access) -> object: ...

    async def search(self, query: str, access: _Access) -> list[object]: ...

    async def revoke(self, reference: str, access: _Access) -> None: ...


class SharedKnowledgeAdapter:
    """Authorize and validate promotions before delegating to shared storage."""

    def __init__(
        self,
        backend: SharedKnowledgeBackend,
        *,
        agent_did: str,
        signer: _Signer,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._backend = backend
        self._agent_did = agent_did
        self._signer = signer
        self._audit_sink = audit_sink

    async def save(self, draft: _Draft, access: _Access) -> _Reference:
        """Save an explicitly shared document under the caller's signed ownership."""
        self._authorize(access, draft.classification)
        return await self._save(
            title=draft.title,
            content=draft.content,
            classification=draft.classification,
            tags=draft.tags,
            document_type=draft.document_type,
            access=access,
            action="knowledge.saved_shared",
        )

    async def promote(self, source: _Source, access: _Access) -> _Reference:
        """Promote one personal export after identity, clearance, and digest checks."""
        self._authorize(access, source.classification)
        if source.reference.scope != "personal":
            raise ValueError("only personal knowledge can be promoted")
        content = source.content.strip()
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        if digest != source.digest:
            raise ValueError("knowledge promotion digest mismatch")
        if not content.strip() or not source.title.strip():
            raise ValueError("knowledge promotion requires title and content")
        result = await self._save(
            title=source.title,
            content=content,
            classification=source.classification,
            tags=source.tags,
            document_type=source.document_type,
            access=access,
            action="knowledge.promoted",
        )
        if result.scope != "shared" or result.digest != digest:
            raise ValueError("shared backend returned an invalid reference")
        return result

    async def read(self, reference: str, access: _Access) -> object:
        """Read through the backend, which owns path and clearance enforcement."""
        return await self._backend.read(reference, access)

    async def search(self, query: str, access: _Access) -> list[object]:
        """Search through the backend, which filters inaccessible documents."""
        return await self._backend.search(query, access)

    async def revoke(self, reference: str, access: _Access) -> None:
        """Revoke an owned shared document through the durable backend."""
        await self._backend.revoke(reference, access)

    def _authorize(self, access: _Access, classification: str) -> None:
        if access.caller_did != self._agent_did:
            raise PermissionError("shared knowledge belongs to a different agent")
        caller_clearance = parse_classification(access.clearance, strict=True)
        document_classification = parse_classification(classification, strict=True)
        if not dominates(caller_clearance, document_classification):
            raise PermissionError("knowledge classification exceeds caller clearance")
        if not dominates(document_classification, caller_clearance):
            raise PermissionError("no-write-down forbids lower-classification shared knowledge")

    async def _save(
        self,
        *,
        title: str,
        content: str,
        classification: str,
        tags: tuple[str, ...],
        document_type: str,
        access: _Access,
        action: str,
    ) -> _Reference:
        content = content.strip()
        if not content or not title.strip() or not document_type.strip():
            raise ValueError("shared knowledge requires title, content, and document type")
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        payload = {
            "title": title,
            "content": content,
            "classification": classification,
            "tags": list(tags),
            "document_type": document_type,
            "digest": digest,
            "owner_did": self._agent_did,
        }
        signed = canonical_json(payload)
        draft = SharedKnowledgeDraft(
            title=title,
            content=content,
            classification=classification,
            tags=tags,
            document_type=document_type,
            digest=digest,
            owner_did=self._agent_did,
            signature=self._signer.sign(signed).hex(),
            public_key=b64encode(self._signer.public_key).decode("ascii"),
            algorithm=self._signer.algorithm,
        )
        result = await self._backend.save(draft, access)
        _audit(self._audit_sink, access.caller_did, digest, action)
        return result


def _audit(sink: AuditSink | None, actor_did: str, digest: str, action: str) -> None:
    if sink is None:
        return
    emit(
        AuditEvent(
            actor_did=actor_did,
            action=action,
            target="shared",
            outcome="promoted",
            payload_hash=digest.removeprefix("sha256:"),
        ),
        sink,
    )


__all__ = ["SharedKnowledgeAdapter", "SharedKnowledgeBackend", "SharedKnowledgeDraft"]
