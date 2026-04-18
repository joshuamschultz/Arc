"""Tests for CachedVaultBackend TTL cache wrapper."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.vault.cache import CachedVaultBackend
from arcagent.modules.vault.protocol import VaultUnreachable


@pytest.fixture
def mock_backend() -> AsyncMock:
    backend = AsyncMock()
    backend.get_secret = AsyncMock(return_value="secret-value")
    return backend


@pytest.mark.asyncio
async def test_first_call_delegates_to_backend(mock_backend: AsyncMock) -> None:
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)
    result = await cache.get_secret("my-key")
    assert result == "secret-value"
    mock_backend.get_secret.assert_called_once_with("my-key")


@pytest.mark.asyncio
async def test_second_call_within_ttl_uses_cache(mock_backend: AsyncMock) -> None:
    """Second call within TTL must NOT call the real backend."""
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)
    await cache.get_secret("my-key")
    await cache.get_secret("my-key")
    # Called exactly once — second hit was served from cache
    assert mock_backend.get_secret.call_count == 1


@pytest.mark.asyncio
async def test_expired_entry_calls_backend_again(mock_backend: AsyncMock) -> None:
    """After TTL expires the backend is called again."""
    cache = CachedVaultBackend(mock_backend, ttl_seconds=0)
    # With ttl=0, any entry expires immediately (expires_at = now + 0)
    await cache.get_secret("my-key")
    # Force-expire by setting expires_at in the past
    cache._cache["my-key"] = cache._cache["my-key"].__class__(
        value="secret-value",
        expires_at=time.monotonic() - 1,
    )
    await cache.get_secret("my-key")
    assert mock_backend.get_secret.call_count == 2


@pytest.mark.asyncio
async def test_vault_unreachable_not_cached(mock_backend: AsyncMock) -> None:
    """VaultUnreachable must NOT be cached — next call should retry."""
    mock_backend.get_secret.side_effect = VaultUnreachable("down")
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)

    with pytest.raises(VaultUnreachable):
        await cache.get_secret("my-key")

    # Key must NOT be in cache after failure
    assert "my-key" not in cache._cache

    # Second call also goes to backend
    with pytest.raises(VaultUnreachable):
        await cache.get_secret("my-key")

    assert mock_backend.get_secret.call_count == 2


@pytest.mark.asyncio
async def test_none_result_is_cached(mock_backend: AsyncMock) -> None:
    """None (secret not found) is cached as a valid result."""
    mock_backend.get_secret.return_value = None
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)

    result1 = await cache.get_secret("missing-key")
    result2 = await cache.get_secret("missing-key")

    assert result1 is None
    assert result2 is None
    assert mock_backend.get_secret.call_count == 1


@pytest.mark.asyncio
async def test_invalidate_single_entry(mock_backend: AsyncMock) -> None:
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)
    await cache.get_secret("key-a")
    await cache.get_secret("key-b")

    cache.invalidate("key-a")

    assert "key-a" not in cache._cache
    assert "key-b" in cache._cache


@pytest.mark.asyncio
async def test_invalidate_all_entries(mock_backend: AsyncMock) -> None:
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)
    await cache.get_secret("key-a")
    await cache.get_secret("key-b")

    cache.invalidate()  # flush all

    assert len(cache._cache) == 0


@pytest.mark.asyncio
async def test_backend_property_exposes_inner_backend(mock_backend: AsyncMock) -> None:
    cache = CachedVaultBackend(mock_backend, ttl_seconds=60)
    assert cache.backend is mock_backend
