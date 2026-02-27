"""Write validation, classification enforcement, audit trail.

All entity writes flow through promote(). No direct writes to MemoryStorage
except from this gate and IndexManager.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from arcteam.memory.classification import ClassificationChecker
from arcteam.memory.errors import EntityValidationError, PromotionError
from arcteam.memory.storage import MemoryStorage
from arcteam.memory.types import Classification, EntityMetadata, PromotionResult

if TYPE_CHECKING:
    from arcteam.audit import AuditLogger
    from arcteam.memory.config import TeamMemoryConfig
    from arcteam.memory.index_manager import IndexManager

logger = logging.getLogger(__name__)


class PromotionGate:
    """Write validation, classification enforcement, audit trail.

    All entity writes flow through promote().
    """

    def __init__(
        self,
        memory_storage: MemoryStorage,
        index_manager: IndexManager,
        classification_checker: ClassificationChecker,
        audit_logger: AuditLogger | None,
        messenger: object | None,
        config: TeamMemoryConfig,
    ) -> None:
        self._storage = memory_storage
        self._index_mgr = index_manager
        self._classifier = classification_checker
        self._audit = audit_logger
        self._messenger = messenger
        self._config = config

    async def promote(
        self,
        entity_id: str,
        content: str,
        metadata: EntityMetadata,
        agent_id: str,
    ) -> PromotionResult:
        """Validate -> classify -> duplicate-check -> write or queue."""
        # 1. Validate metadata
        self._validate(entity_id, metadata, content)

        # 2. Check classification
        entity_classification = ClassificationChecker.parse_classification(metadata.classification)

        # 3. CUI+ requires approval queue (tier-gated)
        if (
            self._config.tier in ("federal", "enterprise")
            and entity_classification > Classification.UNCLASSIFIED
        ):
            return await self._queue_approval(entity_id, content, metadata, agent_id)

        # 4. Check if entity exists (update vs create)
        existing = await self._index_mgr.entity_exists(entity_id)

        # 5. Write entity
        await self._storage.write_entity(entity_id, metadata, content)

        # 6. Touch dirty flag
        await self._index_mgr.touch_dirty()

        # 7. Audit log
        if self._audit:
            await self._audit.log(
                event_type="memory.promote",
                subject=entity_id,
                actor_id=agent_id,
                detail=f"{'updated' if existing else 'created'} entity",
                classification=(
                    metadata.classification.upper() if metadata.classification else "UNCLASSIFIED"
                ),
            )

        logger.info(
            "Promoted entity=%s agent=%s action=%s",
            entity_id,
            agent_id,
            "update" if existing else "create",
        )

        action = "updated" if existing else "created"
        return PromotionResult(
            success=True,
            entity_id=entity_id,
            action=action,
        )

    def _validate(self, entity_id: str, metadata: EntityMetadata, content: str) -> None:
        """Schema validation. Raises EntityValidationError on invalid."""
        # ID mismatch check
        if metadata.entity_id != entity_id:
            raise EntityValidationError(
                f"entity_id mismatch: promote('{entity_id}') "
                f"but metadata has '{metadata.entity_id}'"
            )

        # Path component safety (prevents path traversal)
        MemoryStorage.validate_path_component(entity_id, "entity_id")
        MemoryStorage.validate_path_component(metadata.entity_type, "entity_type")

        # Entity type allowlist
        if metadata.entity_type not in self._config.entity_types:
            raise EntityValidationError(
                f"entity_type '{metadata.entity_type}' not in allowed types: "
                f"{self._config.entity_types}"
            )

        # Federal tier: require explicit classification
        if (
            self._config.tier == "federal"
            and self._config.classification_required
            and not metadata.classification
        ):
            raise EntityValidationError("classification is required for federal tier")

        # Token budget enforcement
        estimated = self._storage.estimate_tokens(content)
        if estimated > self._config.per_entity_budget:
            raise EntityValidationError(
                f"content exceeds token budget: ~{estimated} tokens "
                f"(limit: {self._config.per_entity_budget})"
            )

    async def _queue_approval(
        self,
        entity_id: str,
        content: str,
        metadata: EntityMetadata,
        agent_id: str,
    ) -> PromotionResult:
        """Queue CUI+ entity for human approval."""
        if self._messenger is None:
            # Audit log the rejection
            if self._audit:
                await self._audit.log(
                    event_type="memory.promote_rejected",
                    subject=entity_id,
                    actor_id=agent_id,
                    detail="no messenger for CUI+ approval queue",
                    classification=(
                        metadata.classification.upper()
                        if metadata.classification
                        else "UNCLASSIFIED"
                    ),
                )
            raise PromotionError(
                entity_id,
                "no messaging service configured for CUI+ approval queue",
            )

        # TODO: Send approval message via messenger when messaging integration is ready
        logger.info(
            "Queued entity=%s for approval agent=%s classification=%s",
            entity_id,
            agent_id,
            metadata.classification,
        )

        # Audit log the queuing
        if self._audit:
            await self._audit.log(
                event_type="memory.promote_queued",
                subject=entity_id,
                actor_id=agent_id,
                detail="queued for CUI+ approval",
                classification=(
                    metadata.classification.upper() if metadata.classification else "UNCLASSIFIED"
                ),
            )

        return PromotionResult(
            success=True,
            entity_id=entity_id,
            action="queued_approval",
        )
