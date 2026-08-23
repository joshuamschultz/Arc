"""Fleet promotion and lifecycle orchestration over an optional ArcMemory collection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from arcteam.shared_knowledge.backend import FleetSharedKnowledgeBackend


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


class FleetSharedKnowledgeService:
    """Own fleet promotion policy while delegating collection mechanics to ArcMemory."""

    def __init__(self, backend: FleetSharedKnowledgeBackend) -> None:
        self._backend = backend

    @classmethod
    def for_arc_team(cls, base: Path | str | None = None) -> FleetSharedKnowledgeService:
        return cls(FleetSharedKnowledgeBackend.for_arc_team(base=base))

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
    ) -> object:
        """Promote one owned personal export through the fleet's authorization gate."""
        source: Any = await personal.export_for_promotion(reference, access)
        self._validate_promotion(source)
        collection = self._collection(access.caller_did, signer, audit_sink)
        result = await collection.save(
            _Draft(
                title=source.title,
                content=source.content.strip(),
                classification=source.classification,
                tags=source.tags,
                document_type=source.document_type,
            ),
            access,
        )
        if result.scope != "shared" or result.digest != source.digest:
            raise ValueError("fleet shared backend returned an invalid reference")
        return result

    async def read(self, reference: str, access: _Access) -> Any:
        return await self._backend.read(reference, access)

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


__all__ = ["FleetSharedKnowledgeService", "SharedKnowledgeUnavailableError"]
