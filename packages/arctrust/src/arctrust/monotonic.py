"""Independent monotonic head contract for rollback-sensitive state."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class AnchorUnavailableError(RuntimeError):
    """The external authority cannot prove or advance its current head."""


class AnchorHead(BaseModel):
    """Validated digest and strictly increasing external revision."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    scope: str = Field(pattern=r"^[a-zA-Z0-9_/-]{1,256}$")
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent: str = Field(default="", max_length=1_048_576)


class MonotonicAnchor(Protocol):
    """Externally custodied CAS head; None is valid only before first creation."""

    @property
    def scope(self) -> str: ...

    def latest(self) -> AnchorHead | None: ...

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead: ...


class BootstrapAuthority(Protocol):
    """Externally issued single-use permission to create one missing anchor."""

    def consume(self, scope: str) -> bool: ...
