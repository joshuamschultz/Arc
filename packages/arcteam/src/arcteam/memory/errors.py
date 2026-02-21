"""Team memory error hierarchy."""

from __future__ import annotations


class TeamMemoryError(Exception):
    """Base exception for team memory operations."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


class EntityNotFoundError(TeamMemoryError):
    """Entity not found in index or filesystem."""

    def __init__(self, entity_id: str) -> None:
        super().__init__("ENTITY_NOT_FOUND", f"Entity not found: {entity_id}")


class EntityValidationError(TeamMemoryError):
    """Entity metadata failed Pydantic validation."""

    def __init__(self, detail: str) -> None:
        super().__init__("ENTITY_VALIDATION", detail)


class ClassificationError(TeamMemoryError):
    """Classification access denied."""

    def __init__(self, entity_id: str, reason: str = "access denied") -> None:
        super().__init__("CLASSIFICATION_DENIED", f"{entity_id}: {reason}")


class IndexCorruptionError(TeamMemoryError):
    """Index file corrupted or checksum mismatch."""

    def __init__(self, detail: str = "index corrupted") -> None:
        super().__init__("INDEX_CORRUPTION", detail)


class PromotionError(TeamMemoryError):
    """Promotion gate rejected the write."""

    def __init__(self, entity_id: str, reason: str) -> None:
        super().__init__("PROMOTION_REJECTED", f"{entity_id}: {reason}")


class LockTimeoutError(TeamMemoryError):
    """Could not acquire file lock within timeout."""

    def __init__(self, path: str) -> None:
        super().__init__("LOCK_TIMEOUT", f"Could not acquire lock on {path}")
