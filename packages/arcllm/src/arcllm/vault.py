"""VaultResolver — API key resolution from vault backends with TTL cache."""

from __future__ import annotations

import importlib
import logging
import os
import time
from typing import Protocol, runtime_checkable

from arcllm.exceptions import ArcLLMConfigError

logger = logging.getLogger("arcllm.vault")


@runtime_checkable
class VaultBackend(Protocol):
    """Protocol for vault backend implementations."""

    def get_secret(self, path: str) -> str | None:
        """Retrieve a secret by path. Returns None if not found."""
        ...

    def is_available(self) -> bool:
        """Check if the vault backend is reachable."""
        ...


class VaultResolver:
    """Resolve API keys from vault with TTL cache and env var fallback.

    Resolution order:
        1. If vault backend + vault_path: try vault (check cache first)
        2. Fall back to os.environ[api_key_env]
        3. Raise ArcLLMConfigError if neither source has the key
    """

    def __init__(
        self,
        backend: VaultBackend | None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._backend = backend
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}  # path -> (value, expiry)

    @classmethod
    def from_config(cls, backend_ref: str, cache_ttl_seconds: int) -> VaultResolver:
        """Create VaultResolver from a backend class reference string.

        Args:
            backend_ref: "module.path:ClassName" format.
            cache_ttl_seconds: TTL for cached keys.

        Raises:
            ArcLLMConfigError: On invalid format or missing backend.
        """
        if ":" not in backend_ref:
            raise ArcLLMConfigError(
                f"Vault backend must be in 'module:Class' format, got: '{backend_ref}'"
            )

        module_path, class_name = backend_ref.rsplit(":", 1)
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            raise ArcLLMConfigError(
                f"Vault backend '{backend_ref}' not installed. "
                f"Could not import module '{module_path}'."
            )

        backend_class = getattr(module, class_name, None)
        if backend_class is None:
            raise ArcLLMConfigError(
                f"Vault backend class '{class_name}' not found in '{module_path}'"
            )

        backend = backend_class()
        return cls(backend=backend, cache_ttl_seconds=cache_ttl_seconds)

    def resolve_api_key(
        self,
        api_key_env: str,
        vault_path: str | None,
    ) -> str:
        """Resolve API key: vault first (if configured), then env var.

        Args:
            api_key_env: Environment variable name for the API key.
            vault_path: Path in vault backend. Empty/None skips vault.

        Returns:
            The resolved API key string.

        Raises:
            ArcLLMConfigError: If key cannot be found in any source.
        """
        # Try vault if backend and path are configured
        if self._backend is not None and vault_path:
            vault_key = self._try_vault(vault_path)
            if vault_key is not None:
                return vault_key

        # Fall back to environment variable
        env_key = os.environ.get(api_key_env)
        if env_key is not None:
            return env_key

        raise ArcLLMConfigError(
            f"API key not found. Checked vault path '{vault_path}' and "
            f"environment variable '{api_key_env}'."
        )

    def _try_vault(self, path: str) -> str | None:
        """Try to get key from vault with caching."""
        # Check cache first
        cached = self._get_cached(path)
        if cached is not None:
            return cached

        # Check availability
        if not self._backend.is_available():
            logger.warning("Vault backend unavailable, falling back to env var")
            return None

        # Fetch from vault
        try:
            value = self._backend.get_secret(path)
        except Exception:
            logger.warning(
                "Vault lookup failed for '%s', falling back to env var",
                path,
                exc_info=True,
            )
            return None

        if value is not None:
            self._set_cached(path, value)
        return value

    def _get_cached(self, path: str) -> str | None:
        """Return cached value if within TTL, else None."""
        entry = self._cache.get(path)
        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._cache[path]
            return None
        return value

    def _set_cached(self, path: str, value: str) -> None:
        """Store value in cache with TTL."""
        self._cache[path] = (value, time.monotonic() + self._cache_ttl)
