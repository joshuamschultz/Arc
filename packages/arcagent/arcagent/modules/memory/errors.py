"""Error hierarchy for the memory module.

All memory-specific errors extend ArcAgentError from core,
keeping the structured error contract (code, component, details)
while living alongside the module they serve.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError


class AgentMemoryError(ArcAgentError):
    """Base for memory module errors."""

    _component = "memory"


class EntityExtractionError(AgentMemoryError):
    """Entity extraction or index update failure."""

    def __init__(
        self,
        code: str = "MEMORY_EXTRACTION",
        message: str = "Entity extraction failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, details=details)


class SearchError(AgentMemoryError):
    """Search index or query failure."""

    def __init__(
        self,
        code: str = "MEMORY_SEARCH",
        message: str = "Search failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, details=details)
