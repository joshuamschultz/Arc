"""Tests for resolve_secret at enterprise tier.

Enterprise tier contract:
- vault reachable + secret found → return value (no fallback)
- vault raises VaultUnreachable → WARN + audit event + try env var
- vault returns None (not found) → WARN + try env var
- env var set + vault fails → return env var value
- vault fails + env var also missing → RuntimeError
"""

from __future__ import annotations

import logging

import pytest

from arcagent.modules.vault.protocol import VaultUnreachable
from arcagent.modules.vault.resolver import resolve_secret


class _UnreachableBackend:
    async def get_secret(self, path: str) -> str | None:
        raise VaultUnreachable("vault down")


class _SecretFoundBackend:
    def __init__(self, value: str) -> None:
        self._value = value

    async def get_secret(self, path: str) -> str | None:
        return self._value


class _NotFoundBackend:
    async def get_secret(self, path: str) -> str | None:
        return None


@pytest.mark.asyncio
async def test_enterprise_vault_success_returns_value() -> None:
    """Vault returns a value: enterprise returns it directly."""
    backend = _SecretFoundBackend("vault-value")
    result = await resolve_secret(
        "my-secret",
        tier="enterprise",
        backend=backend,
        env_fallback_var="MY_SECRET",
    )
    assert result == "vault-value"


@pytest.mark.asyncio
async def test_enterprise_vault_unreachable_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VaultUnreachable + env var set → return env var value."""
    monkeypatch.setenv("MY_SECRET", "env-fallback-value")
    backend = _UnreachableBackend()
    result = await resolve_secret(
        "my-secret",
        tier="enterprise",
        backend=backend,
        env_fallback_var="MY_SECRET",
    )
    assert result == "env-fallback-value"


@pytest.mark.asyncio
async def test_enterprise_vault_unreachable_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """VaultUnreachable must emit a WARNING log (audit event)."""
    monkeypatch.setenv("MY_SECRET", "env-value")
    backend = _UnreachableBackend()
    with caplog.at_level(logging.WARNING, logger="arcagent.modules.vault.resolver"):
        await resolve_secret(
            "my-secret",
            tier="enterprise",
            backend=backend,
            env_fallback_var="MY_SECRET",
        )
    # At least one warning mentioning the secret name
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("my-secret" in r.message for r in warnings)


@pytest.mark.asyncio
async def test_enterprise_vault_unreachable_emits_audit_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """VaultUnreachable must emit an AUDIT log entry."""
    monkeypatch.setenv("MY_SECRET", "env-value")
    backend = _UnreachableBackend()
    with caplog.at_level(logging.WARNING, logger="arcagent.modules.vault.resolver"):
        await resolve_secret(
            "my-secret",
            tier="enterprise",
            backend=backend,
            env_fallback_var="MY_SECRET",
        )
    audit_records = [r for r in caplog.records if "AUDIT" in r.message]
    assert audit_records, "Expected at least one AUDIT log record"


@pytest.mark.asyncio
async def test_enterprise_vault_not_found_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault returns None (secret not found) → fall back to env var."""
    monkeypatch.setenv("MY_SECRET", "env-value")
    backend = _NotFoundBackend()
    result = await resolve_secret(
        "my-secret",
        tier="enterprise",
        backend=backend,
        env_fallback_var="MY_SECRET",
    )
    assert result == "env-value"


@pytest.mark.asyncio
async def test_enterprise_vault_and_env_both_missing_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault unreachable + env var not set → RuntimeError with clear message."""
    monkeypatch.delenv("MY_SECRET", raising=False)
    backend = _UnreachableBackend()
    with pytest.raises(RuntimeError, match="enterprise"):
        await resolve_secret(
            "my-secret",
            tier="enterprise",
            backend=backend,
            env_fallback_var="MY_SECRET",
        )


@pytest.mark.asyncio
async def test_enterprise_no_backend_no_env_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No backend + no env var at enterprise → RuntimeError."""
    monkeypatch.delenv("MY_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="enterprise"):
        await resolve_secret(
            "my-secret",
            tier="enterprise",
            backend=None,
            env_fallback_var="MY_SECRET",
        )


@pytest.mark.asyncio
async def test_enterprise_no_backend_env_set_returns_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No backend but env var set → return env var (enterprise allows this)."""
    monkeypatch.setenv("MY_SECRET", "env-only")
    result = await resolve_secret(
        "my-secret",
        tier="enterprise",
        backend=None,
        env_fallback_var="MY_SECRET",
    )
    assert result == "env-only"
