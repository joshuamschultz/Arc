"""Tests for VaultResolver — vault integration with TTL cache and env var fallback."""

import os
import time
from unittest.mock import patch

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.vault import VaultBackend, VaultResolver


# ---------------------------------------------------------------------------
# Mock vault backend for testing
# ---------------------------------------------------------------------------


class MockVaultBackend:
    """Simple mock implementing VaultBackend protocol."""

    def __init__(self, secrets: dict[str, str] | None = None, available: bool = True):
        self._secrets = secrets or {}
        self._available = available
        self.get_secret_calls: list[str] = []

    def get_secret(self, path: str) -> str | None:
        self.get_secret_calls.append(path)
        return self._secrets.get(path)

    def is_available(self) -> bool:
        return self._available


class FailingVaultBackend:
    """Backend that raises on get_secret."""

    def get_secret(self, path: str) -> str | None:
        raise ConnectionError("Vault unreachable")

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# VaultBackend protocol
# ---------------------------------------------------------------------------


class TestVaultBackendProtocol:
    def test_mock_satisfies_protocol(self):
        backend = MockVaultBackend()
        assert isinstance(backend, VaultBackend)

    def test_failing_satisfies_protocol(self):
        backend = FailingVaultBackend()
        assert isinstance(backend, VaultBackend)


# ---------------------------------------------------------------------------
# VaultResolver — vault hit
# ---------------------------------------------------------------------------


class TestVaultHit:
    def test_vault_returns_key(self):
        backend = MockVaultBackend(secrets={"secret/anthropic": "sk-vault-key"})
        resolver = VaultResolver(backend=backend)
        with patch.dict(os.environ, {"API_KEY": "sk-env-key"}):
            key = resolver.resolve_api_key("API_KEY", "secret/anthropic")
        assert key == "sk-vault-key"

    def test_vault_called_with_correct_path(self):
        backend = MockVaultBackend(secrets={"my/path": "key"})
        resolver = VaultResolver(backend=backend)
        with patch.dict(os.environ, {"K": "fallback"}):
            resolver.resolve_api_key("K", "my/path")
        assert backend.get_secret_calls == ["my/path"]


# ---------------------------------------------------------------------------
# VaultResolver — fallback to env var
# ---------------------------------------------------------------------------


class TestVaultFallback:
    def test_vault_miss_falls_back_to_env(self):
        backend = MockVaultBackend(secrets={})  # no secrets
        resolver = VaultResolver(backend=backend)
        with patch.dict(os.environ, {"API_KEY": "sk-env-key"}):
            key = resolver.resolve_api_key("API_KEY", "secret/missing")
        assert key == "sk-env-key"

    def test_vault_unreachable_falls_back_to_env(self):
        backend = FailingVaultBackend()
        resolver = VaultResolver(backend=backend)
        with patch.dict(os.environ, {"API_KEY": "sk-env-key"}):
            key = resolver.resolve_api_key("API_KEY", "secret/path")
        assert key == "sk-env-key"

    def test_vault_unavailable_falls_back_to_env(self):
        backend = MockVaultBackend(available=False)
        resolver = VaultResolver(backend=backend)
        with patch.dict(os.environ, {"API_KEY": "sk-env-key"}):
            key = resolver.resolve_api_key("API_KEY", "secret/path")
        assert key == "sk-env-key"
        # Backend should not be called when unavailable
        assert len(backend.get_secret_calls) == 0


# ---------------------------------------------------------------------------
# VaultResolver — no vault configured
# ---------------------------------------------------------------------------


class TestNoVault:
    def test_no_backend_uses_env_var(self):
        resolver = VaultResolver(backend=None)
        with patch.dict(os.environ, {"API_KEY": "sk-env-key"}):
            key = resolver.resolve_api_key("API_KEY", "")
        assert key == "sk-env-key"

    def test_no_backend_no_vault_path_uses_env(self):
        resolver = VaultResolver(backend=None)
        with patch.dict(os.environ, {"API_KEY": "sk-env-key"}):
            key = resolver.resolve_api_key("API_KEY", None)
        assert key == "sk-env-key"

    def test_neither_vault_nor_env_raises(self):
        resolver = VaultResolver(backend=None)
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ArcLLMConfigError, match="API_KEY"):
                resolver.resolve_api_key("API_KEY", "")


# ---------------------------------------------------------------------------
# VaultResolver — TTL cache
# ---------------------------------------------------------------------------


class TestVaultCache:
    def test_cache_hit_within_ttl(self):
        backend = MockVaultBackend(secrets={"path": "cached-key"})
        resolver = VaultResolver(backend=backend, cache_ttl_seconds=60)
        with patch.dict(os.environ, {"K": "env"}):
            key1 = resolver.resolve_api_key("K", "path")
            key2 = resolver.resolve_api_key("K", "path")
        assert key1 == "cached-key"
        assert key2 == "cached-key"
        # Backend called only once (second call hits cache)
        assert len(backend.get_secret_calls) == 1

    def test_cache_expired_refetches(self):
        backend = MockVaultBackend(secrets={"path": "fresh-key"})
        resolver = VaultResolver(backend=backend, cache_ttl_seconds=0)
        with patch.dict(os.environ, {"K": "env"}):
            # With TTL=0, cache expires immediately
            key1 = resolver.resolve_api_key("K", "path")
            # Small sleep to ensure monotonic time advances
            time.sleep(0.01)
            key2 = resolver.resolve_api_key("K", "path")
        assert key1 == "fresh-key"
        assert key2 == "fresh-key"
        # Backend called twice (cache expired between calls)
        assert len(backend.get_secret_calls) == 2


# ---------------------------------------------------------------------------
# VaultResolver — error cases
# ---------------------------------------------------------------------------


class TestVaultErrors:
    def test_backend_not_installed_string(self):
        """When backend is a string that can't be imported, raise clear error."""
        with pytest.raises(ArcLLMConfigError, match="not installed"):
            VaultResolver.from_config("nonexistent.module:Backend", 300)

    def test_invalid_backend_config_no_colon(self):
        """Backend string must have module:Class format."""
        with pytest.raises(ArcLLMConfigError, match="format"):
            VaultResolver.from_config("just_a_module", 300)
