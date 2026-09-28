"""The ``PromotionPublisher`` seam — arcmemory's side of the shared write (SPEC-083 COMP-022).

arcmemory decides; it never writes to the shared store itself. The integrator
(arcagent's ``SharedKnowledgePublisher``) implements this Protocol by calling the
shared promote, which pulls the bytes from the consolidated-memory exporter and
re-verifies ``content_sha256`` on its own side.

Two typed failures, with different ledger outcomes in the sweep:

* :class:`PublisherUnavailableError` — refused BEFORE any write (store absent,
  digest mismatch, policy deny). The row stays ``pending``; a changed item is
  re-evaluated next night.
* :class:`PublishOutcomeUnknownError` — the write may or may not have landed
  (connection dropped after send). The row is marked ``outcome_unknown`` and is
  never retried automatically, so an item is never shared twice.
"""

from __future__ import annotations

from typing import Protocol


class PublisherUnavailableError(Exception):
    """The publisher refused before writing anything; nothing was shared."""


class PublishOutcomeUnknownError(Exception):
    """The publish effect is uncertain; the sweep must never retry it."""


class PromotionPublisher(Protocol):
    """Publish one classifier-approved memory item to the shared store."""

    async def publish(
        self,
        reference: str,
        *,
        content_sha256: str,
        confidence: float,
        classifier_version: str,
    ) -> str:
        """Publish ``reference`` (``"<kind>:<id>"``) and return the shared ref.

        ``content_sha256`` is the digest the classifier judged; the shared side
        must refuse (:class:`PublisherUnavailableError`) when the bytes it pulls
        no longer hash to it.
        """
        ...


__all__ = ["PromotionPublisher", "PublishOutcomeUnknownError", "PublisherUnavailableError"]
