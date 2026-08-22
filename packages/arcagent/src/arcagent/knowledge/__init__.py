"""Typed, injected seams for personal and shared curated knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

KnowledgeScope = Literal["personal", "shared"]


@dataclass(frozen=True)
class KnowledgeAccess:
    """Authoritative runtime access context, never model-supplied input."""

    caller_did: str
    clearance: str


@dataclass(frozen=True)
class KnowledgeDraft:
    title: str
    content: str
    classification: str
    tags: tuple[str, ...] = ()
    document_type: str = "note"


@dataclass(frozen=True)
class KnowledgeRef:
    scope: KnowledgeScope
    identifier: str
    digest: str


@dataclass(frozen=True)
class KnowledgeDocument:
    reference: KnowledgeRef
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeHit:
    reference: KnowledgeRef
    title: str
    excerpt: str


@dataclass(frozen=True)
class PromotionSource:
    reference: KnowledgeRef
    digest: str
    content: str
    classification: str
    title: str = ""
    tags: tuple[str, ...] = ()
    document_type: str = "note"


class KnowledgePort(Protocol):
    async def save(self, draft: KnowledgeDraft, access: KnowledgeAccess) -> KnowledgeRef: ...
    async def read(self, reference: str, access: KnowledgeAccess) -> KnowledgeDocument: ...
    async def search(self, query: str, access: KnowledgeAccess) -> list[KnowledgeHit]: ...


class PersonalKnowledgePort(KnowledgePort, Protocol):
    async def export_for_promotion(
        self, reference: str, access: KnowledgeAccess
    ) -> PromotionSource: ...


class SharedKnowledgePort(KnowledgePort, Protocol):
    async def promote(
        self, source: PromotionSource, access: KnowledgeAccess
    ) -> KnowledgeRef: ...


__all__ = [
    "KnowledgeAccess",
    "KnowledgeDocument",
    "KnowledgeDraft",
    "KnowledgeHit",
    "KnowledgePort",
    "KnowledgeRef",
    "KnowledgeScope",
    "PersonalKnowledgePort",
    "PromotionSource",
    "SharedKnowledgePort",
]
