"""TTL cache wrapper around any VaultBackend.

Wraps any ``VaultBackend`` and caches successful ``get_secret`` results for
``ttl_seconds`` to avoid hammering the secret store on every tool call.

Design decisions:
- In-memory only; no persistence. The cache is per-process and intentionally
  ephemeral.
- ``VaultUnreachable`` is NOT cached — retries go through to the real backend
  so a transient network blip doesn't lock the agent out for the full TTL.
- Cache is keyed by ``path`` (exact string). Case-sensitivity matches the
  underlying backend.
- Thread-safe via asyncio (no threading.Lock needed in a single-event-loop
  process); concurrent awaits for the same key hit the real backend once because
  Python's asyncio coroutines are cooperatively scheduled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from arcagent.modules.vault.protocol import (
    VaultBackend,
    VaultUnreachable,  # noqa: F401 — referenced in docstrings and re-raised by callers
)


@dataclass
class _CacheEntry:
    value: str | None
    expires_at: float


class CachedVaultBackend:
    """TTL-cache decorator for any VaultBackend.

    Successful results (including ``None`` — "secret does not exist") are
    cached for ``ttl_seconds``.  ``VaultUnreachable`` propagates immediately
    without caching so callers always see live reachability status.

    Args:
        backend: The underlying vault backend to wrap.
        ttl_seconds: How long to cache each secret value.  Defaults to 300 s
            (5 min), matching the Azure KV default.
    """

    def __init__(self, backend: VaultBackend, ttl_seconds: int = 300) -> None:
        self._backend = backend
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}

    async def get_secret(self, path: str) -> str | None:
        """Return cached value if fresh; otherwise delegate to real backend.

        Args:
            path: Secret identifier forwarded to the underlying backend.

        Returns:
            Secret value or ``None`` if not found.

        Raises:
            VaultUnreachable: Propagated from the underlying backend without
                caching so the next call retries the real backend.
        """
        entry = self._cache.get(path)
        if entry is not None and time.monotonic() < entry.expires_at:
            return entry.value

        # May raise VaultUnreachable — intentionally not caught here
        value = await self._backend.get_secret(path)

        # Cache the result (including None = "not found") on success
        self._cache[path] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        return value

    def invalidate(self, path: str | None = None) -> None:
        """Expire a single entry or flush the entire cache.

        Args:
            path: If provided, only that entry is invalidated.  If ``None``,
                the entire cache is cleared.
        """
        if path is None:
            self._cache.clear()
        else:
            self._cache.pop(path, None)

    @property
    def backend(self) -> Any:
        """The underlying VaultBackend being wrapped."""
        return self._backend


__all__ = ["CachedVaultBackend"]
