"""Error hierarchy for the bio-memory module.

All bio-memory errors extend ArcAgentError from core,
keeping the structured error contract (code, component, details)
while living alongside the module they serve.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError


class BioMemoryError(ArcAgentError):
    """Base for bio-memory module errors."""

    _component = "bio_memory"


class ConsolidationError(BioMemoryError):
    """Consolidation failure (significance evaluation or episode creation)."""

    def __init__(
        self,
        code: str = "BIO_MEMORY_CONSOLIDATION",
        message: str = "Consolidation failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, details=details)


class RetrievalError(BioMemoryError):
    """Search or recall failure."""

    def __init__(
        self,
        code: str = "BIO_MEMORY_RETRIEVAL",
        message: str = "Retrieval failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=code, message=message, details=details)
