"""Team-agnostic signed knowledge-collection mechanics."""

from __future__ import annotations

import hashlib
from base64 import b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from arctrust import AuditEvent, AuditSink, canonical_json, emit


@dataclass(frozen=True)
class SharedKnowledgeDraft:
    """A provenance-bearing document ready for an application-owned collection."""

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
    @property
    def caller_did(self) -> str: ...


class _Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class _Draft(Protocol):
    @property
    def title(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def classification(self) -> str: ...

    @property
    def tags(self) -> tuple[str, ...]: ...

    @property
    def document_type(self) -> str: ...


class _Reference(Protocol):
    @property
    def scope(self) -> str: ...

    @property
    def digest(self) -> str: ...


class SharedKnowledgeBackend(Protocol):
    """Storage/index implementation selected by the consuming application."""

    async def save(self, draft: SharedKnowledgeDraft, access: _Access) -> Any: ...

    async def read(self, reference: str, access: _Access) -> Any: ...

    async def search(self, query: str, access: _Access) -> list[Any]: ...

    async def revoke(self, reference: str, access: _Access) -> None: ...


class SharedKnowledgeAdapter:
    """Sign, provenance-stamp, and delegate generic collection operations.

    Membership, promotion approval, and classification policy belong to the caller's
    application. This adapter intentionally has no concept of a team or roster.
    """

    def __init__(
        self,
        backend: SharedKnowledgeBackend,
        *,
        owner_did: str,
        signer: _Signer,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._backend = backend
        self._owner_did = owner_did
        self._signer = signer
        self._audit_sink = audit_sink

    async def save(self, draft: _Draft, access: _Access) -> _Reference:
        """Create a signed, content-addressed document and persist it through the seam."""
        prepared = self._prepare(draft)
        try:
            result = await self._backend.save(prepared, access)
        except (PermissionError, ValueError) as error:
            self._emit(access, "knowledge.collection_saved", draft.title, "deny", error=error)
            raise
        self._emit(
            access,
            "knowledge.collection_saved",
            result.scope,
            "allow",
            payload_hash=prepared.digest.removeprefix("sha256:"),
        )
        return cast(_Reference, result)

    async def read(self, reference: str, access: _Access) -> Any:
        """Delegate retrieval; the application backend authorizes the operation."""
        return await self._delegated(
            "knowledge.collection_retrieved", reference, self._backend.read, access
        )

    async def search(self, query: str, access: _Access) -> list[Any]:
        """Delegate search without writing a plaintext query to audit."""
        return cast(
            list[Any],
            await self._delegated(
                "knowledge.collection_searched",
                "collection",
                self._backend.search,
                access,
                argument=query,
                payload_hash=_hash_text(query),
            ),
        )

    async def revoke(self, reference: str, access: _Access) -> None:
        """Delegate revocation; lifecycle policy remains application-owned."""
        await self._delegated(
            "knowledge.collection_revoked", reference, self._backend.revoke, access
        )

    def _prepare(self, draft: _Draft) -> SharedKnowledgeDraft:
        title = draft.title.strip()
        content = draft.content.strip()
        document_type = draft.document_type.strip()
        if not title or not content or not document_type:
            raise ValueError("shared knowledge requires title, content, and document type")
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        payload = {
            "title": title,
            "content": content,
            "classification": draft.classification,
            "tags": list(draft.tags),
            "document_type": document_type,
            "digest": digest,
            "owner_did": self._owner_did,
        }
        return SharedKnowledgeDraft(
            title=title,
            content=content,
            classification=draft.classification,
            tags=draft.tags,
            document_type=document_type,
            digest=digest,
            owner_did=self._owner_did,
            signature=self._signer.sign(canonical_json(payload)).hex(),
            public_key=b64encode(self._signer.public_key).decode("ascii"),
            algorithm=self._signer.algorithm,
        )

    async def _delegated(
        self,
        action: str,
        target: str,
        operation: Callable[[str, _Access], Awaitable[Any]],
        access: _Access,
        *,
        argument: str | None = None,
        payload_hash: str | None = None,
    ) -> Any:
        try:
            if argument is None:
                result = await operation(target, access)
            else:
                result = await operation(argument, access)
        except (FileNotFoundError, PermissionError, ValueError) as error:
            self._emit(access, action, target, "deny", payload_hash=payload_hash, error=error)
            raise
        self._emit(access, action, target, "allow", payload_hash=payload_hash)
        return result

    def _emit(
        self,
        access: _Access,
        action: str,
        target: str,
        outcome: str,
        *,
        payload_hash: str | None = None,
        error: Exception | None = None,
    ) -> None:
        if self._audit_sink is None:
            return
        extra = {} if error is None else {"error": type(error).__name__}
        emit(
            AuditEvent(
                actor_did=access.caller_did,
                action=action,
                target=target,
                outcome=outcome,
                payload_hash=payload_hash,
                extra=extra,
            ),
            self._audit_sink,
        )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = ["SharedKnowledgeAdapter", "SharedKnowledgeBackend", "SharedKnowledgeDraft"]
