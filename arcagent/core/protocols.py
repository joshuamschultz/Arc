"""Protocol definitions for core component boundaries.

Replaces ``Any`` typing at component interfaces with explicit
structural contracts. Uses ``typing.Protocol`` for duck-typing
compatibility — no inheritance required.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VaultResolverProtocol(Protocol):
    """Structural contract for vault secret resolution.

    Compatible with ArcLLM's VaultResolver and any custom backend
    that implements ``resolve_secret(path, key) -> str``.
    """

    def resolve_secret(self, path: str, key: str) -> str: ...


@runtime_checkable
class EvalModelProtocol(Protocol):
    """Structural contract for eval model callables.

    The eval model accepts a prompt string and returns a response string.
    Used by EntityExtractor, PolicyEngine, and SessionManager.
    """

    async def __call__(self, prompt: str) -> str: ...


@runtime_checkable
class TelemetryProtocol(Protocol):
    """Structural contract for telemetry providers.

    Covers span creation and audit event logging. All span methods
    are async context managers that yield an opaque span handle.
    """

    def session_span(self, task: str) -> AbstractAsyncContextManager[Any]: ...

    def turn_span(self, turn_number: int) -> AbstractAsyncContextManager[Any]: ...

    def tool_span(
        self, tool_name: str, args: dict[str, Any]
    ) -> AbstractAsyncContextManager[Any]: ...

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None: ...
