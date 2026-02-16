"""Tests for protocol definitions."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from arcagent.core.protocols import (
    EvalModelProtocol,
    TelemetryProtocol,
    VaultResolverProtocol,
)


class TestVaultResolverProtocol:
    def test_isinstance_check(self) -> None:
        class FakeVault:
            def resolve_secret(self, path: str, key: str) -> str:
                return "secret"

        assert isinstance(FakeVault(), VaultResolverProtocol)

    def test_non_conforming_rejected(self) -> None:
        class NoVault:
            pass

        assert not isinstance(NoVault(), VaultResolverProtocol)


class TestEvalModelProtocol:
    async def test_isinstance_check(self) -> None:
        class FakeModel:
            async def __call__(self, prompt: str) -> str:
                return "response"

        assert isinstance(FakeModel(), EvalModelProtocol)


class TestTelemetryProtocol:
    def test_isinstance_check(self) -> None:
        class FakeTelemetry:
            def session_span(self, task: str) -> Any:
                @asynccontextmanager
                async def _span() -> Any:
                    yield None

                return _span()

            def turn_span(self, turn_number: int) -> Any:
                return self.session_span("")

            def tool_span(self, tool_name: str, args: dict[str, Any]) -> Any:
                return self.session_span("")

            def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
                pass

        assert isinstance(FakeTelemetry(), TelemetryProtocol)
