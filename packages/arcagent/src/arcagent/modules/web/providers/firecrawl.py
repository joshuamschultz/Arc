"""Firecrawl provider adapter for web search and extraction.

Implements WebSearchProvider and WebExtractProvider via the
Firecrawl REST API.  API key is resolved from vault — never hardcoded.

Performance (SPEC-018 Wave B1):
  A single ``httpx.AsyncClient`` is created lazily on first use and
  reused across all calls, avoiding per-request TCP/TLS handshake cost.
  Call ``await provider.close()`` during shutdown to drain the pool.

Spec: SPEC-018 T4.8.2
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from arcagent.modules.web.errors import ExtractFailed, ProviderConfigMissing, SearchFailed
from arcagent.modules.web.protocols import ExtractResult, SearchHit
from arcagent.modules.web.providers._shared import format_http_error

_logger = logging.getLogger("arcagent.modules.web.providers.firecrawl")

_SEARCH_URL = "https://api.firecrawl.dev/v1/search"
_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"

_SECRET_NAME = "firecrawl_api_key"  # noqa: S105


class FirecrawlProvider:
    """Firecrawl adapter implementing WebSearchProvider + WebExtractProvider.

    Construct via ``FirecrawlProvider.create(api_key, timeout_s)`` after
    resolving the API key from vault.
    """

    def __init__(self, api_key: str, timeout_s: float = 30.0) -> None:
        if not api_key:
            raise ProviderConfigMissing("firecrawl", _SECRET_NAME)
        self._api_key = api_key
        self._timeout_s = timeout_s
        # Long-lived client; populated on first use via _get_client().
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def create(cls, api_key: str, timeout_s: float = 30.0) -> FirecrawlProvider:
        """Factory — validates api_key is non-empty before constructing."""
        return cls(api_key=api_key, timeout_s=timeout_s)

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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        """Search via Firecrawl search endpoint.

        Args:
            query: Search query string.
            limit: Maximum results to return.

        Returns:
            List of SearchHit instances.

        Raises:
            SearchFailed: On any HTTP or parse error.
        """
        payload: dict[str, Any] = {"query": query, "limit": limit}
        try:
            client = self._get_client()
            resp = await client.post(
                _SEARCH_URL,
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise SearchFailed(
                f"Firecrawl search {format_http_error(exc)}",
                details={"status_code": exc.response.status_code},
            ) from exc
        except httpx.RequestError as exc:
            raise SearchFailed(
                f"Firecrawl search request error: {exc}",
            ) from exc

        return _parse_search_results(data)

    async def extract(self, url: str) -> ExtractResult:
        """Extract content from URL via Firecrawl scrape endpoint.

        Uses ``formats=["markdown"]`` so content is always Markdown.

        Args:
            url: Target URL to extract.

        Returns:
            ExtractResult with Markdown content.

        Raises:
            ExtractFailed: On any HTTP or parse error.
        """
        payload: dict[str, Any] = {"url": url, "formats": ["markdown", "links"]}
        try:
            client = self._get_client()
            resp = await client.post(
                _SCRAPE_URL,
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ExtractFailed(
                f"Firecrawl scrape {format_http_error(exc)}",
                details={"url": url, "status_code": exc.response.status_code},
            ) from exc
        except httpx.RequestError as exc:
            raise ExtractFailed(
                f"Firecrawl scrape request error for {url}: {exc}",
                details={"url": url},
            ) from exc

        return _parse_extract_result(url, data)


def _parse_search_results(data: dict[str, Any]) -> list[SearchHit]:
    """Parse Firecrawl search response into SearchHit list.

    Firecrawl v1 returns ``{"data": [{"url": ..., "title": ..., ...}]}``.
    """
    results = data.get("data", data.get("results", []))
    hits: list[SearchHit] = []
    for item in results:
        try:
            hits.append(
                SearchHit(
                    url=item.get("url", ""),
                    title=item.get("title", item.get("metadata", {}).get("title", "")),
                    snippet=item.get("description", item.get("snippet", "")),
                    score=item.get("score"),
                )
            )
        except Exception as exc:
            _logger.warning("Skipping malformed Firecrawl search result: %s", exc)
    return hits


def _parse_extract_result(url: str, data: dict[str, Any]) -> ExtractResult:
    """Parse Firecrawl scrape response into ExtractResult.

    Firecrawl v1 scrape returns ``{"data": {"markdown": ..., "links": [...]}}``.
    """
    inner = data.get("data", data)
    metadata = inner.get("metadata", {})
    return ExtractResult(
        url=url,
        title=metadata.get("title", inner.get("title", "")),
        content=inner.get("markdown", inner.get("content", "")),
        links=inner.get("links", []),
        fetched_at=time.time(),
    )


__all__ = ["FirecrawlProvider"]
