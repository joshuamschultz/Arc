"""Tests for vault.unreachable AuditEvent emission via arctrust.audit.

Verifies that VaultUnreachable paths emit a canonical AuditEvent
through the arctrust audit pipeline before propagating the exception.

Task B: migrate logger.warning("AUDIT vault.unreachable ...") → arctrust.emit(AuditEvent).
"""

from __future__ import annotations

from typing import Any

import pytest
from arctrust import AuditEvent

from arcagent.modules.vault.protocol import VaultUnreachable
from arcagent.modules.vault.resolver import resolve_secret

# ---------------------------------------------------------------------------
# Recording sink — collects AuditEvents for assertion
# ---------------------------------------------------------------------------


class MemorySink:
    """In-memory AuditSink for test assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def events_for_action(self, action: str) -> list[AuditEvent]:
        return [e for e in self.events if e.action == action]


# ---------------------------------------------------------------------------
# Backend stubs
# ---------------------------------------------------------------------------


class _AlwaysRaisesBackend:
    async def get_secret(self, path: str) -> str | None:
        raise VaultUnreachable("simulated vault outage")


class _ReturnsValueBackend:
    def __init__(self, value: str) -> None:
        self._value = value

    async def get_secret(self, path: str) -> str | None:
        return self._value


# ---------------------------------------------------------------------------
# Federal tier tests
# ---------------------------------------------------------------------------


class TestFederalVaultUnreachableAuditEvent:
    """Federal tier VaultUnreachable paths must emit AuditEvent before propagating."""

    @pytest.mark.asyncio
    async def test_federal_no_backend_emits_audit_event(self) -> None:
        """No backend at federal tier → AuditEvent(action=vault.unreachable)."""
        sink = MemorySink()
        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "my-secret",
                tier="federal",
                backend=None,
                audit_sink=sink,
            )
        events = sink.events_for_action("vault.unreachable")
        assert len(events) >= 1, (
            f"Expected vault.unreachable AuditEvent, got: {[e.action for e in sink.events]}"
        )

    @pytest.mark.asyncio
    async def test_federal_no_backend_audit_event_fields(self) -> None:
        """AuditEvent must carry outcome=error, target=secret_name, tier=federal."""
        sink = MemorySink()
        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "deploy-key",
                tier="federal",
                backend=None,
                audit_sink=sink,
            )
        event = sink.events_for_action("vault.unreachable")[0]
        assert event.outcome == "error"
        assert event.target == "deploy-key"
        assert event.tier == "federal"

    @pytest.mark.asyncio
    async def test_federal_backend_raises_emits_audit_event(self) -> None:
        """Backend raising VaultUnreachable → AuditEvent before propagation."""
        sink = MemorySink()
        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "db-password",
                tier="federal",
                backend=_AlwaysRaisesBackend(),
                audit_sink=sink,
            )
        events = sink.events_for_action("vault.unreachable")
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_federal_backend_raises_audit_event_has_tier(self) -> None:
        sink = MemorySink()
        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "api-key",
                tier="federal",
                backend=_AlwaysRaisesBackend(),
                audit_sink=sink,
            )
        event = sink.events_for_action("vault.unreachable")[0]
        assert event.tier == "federal"

    @pytest.mark.asyncio
    async def test_null_sink_default_does_not_raise(self) -> None:
        """Default audit_sink (NullSink) must not crash existing call sites."""
        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "my-secret",
                tier="federal",
                backend=None,
                # no audit_sink → defaults to NullSink
            )

    @pytest.mark.asyncio
    async def test_logger_warning_still_emitted(self, caplog: Any) -> None:
        """Defense-in-depth: logger.warning must still fire alongside AuditEvent."""
        sink = MemorySink()
        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "my-secret",
                tier="federal",
                backend=None,
                audit_sink=sink,
            )
        assert any("vault.unreachable" in r.message for r in caplog.records), (
            "logger.warning with vault.unreachable must still fire"
        )


# ---------------------------------------------------------------------------
# Enterprise tier tests
# ---------------------------------------------------------------------------


class TestEnterpriseVaultUnreachableAuditEvent:
    """Enterprise tier VaultUnreachable paths must emit AuditEvent."""

    @pytest.mark.asyncio
    async def test_enterprise_backend_raises_emits_audit_event(self) -> None:
        sink = MemorySink()
        with pytest.raises(RuntimeError):
            await resolve_secret(
                "enterprise-secret",
                tier="enterprise",
                backend=_AlwaysRaisesBackend(),
                env_fallback_var=None,
                audit_sink=sink,
            )
        events = sink.events_for_action("vault.unreachable")
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_enterprise_audit_event_has_tier(self) -> None:
        sink = MemorySink()
        with pytest.raises(RuntimeError):
            await resolve_secret(
                "ent-key",
                tier="enterprise",
                backend=_AlwaysRaisesBackend(),
                env_fallback_var=None,
                audit_sink=sink,
            )
        event = sink.events_for_action("vault.unreachable")[0]
        assert event.tier == "enterprise"
        assert event.target == "ent-key"

    @pytest.mark.asyncio
    async def test_enterprise_no_backend_null_sink_does_not_raise(self) -> None:
        """Enterprise with value-returning backend must not emit vault.unreachable."""
        sink = MemorySink()
        result = await resolve_secret(
            "good-secret",
            tier="enterprise",
            backend=_ReturnsValueBackend("the-value"),
            audit_sink=sink,
        )
        assert result == "the-value"
        # No vault.unreachable events when backend succeeds
        assert len(sink.events_for_action("vault.unreachable")) == 0
