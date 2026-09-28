"""``SharedKnowledgePublisher`` — arcagent's side of the promotion write (SPEC-083 COMP-023).

arcmemory decides which memory cards may leave the agent; it never writes to the
shared store. This module implements arcmemory's ``PromotionPublisher`` Protocol
(COMP-022) over the fleet's :class:`~arcagent.knowledge.SharedKnowledgePort`.

ARCAGENT-side by construction: it is the one layer that legally knows both the
arcmemory seam (the optional ``arcagent[memory]`` extra) and the
``arcagent.knowledge`` ports. It is only imported when promotion is composed, so
``NullBrain`` never pulls ``arcmemory`` in through here.

Trust rules:

* The origin DID is the runtime identity (``access_factory()``), never item content.
  The same access authorizes the export AND the shared promote.
* The bytes are re-exported and must still hash to the digest the classifier
  judged; a changed card is refused before the port is called.
* Errors are mapped to the sweep's two ledger outcomes: a refusal before any write
  is :class:`PublisherUnavailableError` (row stays ``pending``); anything that
  may have written is :class:`PublishOutcomeUnknownError` (never auto-retried).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from arcmemory.promotion.publisher import PublisherUnavailableError, PublishOutcomeUnknownError

from arcagent.knowledge import KnowledgeAccess, PromotionSource, SharedKnowledgePort

#: The only decision the shared side accepts for an automated promotion (COMP-024).
_CLASSIFIER_DECISION = "classifier_promote"


class PromotionExporter(Protocol):
    """Re-reads one consolidated memory card as a promotion source."""

    async def export_for_promotion(
        self, reference: str, access: KnowledgeAccess
    ) -> PromotionSource: ...


class SharedKnowledgePublisher:
    """Publish one classifier-approved memory card through the shared-knowledge port."""

    def __init__(
        self,
        *,
        port: SharedKnowledgePort,
        exporter: PromotionExporter,
        access_factory: Callable[[], KnowledgeAccess],
    ) -> None:
        self._port = port
        self._exporter = exporter
        self._access_factory = access_factory

    async def publish(
        self,
        reference: str,
        *,
        content_sha256: str,
        confidence: float,
        classifier_version: str,
    ) -> str:
        """Promote ``reference`` if its bytes still hash to ``content_sha256``.

        Returns the shared reference identifier.

        Raises:
            PublisherUnavailableError: refused before any write (card missing,
                changed, owned by another DID, or the shared side refused).
            PublishOutcomeUnknownError: the shared write may have landed, or it
                returned a reference that is not the bytes we sent.
        """
        access = self._access_factory()
        source = await self._verified_source(reference, content_sha256, access)
        try:
            shared = await self._port.promote(
                source,
                access,
                decision=_CLASSIFIER_DECISION,
                confidence=confidence,
                classifier_version=classifier_version,
            )
        except PermissionError as error:
            raise PublisherUnavailableError(f"shared side refused: {error}") from error
        except Exception as error:  # reason: effect unknown; the sweep must never retry it
            raise PublishOutcomeUnknownError(
                f"shared promote failed ({type(error).__name__})"
            ) from error
        if shared.scope != "shared" or shared.digest != content_sha256:
            raise PublishOutcomeUnknownError("shared side returned a reference to other bytes")
        return shared.identifier

    async def _verified_source(
        self, reference: str, content_sha256: str, access: KnowledgeAccess
    ) -> PromotionSource:
        try:
            source = await self._exporter.export_for_promotion(reference, access)
        except (PermissionError, LookupError, ValueError) as error:
            raise PublisherUnavailableError(f"memory card not exportable: {error}") from error
        if (
            source.reference.scope != "personal"
            or source.reference.identifier != reference
            or source.digest != content_sha256
        ):
            raise PublisherUnavailableError("memory card changed since it was classified")
        return source


__all__ = ["PromotionExporter", "SharedKnowledgePublisher"]
