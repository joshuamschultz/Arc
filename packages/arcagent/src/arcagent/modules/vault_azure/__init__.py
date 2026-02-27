"""Azure Key Vault backend for ArcLLM VaultResolver.

Uses DefaultAzureCredential which automatically picks up:
- Managed Identity (on Azure VMs, Container Apps, AKS)
- Azure CLI credentials (local dev)
- Environment variables (service principal)

No credentials touch the filesystem.

Configuration in arcllm config.toml:
    [vault]
    backend = "arcagent.modules.vault_azure:AzureKeyVaultBackend"
    url = "https://kv-joshagent.vault.azure.net/"
    cache_ttl_seconds = 300

Requires: pip install azure-identity azure-keyvault-secrets
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("arcagent.modules.vault_azure")


class AzureKeyVaultBackend:
    """Azure Key Vault secret retrieval via DefaultAzureCredential.

    Implements the VaultBackend protocol (get_secret, is_available).

    Vault URL resolution order:
        1. Constructor argument
        2. AZURE_KEYVAULT_URL environment variable

    Requires: pip install azure-identity azure-keyvault-secrets
    """

    def __init__(self, vault_url: str = "") -> None:
        self._vault_url = vault_url or os.environ.get("AZURE_KEYVAULT_URL", "")
        self._client: object | None = None
        self._available: bool | None = None

        if not self._vault_url:
            msg = (
                "Azure Key Vault URL not configured. Set AZURE_KEYVAULT_URL "
                "environment variable."
            )
            raise ValueError(msg)

    def _ensure_client(self) -> object:
        """Lazy-init the SecretClient (avoids import cost at startup)."""
        if self._client is not None:
            return self._client

        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import-untyped]
            from azure.keyvault.secrets import SecretClient  # type: ignore[import-untyped]
        except ImportError as e:
            msg = (
                "Azure Key Vault SDK not installed. "
                "Install with: pip install azure-identity azure-keyvault-secrets"
            )
            raise ImportError(msg) from e

        credential = DefaultAzureCredential()
        self._client = SecretClient(vault_url=self._vault_url, credential=credential)
        return self._client

    def get_secret(self, path: str) -> str | None:
        """Retrieve a secret by name from Azure Key Vault.

        Args:
            path: Secret name in Key Vault (e.g., "azure-openai-api-key").

        Returns:
            Secret value string, or None if not found.
        """
        client = self._ensure_client()
        try:
            secret = client.get_secret(path)  # type: ignore[union-attr]
            return secret.value  # type: ignore[union-attr]
        except Exception:
            logger.warning("Key Vault lookup failed for '%s'", path, exc_info=True)
            return None

    def is_available(self) -> bool:
        """Check if Key Vault is reachable. Caches the result."""
        if self._available is not None:
            return self._available

        try:
            client = self._ensure_client()
            next(
                client.list_properties_of_secrets(max_page_size=1),  # type: ignore[union-attr]
                None,
            )
            self._available = True
        except Exception:
            logger.warning("Key Vault not available at %s", self._vault_url, exc_info=True)
            self._available = False

        return self._available


__all__ = ["AzureKeyVaultBackend"]
