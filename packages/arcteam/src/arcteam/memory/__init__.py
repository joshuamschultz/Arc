"""ArcTeam Memory: Shared team knowledge graph with wiki-linked entities."""

from __future__ import annotations

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.errors import (
    ClassificationError,
    EntityNotFoundError,
    EntityValidationError,
    IndexCorruptionError,
    LockTimeoutError,
    PromotionError,
    TeamMemoryError,
)
from arcteam.memory.service import TeamMemoryService
from arcteam.memory.types import (
    Classification,
    EntityFile,
    EntityMetadata,
    IndexEntry,
    MemoryStatus,
    PromotionResult,
    SearchResult,
)

__all__ = [
    "Classification",
    "ClassificationError",
    "EntityFile",
    "EntityMetadata",
    "EntityNotFoundError",
    "EntityValidationError",
    "IndexCorruptionError",
    "IndexEntry",
    "LockTimeoutError",
    "MemoryStatus",
    "PromotionError",
    "PromotionResult",
    "SearchResult",
    "TeamMemoryConfig",
    "TeamMemoryError",
    "TeamMemoryService",
]
