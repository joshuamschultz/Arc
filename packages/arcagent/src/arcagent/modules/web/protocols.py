"""Protocols and result models for the web module.

Defines the WebSearchProvider and WebExtractProvider Protocols plus
the Pydantic result models SearchHit and ExtractResult.

Any adapter (Parallel, Firecrawl, Tavily, or third-party) implements
these Protocols without inheriting from a base class — pure duck-typing.

Spec: SPEC-018 T4.8.1
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    """Single result from a web search provider.

    ``score`` is provider-dependent — some return relevance floats,
    others do not rank results.  Callers must not assume it is populated.
    """

    url: str
    title: str
    snippet: str
    score: float | None = None


class ExtractResult(BaseModel):
    """Extracted content from a web page.

    ``content`` is Markdown — providers are responsible for converting
    HTML to Markdown before returning.  ``links`` contains absolute URLs
    found on the page.  ``fetched_at`` is a UNIX timestamp.
    """

    url: str
    title: str
    content: str
    links: list[str] = Field(default_factory=list)
    fetched_at: float


@runtime_checkable
class WebSearchProvider(Protocol):
    """Protocol for web search backends.

    Any adapter implementing ``search`` is a valid WebSearchProvider.
    No inheritance required.
    """

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        """Search the web and return ranked hits.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of SearchHit instances, most relevant first.
        """
        ...


@runtime_checkable
class WebExtractProvider(Protocol):
    """Protocol for web content extraction backends.

    Any adapter implementing ``extract`` is a valid WebExtractProvider.
    No inheritance required.
    """

    async def extract(self, url: str) -> ExtractResult:
        """Fetch and extract content from a URL.

        Args:
            url: Fully-qualified URL to extract.

        Returns:
            ExtractResult with Markdown content and links.
        """
        ...


__all__ = [
    "ExtractResult",
    "SearchHit",
    "WebExtractProvider",
    "WebSearchProvider",
]
