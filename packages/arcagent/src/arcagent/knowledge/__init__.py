"""Typed, injected seams for personal and shared curated knowledge."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

KnowledgeScope = Literal["personal", "shared"]

#: Module-bus event published when the fleet binds a shared-knowledge port to a
#: started agent; ``data["port"]`` is the :class:`SharedKnowledgePort`.
SHARED_KNOWLEDGE_ATTACHED = "knowledge:shared_attached"
#: Module-bus event published when the fleet unbinds that port.
SHARED_KNOWLEDGE_DETACHED = "knowledge:shared_detached"


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
    #: True only from the consolidated-memory exporter: the label is the card's own
    #: stored label. The shared side honours a label below the writer's clearance
    #: only from such a source.
    label_from_card: bool = False


class KnowledgePort(Protocol):
    async def save(self, draft: KnowledgeDraft, access: KnowledgeAccess) -> KnowledgeRef: ...
    async def read(self, reference: str, access: KnowledgeAccess) -> KnowledgeDocument: ...
    async def search(self, query: str, access: KnowledgeAccess) -> list[KnowledgeHit]: ...


class PersonalKnowledgePort(KnowledgePort, Protocol):
    async def export_for_promotion(
        self, reference: str, access: KnowledgeAccess
    ) -> PromotionSource: ...


class KnowledgeDemotion(Protocol):
    """One verified operator demotion of a shared document (alpha-2 item 16)."""

    @property
    def identifier(self) -> str: ...

    @property
    def demoted_by(self) -> str: ...

    @property
    def reason(self) -> str: ...


class SharedKnowledgePort(KnowledgePort, Protocol):
    async def promote(
        self,
        source: PromotionSource,
        access: KnowledgeAccess,
        *,
        decision: str,
        confidence: float | None = None,
        classifier_version: str | None = None,
        decided_by: str | None = None,
    ) -> KnowledgeRef:
        """Promote ``source`` as ``access.caller_did`` under a recorded decision.

        ``decision="classifier_promote"`` carries ``confidence`` and
        ``classifier_version``; ``decision="operator_promote"`` carries the
        operator DID in ``decided_by`` and no classifier verdict.

        Raises ``PermissionError`` when the shared side refuses before writing
        (owner, clearance, signer, TOFU pin, type allowlist, demoted). Any other
        failure means the write may or may not have landed.
        """
        ...

    async def demotions(self, access: KnowledgeAccess) -> Sequence[KnowledgeDemotion]:
        """Every verified operator demotion of a shared document."""
        ...

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None: ...


__all__ = [
    "SHARED_KNOWLEDGE_ATTACHED",
    "SHARED_KNOWLEDGE_DETACHED",
    "KnowledgeAccess",
    "KnowledgeDemotion",
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
