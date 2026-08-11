"""Manifest-owned MCP policy and transport resilience state."""

from __future__ import annotations

from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import Classification


class _Declaration(BaseModel):
    """Frozen manifest declaration; unknown keys are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class McpToolPolicy(_Declaration):
    """Trusted manifest policy for one server-advertised tool."""

    classification: Classification = "state_modifying"
    capability_tags: list[str] = Field(default_factory=list)


class McpResilience(_Declaration):
    """Timeout, retry, and circuit-breaker bounds for an MCP server."""

    timeout_seconds: float = 60.0
    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: float = 0.5
    failure_threshold: int = Field(default=5, ge=1)
    reset_after_seconds: float = 30.0


DEFAULT_POLICY = McpToolPolicy()


class UnavailableError(ExtensionError):
    """The server did not answer after bounded transport attempts."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code="EXTENSION_UNAVAILABLE", message=message, details=details)


class CircuitBreaker:
    """Open after consecutive transport failures and reset after a cooldown."""

    def __init__(self, threshold: int, reset_after_seconds: float) -> None:
        self._threshold = threshold
        self._reset_after_seconds = reset_after_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def retry_after(self) -> float | None:
        if self._opened_at is None:
            return None
        remaining = self._reset_after_seconds - (monotonic() - self._opened_at)
        if remaining > 0:
            return remaining
        self._opened_at = None
        self._failures = self._threshold - 1
        return None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = monotonic()

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None


__all__ = [
    "DEFAULT_POLICY",
    "CircuitBreaker",
    "McpResilience",
    "McpToolPolicy",
    "UnavailableError",
]
