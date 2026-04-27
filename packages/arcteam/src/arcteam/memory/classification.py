"""Classification access control enforcement.

Checks agent clearance against entity classification on every read/search.
Classification check runs at ALL tiers (ADR-019 four-pillars-universal).
Tier sets stringency of ancillary behavior (audit verbosity), not whether
to enforce — authorization is a pillar.

Default classification is UNCLASSIFIED (most permissive), so personal-tier
developers who do not explicitly classify entities see no change in behavior.
An operator who explicitly classifies entities at personal tier gets enforcement.

NIST 800-53 AC-3.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, TypeVar

from arcteam.memory.types import Classification, IndexEntry, SearchResult

if TYPE_CHECKING:
    from arcteam.audit import AuditLogger
    from arcteam.memory.config import TeamMemoryConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", SearchResult, IndexEntry)


class ClassificationChecker:
    """Classification access control enforcement.

    Universal: classification check runs at all tiers (ADR-019).
    Tier affects ancillary behavior only (e.g. audit verbosity).
    """

    def __init__(
        self,
        config: TeamMemoryConfig,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._config = config
        self._audit = audit_logger

    def check_access(
        self,
        entity_classification: str,
        agent_classification: Classification,
        entity_id: str = "",
        agent_id: str = "",
    ) -> bool:
        """Check if agent has clearance. Returns True if access allowed.

        Runs at all tiers (ADR-019). Default classification (UNCLASSIFIED) makes
        personal-tier single-developer workflows permissive by default.
        """
        entity_level = self.parse_classification(entity_classification)

        # Agent clearance must be >= entity classification
        if agent_classification >= entity_level:
            return True

        # Access denied — log and audit (NIST 800-53 AU-2)
        logger.warning(
            "Classification denied: agent=%s (level=%s) entity=%s (level=%s)",
            agent_id,
            agent_classification.name,
            entity_id,
            entity_level.name,
        )
        if self._audit:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(  # noqa: RUF006
                    self._audit.log(
                        event_type="memory.classification_denied",
                        subject=entity_id,
                        actor_id=agent_id,
                        detail=(
                            f"agent clearance {agent_classification.name} "
                            f"< entity level {entity_level.name}"
                        ),
                        classification=entity_level.name,
                    )
                )
            except RuntimeError:
                # No running event loop — skip async audit
                pass
        return False

    def filter_results(
        self,
        results: list[T],
        agent_classification: Classification,
        agent_id: str = "",
    ) -> list[T]:
        """Silently filter results above agent clearance.

        Runs at all tiers (ADR-019). Default classification (UNCLASSIFIED) makes
        personal-tier single-developer workflows permissive by default.
        """
        filtered = []
        for item in results:
            if self.check_access(
                item.classification,
                agent_classification,
                entity_id=item.entity_id,
                agent_id=agent_id,
            ):
                filtered.append(item)

        return filtered

    @staticmethod
    def parse_classification(value: str) -> Classification:
        """Parse classification string to enum. Defaults to UNCLASSIFIED.

        Warns on unrecognized values to catch typos like 'SECERT'.
        """
        if not value:
            return Classification.UNCLASSIFIED
        normalized = value.upper().strip()
        try:
            return Classification[normalized]
        except KeyError:
            logger.warning(
                "Unknown classification value %r, defaulting to UNCLASSIFIED",
                value,
            )
            return Classification.UNCLASSIFIED
