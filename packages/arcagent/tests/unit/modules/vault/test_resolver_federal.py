"""Tests for resolve_secret at federal tier.

Federal tier contract:
- backend is None              → hard error (VaultUnreachable), NO env fallback
- backend raises VaultUnreachable → hard error, NO env fallback
- backend returns None (secret missing) → RuntimeError
- backend returns value        → value returned
- env var is set but tier=federal → STILL hard error (env ignored)
"""

from __future__ import annotations

import os

import pytest

from arcagent.modules.vault.protocol import VaultUnreachable
from arcagent.modules.vault.resolver import resolve_secret


class _AlwaysRaisesBackend:
    """Backend that always raises VaultUnreachable."""

    async def get_secret(self, path: str) -> str | None:
        raise VaultUnreachable("simulated vault outage")


class _ReturnsValueBackend:
    """Backend that always returns a value."""

    def __init__(self, value: str) -> None:
        self._value = value

    async def get_secret(self, path: str) -> str | None:
        return self._value


class _ReturnsNoneBackend:
    """Backend that returns None (secret not found)."""

    async def get_secret(self, path: str) -> str | None:
        return None


@pytest.mark.asyncio
async def test_federal_no_backend_raises_hard_error() -> None:
    """Federal with no backend is always a hard error."""
    with pytest.raises(VaultUnreachable, match="federal"):
        await resolve_secret(
            "my-secret",
            tier="federal",
            backend=None,
            env_fallback_var=None,
        )


@pytest.mark.asyncio
async def test_federal_no_backend_ignores_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if env var is set, federal tier must NOT fall back to env."""
    monkeypatch.setenv("MY_SECRET", "env-value")
    with pytest.raises(VaultUnreachable, match="federal"):
        await resolve_secret(
            "my-secret",
            tier="federal",
            backend=None,
            env_fallback_var="MY_SECRET",
        )


@pytest.mark.asyncio
async def test_federal_vault_unreachable_raises_hard_error() -> None:
    """Backend raises VaultUnreachable → propagates as hard error at federal."""
    backend = _AlwaysRaisesBackend()
    with pytest.raises(VaultUnreachable):
        await resolve_secret(
            "my-secret",
            tier="federal",
            backend=backend,
            env_fallback_var=None,
        )


@pytest.mark.asyncio
async def test_federal_vault_unreachable_ignores_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Federal tier must NOT fall back to env even if backend raises and env is set."""
    monkeypatch.setenv("MY_SECRET", "env-value")
    backend = _AlwaysRaisesBackend()
    with pytest.raises(VaultUnreachable):
        await resolve_secret(
            "my-secret",
            tier="federal",
            backend=backend,
            env_fallback_var="MY_SECRET",
        )


@pytest.mark.asyncio
async def test_federal_secret_not_in_vault_raises() -> None:
    """Federal: secret not found (None) → RuntimeError."""
    backend = _ReturnsNoneBackend()
    with pytest.raises(RuntimeError, match="federal"):
        await resolve_secret(
            "missing-secret",
            tier="federal",
            backend=backend,
        )


@pytest.mark.asyncio
async def test_federal_vault_returns_value_succeeds() -> None:
    """Happy path: vault returns a value at federal tier."""
    backend = _ReturnsValueBackend("super-secret")
    result = await resolve_secret(
        "my-secret",
        tier="federal",
        backend=backend,
    )
    assert result == "super-secret"
