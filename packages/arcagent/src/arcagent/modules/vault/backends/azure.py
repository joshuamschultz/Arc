"""Azure Key Vault backend for arcagent.modules.vault.

Uses ``DefaultAzureCredential`` which automatically picks up:
- Managed Identity (on Azure VMs, Container Apps, AKS)
- Azure CLI credentials (local dev)
- Environment variables (service principal)

No credentials touch the filesystem.

Configuration in arcagent.toml:
    [vault]
    backend = "arcagent.modules.vault.backends.azure:AzureKeyVaultBackend"
    url = "https://kv-joshagent.vault.azure.net/"
    cache_ttl_seconds = 300

Requires: pip install azure-identity azure-keyvault-secrets
  OR install the optional dep group: pip install arc-agent[azure]
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from arcagent.modules.vault.protocol import VaultUnreachable

logger = logging.getLogger("arcagent.modules.vault.backends.azure")


class AzureKeyVaultBackend:
    """Azure Key Vault secret retrieval via DefaultAzureCredential.

    Implements the ``VaultBackend`` protocol (``async get_secret``).

    The underlying Azure SDK is synchronous; each call is dispatched to a
    thread pool via ``asyncio.to_thread`` so the event loop is never blocked.

    Vault URL resolution order:
        1. Constructor argument
        2. ``AZURE_KEYVAULT_URL`` environment variable

    Args:
        vault_url: Full Azure Key Vault URL.  Falls back to
            ``AZURE_KEYVAULT_URL`` env var if empty.
        cache_ttl_seconds: Unused here — caching is handled by
            ``CachedVaultBackend`` in the layer above.  Accepted for backward
            compatibility with the old ``vault_azure`` module config schema.

    Raises:
        ValueError: If the vault URL cannot be resolved from either the
            constructor argument or the environment variable.
    """

    def __init__(
        self,
        vault_url: str = "",
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._vault_url = vault_url or os.environ.get("AZURE_KEYVAULT_URL", "")
        # cache_ttl_seconds is accepted but unused here; caching lives in
        # CachedVaultBackend above this layer.
        self._cache_ttl_seconds = cache_ttl_seconds
        # Typed as Any to avoid union-attr noise from the late-bound SDK client.
        self._client: Any = None

        if not self._vault_url:
            msg = (
                "Azure Key Vault URL not configured. Set AZURE_KEYVAULT_URL environment variable."
            )
            raise ValueError(msg)

    def _ensure_client(self) -> Any:
        """Lazy-init the SecretClient (avoids import cost at startup)."""
        if self._client is not None:
            return self._client

        try:
            # Azure SDK is an optional dependency; imported lazily so the
            # module can load without it installed.
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as e:
            msg = (
                "Azure Key Vault SDK not installed. "
                "Install with: pip install azure-identity azure-keyvault-secrets"
                "  OR: pip install arc-agent[azure]"
            )
            raise ImportError(msg) from e

        credential = DefaultAzureCredential()
        self._client = SecretClient(vault_url=self._vault_url, credential=credential)
        return self._client

    def _get_secret_sync(self, path: str) -> str | None:
        """Synchronous secret retrieval; called from a thread pool."""
        client = self._ensure_client()
        try:
            secret = client.get_secret(path)
            return str(secret.value) if secret.value is not None else None
        except Exception as exc:
            # Distinguish "secret not found" (404) from connectivity issues.
            exc_name = type(exc).__name__
            if "ResourceNotFoundError" in exc_name or "NotFound" in exc_name:
                return None
            logger.warning("Key Vault lookup failed for %r: %s", path, exc)
            raise VaultUnreachable(
                f"Azure Key Vault unreachable or auth failed for {path!r}: {exc}"
            ) from exc

    async def get_secret(self, path: str) -> str | None:
        """Retrieve a secret asynchronously from Azure Key Vault.

        Dispatches the synchronous Azure SDK call to a thread pool.

        Args:
            path: Secret name in Key Vault (e.g., ``"azure-openai-api-key"``).

        Returns:
            Secret value string, or ``None`` if not found.

        Raises:
            VaultUnreachable: If Azure Key Vault is unreachable or auth fails.
        """
        return await asyncio.to_thread(self._get_secret_sync, path)


__all__ = ["AzureKeyVaultBackend"]
