"""Tests for AzureKeyVaultBackend in arcagent.modules.vault.backends.azure.

These tests mock the Azure SDK so no real vault is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.vault.backends.azure import AzureKeyVaultBackend
from arcagent.modules.vault.protocol import VaultUnreachable


def test_init_requires_vault_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_KEYVAULT_URL", raising=False)
    with pytest.raises(ValueError, match="URL not configured"):
        AzureKeyVaultBackend()


def test_init_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://my-vault.vault.azure.net/")
    backend = AzureKeyVaultBackend()
    assert backend._vault_url == "https://my-vault.vault.azure.net/"


def test_init_constructor_url_takes_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://env-vault.vault.azure.net/")
    backend = AzureKeyVaultBackend(vault_url="https://ctor-vault.vault.azure.net/")
    assert backend._vault_url == "https://ctor-vault.vault.azure.net/"


def test_ensure_client_raises_import_error_without_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://my-vault.vault.azure.net/")
    backend = AzureKeyVaultBackend()

    with patch.dict("sys.modules", {"azure.identity": None, "azure.keyvault.secrets": None}):
        with pytest.raises(ImportError, match="azure-identity"):
            backend._ensure_client()


@pytest.mark.asyncio
async def test_get_secret_returns_value() -> None:
    """get_secret returns the secret value when found."""
    mock_secret = MagicMock()
    mock_secret.value = "my-secret-value"

    mock_client = MagicMock()
    mock_client.get_secret.return_value = mock_secret

    with patch(
        "arcagent.modules.vault.backends.azure.AzureKeyVaultBackend._ensure_client",
        return_value=mock_client,
    ):
        backend = AzureKeyVaultBackend(vault_url="https://my-vault.vault.azure.net/")
        result = await backend.get_secret("my-key")

    assert result == "my-secret-value"


@pytest.mark.asyncio
async def test_get_secret_returns_none_for_not_found() -> None:
    """ResourceNotFoundError is converted to None return."""
    mock_client = MagicMock()

    # Simulate ResourceNotFoundError by name
    class ResourceNotFoundError(Exception):
        pass

    mock_client.get_secret.side_effect = ResourceNotFoundError("not found")

    with patch(
        "arcagent.modules.vault.backends.azure.AzureKeyVaultBackend._ensure_client",
        return_value=mock_client,
    ):
        backend = AzureKeyVaultBackend(vault_url="https://my-vault.vault.azure.net/")
        result = await backend.get_secret("missing-key")

    assert result is None


@pytest.mark.asyncio
async def test_get_secret_raises_vault_unreachable_on_network_error() -> None:
    """Network error → VaultUnreachable (not swallowed)."""
    mock_client = MagicMock()
    mock_client.get_secret.side_effect = ConnectionError("timeout")

    with patch(
        "arcagent.modules.vault.backends.azure.AzureKeyVaultBackend._ensure_client",
        return_value=mock_client,
    ):
        backend = AzureKeyVaultBackend(vault_url="https://my-vault.vault.azure.net/")
        with pytest.raises(VaultUnreachable):
            await backend.get_secret("my-key")


def test_cache_ttl_accepted_for_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cache_ttl_seconds is accepted (backward compat) without breaking."""
    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://my-vault.vault.azure.net/")
    backend = AzureKeyVaultBackend(cache_ttl_seconds=600)
    assert backend._cache_ttl_seconds == 600
