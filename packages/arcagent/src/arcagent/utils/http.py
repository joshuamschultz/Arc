"""Shared lazy httpx client lifecycle for provider adapters.

Provider adapters (web search/extract, voice STT/TTS) each hold a single
long-lived ``httpx.AsyncClient`` created lazily on first use and reused
across calls to avoid per-request TCP/TLS handshake cost (SPEC-018 Wave B1).
This mixin centralises that lifecycle so connection-pool limits, timeout
config, or shutdown behaviour are changed in exactly one place.

Subclasses set ``self._timeout_s`` in ``__init__`` and keep only their own
headers/endpoints/request logic.
"""

from __future__ import annotations

import httpx


class LazyHttpProvider:
    """Mixin providing a lazily-created, reusable ``httpx.AsyncClient``.

    Subclasses must set ``self._timeout_s`` before the first ``_get_client``
    call. ``_timeout_s`` is annotated as ``float`` since httpx accepts both
    ``int`` and ``float`` timeouts.
    """

    _timeout_s: float
    _client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared client, creating it lazily on first call."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    async def close(self) -> None:
        """Close the shared httpx client and release its connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["LazyHttpProvider"]
