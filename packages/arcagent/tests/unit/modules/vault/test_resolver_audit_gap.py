"""Tests for §5: vault resolver audit gap fix.

VaultUnreachable at any tier must emit vault.unreachable AuditEvent
before propagating the exception.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from arcagent.modules.vault.protocol import VaultUnreachable
from arcagent.modules.vault.resolver import resolve_secret


class _AlwaysRaisesBackend:
    async def get_secret(self, path: str) -> str | None:
        raise VaultUnreachable("simulated vault outage")


class TestVaultUnreachableAuditEvent:
    """§5: VaultUnreachable must trigger an audit log before propagating."""

    async def test_federal_vault_unreachable_emits_audit_log(
        self, caplog: Any
    ) -> None:
        """vault.unreachable must appear in logs when vault is unreachable at federal."""
        import logging

        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "my-secret",
                tier="federal",
                backend=_AlwaysRaisesBackend(),
            )
        # Audit log must mention vault.unreachable
        assert any(
            "vault.unreachable" in record.message
            for record in caplog.records
        ), f"Expected vault.unreachable in logs, got: {[r.message for r in caplog.records]}"

    async def test_federal_no_backend_emits_audit_log(self, caplog: Any) -> None:
        """vault.unreachable logged when no backend at federal tier."""
        import logging

        with pytest.raises(VaultUnreachable):
            await resolve_secret(
                "my-secret",
                tier="federal",
                backend=None,
            )
        assert any(
            "vault.unreachable" in record.message
            for record in caplog.records
        )

    async def test_enterprise_vault_unreachable_emits_audit_log(
        self, caplog: Any
    ) -> None:
        """vault.unreachable must appear in logs at enterprise tier."""
        import logging

        with pytest.raises(RuntimeError):
            await resolve_secret(
                "my-secret",
                tier="enterprise",
                backend=_AlwaysRaisesBackend(),
                env_fallback_var=None,  # no env var, so RuntimeError propagated
            )
        assert any(
            "vault.unreachable" in record.message or "vault" in record.message.lower()
            for record in caplog.records
        )
