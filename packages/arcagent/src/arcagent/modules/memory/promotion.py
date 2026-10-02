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
* The label is bound the same way: the sweep read it from the card's stored
  classification, and the re-exported card must carry exactly that label. A
  label that moved (or a caller-supplied label that was never the card's) is
  refused, so a lower label cannot be smuggled in between judging and writing.
* Errors are mapped to the sweep's two ledger outcomes: a refusal before any write
  is :class:`PublisherUnavailableError` (row stays ``pending``); anything that
  may have written is :class:`PublishOutcomeUnknownError` (never auto-retried).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from arcmemory.promotion.publisher import (
    Demotion,
    PublisherUnavailableError,
    PublishOutcomeUnknownError,
)

from arcagent.knowledge import KnowledgeAccess, PromotionSource, SharedKnowledgePort

#: The only decision the shared side accepts for an automated promotion (COMP-024).
_CLASSIFIER_DECISION = "classifier_promote"
#: An operator's hand share (alpha-2 item 16): no classifier verdict, a named DID.
_OPERATOR_DECISION = "operator_promote"


class PromotionExporter(Protocol):
    """Re-reads one consolidated memory card as a promotion source."""

    async def export_for_promotion(
        self, reference: str, access: KnowledgeAccess
    ) -> PromotionSource: ...


class SharedKnowledgePublisher:
    """Publish one approved memory card through the shared-knowledge port.

    Approved by the classifier (:meth:`publish`) or by an operator
    (:meth:`publish_by_operator`); also reports the shared side's verified
    operator demotions (:meth:`demotions`) so the sweep can make them sticky.
    """

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
        classification: str,
    ) -> str:
        """Promote ``reference`` if its bytes still hash to ``content_sha256``.

        Returns the shared reference identifier.

        Raises:
            PublisherUnavailableError: refused before any write (card missing,
                changed, owned by another DID, or the shared side refused).
            PublishOutcomeUnknownError: the shared write may have landed, or it
                returned a reference that is not the bytes we sent.
        """
        return await self._promote(
            reference,
            content_sha256,
            classification,
            decision=_CLASSIFIER_DECISION,
            confidence=confidence,
            classifier_version=classifier_version,
        )

    async def publish_by_operator(
        self, reference: str, *, content_sha256: str, decided_by: str, classification: str
    ) -> str:
        """Promote ``reference`` on an operator's decision (alpha-2 item 16).

        The same digest re-check and the same two failures as :meth:`publish`;
        the shared side records ``decided_by`` durably before it writes.
        """
        return await self._promote(
            reference,
            content_sha256,
            classification,
            decision=_OPERATOR_DECISION,
            decided_by=decided_by,
        )

    async def demotions(self) -> dict[str, Demotion]:
        """The shared side's verified operator demotions, keyed by shared ref.

        Raises :class:`PublisherUnavailableError` when they cannot be read, so the
        sweep decides nothing rather than re-sending a demoted card.
        """
        try:
            found = await self._port.demotions(self._access_factory())
        except Exception as error:  # reason: unreadable -> the sweep must decide nothing
            raise PublisherUnavailableError(
                f"shared demotions unreadable ({type(error).__name__})"
            ) from error
        return {
            item.identifier: Demotion(item.identifier, item.demoted_by, item.reason)
            for item in found
        }

    async def _promote(
        self,
        reference: str,
        content_sha256: str,
        classification: str,
        *,
        decision: str,
        confidence: float | None = None,
        classifier_version: str | None = None,
        decided_by: str | None = None,
    ) -> str:
        access = self._access_factory()
        source = await self._verified_source(reference, content_sha256, classification, access)
        try:
            shared = await self._port.promote(
                source,
                access,
                decision=decision,
                confidence=confidence,
                classifier_version=classifier_version,
                decided_by=decided_by,
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
        self, reference: str, content_sha256: str, classification: str, access: KnowledgeAccess
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
        if source.classification.strip().upper() != classification.strip().upper():
            raise PublisherUnavailableError("memory card label differs from the label judged")
        return source


__all__ = ["PromotionExporter", "SharedKnowledgePublisher"]
