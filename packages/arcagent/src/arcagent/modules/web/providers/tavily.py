"""Tavily provider adapter for web search and extraction.

Implements WebSearchProvider and WebExtractProvider via the
Tavily REST API.  API key is resolved from vault — never hardcoded.

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

_logger = logging.getLogger("arcagent.modules.web.providers.tavily")

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"

_SECRET_NAME = "tavily_api_key"  # noqa: S105


class TavilyProvider:
    """Tavily adapter implementing WebSearchProvider + WebExtractProvider.

    Construct via ``TavilyProvider.create(api_key, timeout_s)`` after
    resolving the API key from vault.
    """

    def __init__(self, api_key: str, timeout_s: float = 30.0) -> None:
        if not api_key:
            raise ProviderConfigMissing("tavily", _SECRET_NAME)
        self._api_key = api_key
        self._timeout_s = timeout_s
        # Long-lived client; populated on first use via _get_client().
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def create(cls, api_key: str, timeout_s: float = 30.0) -> TavilyProvider:
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

    def _auth_payload(self) -> dict[str, str]:
        """Return the api_key dict merged into request payloads."""
        return {"api_key": self._api_key}

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        """Search via Tavily search endpoint.

        Tavily uses ``api_key`` in the POST body, not Authorization header.

        Args:
            query: Search query string.
            limit: Maximum results to return.

        Returns:
            List of SearchHit instances.

        Raises:
            SearchFailed: On any HTTP or parse error.
        """
        payload: dict[str, Any] = {
            **self._auth_payload(),
            "query": query,
            "max_results": limit,
        }
        try:
            client = self._get_client()
            resp = await client.post(
                _SEARCH_URL,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise SearchFailed(
                f"Tavily search {format_http_error(exc)}",
                details={"status_code": exc.response.status_code},
            ) from exc
        except httpx.RequestError as exc:
            raise SearchFailed(
                f"Tavily search request error: {exc}",
            ) from exc

        return _parse_search_results(data)

    async def extract(self, url: str) -> ExtractResult:
        """Extract content from URL via Tavily extract endpoint.

        Args:
            url: Target URL to extract.

        Returns:
            ExtractResult with Markdown content.

        Raises:
            ExtractFailed: On any HTTP or parse error.
        """
        payload: dict[str, Any] = {
            **self._auth_payload(),
            "urls": [url],
        }
        try:
            client = self._get_client()
            resp = await client.post(
                _EXTRACT_URL,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ExtractFailed(
                f"Tavily extract {format_http_error(exc)}",
                details={"url": url, "status_code": exc.response.status_code},
            ) from exc
        except httpx.RequestError as exc:
            raise ExtractFailed(
                f"Tavily extract request error for {url}: {exc}",
                details={"url": url},
            ) from exc

        return _parse_extract_result(url, data)


def _parse_search_results(data: dict[str, Any]) -> list[SearchHit]:
    """Parse Tavily search response into SearchHit list.

    Tavily returns ``{"results": [{"url": ..., "title": ..., "content": ..., "score": ...}]}``.
    """
    results = data.get("results", [])
    hits: list[SearchHit] = []
    for item in results:
        try:
            hits.append(
                SearchHit(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("content", item.get("snippet", "")),
                    score=item.get("score"),
                )
            )
        except Exception as exc:
            _logger.warning("Skipping malformed Tavily search result: %s", exc)
    return hits


def _parse_extract_result(url: str, data: dict[str, Any]) -> ExtractResult:
    """Parse Tavily extract response into ExtractResult.

    Tavily extract returns ``{"results": [{"url": ..., "raw_content": ...}]}``.
    """
    results = data.get("results", [])
    # Find the result matching our URL; fall back to first if no match
    item: dict[str, Any] = {}
    for r in results:
        if r.get("url") == url:
            item = r
            break
    if not item and results:
        item = results[0]

    return ExtractResult(
        url=url,
        title=item.get("title", ""),
        content=item.get("raw_content", item.get("content", "")),
        links=[],  # Tavily extract does not return link lists
        fetched_at=time.time(),
    )


__all__ = ["TavilyProvider"]
