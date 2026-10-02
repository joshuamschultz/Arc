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

The seam also reports the shared side's verified operator demotions (alpha-2
item 16), so the sweep can make a demote this agent's sticky ledger decision
without ever reading the fleet store itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class PublisherUnavailableError(Exception):
    """The publisher refused before writing anything; nothing was shared."""


class PublishOutcomeUnknownError(Exception):
    """The publish effect is uncertain; the sweep must never retry it."""


@dataclass(frozen=True)
class Demotion:
    """One verified operator demotion of a shared document.

    ``shared_ref`` is the shared identifier a publish returned; ``decided_by`` is
    the operator DID whose signature the shared side verified.
    """

    shared_ref: str
    decided_by: str
    reason: str


class PromotionPublisher(Protocol):
    """Publish one approved memory item to the shared store."""

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

    async def publish_by_operator(
        self, reference: str, *, content_sha256: str, decided_by: str
    ) -> str:
        """Publish ``reference`` on an operator's decision (no classifier verdict).

        Same digest rule and the same two failures as :meth:`publish`; the shared
        side records ``decided_by`` durably before it writes.
        """
        ...

    async def demotions(self) -> Mapping[str, Demotion]:
        """Every verified operator demotion, keyed by shared ref.

        Raises :class:`PublisherUnavailableError` when the shared side cannot be
        read; the caller then decides nothing (it cannot know what was demoted).
        """
        ...


__all__ = [
    "Demotion",
    "PromotionPublisher",
    "PublishOutcomeUnknownError",
    "PublisherUnavailableError",
]
