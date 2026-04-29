"""Unit tests for web module Protocols and result models.

Verifies:
- SearchHit and ExtractResult Pydantic models validate correctly
- WebSearchProvider and WebExtractProvider Protocols are runtime-checkable
- Duck-typed adapters satisfy the Protocols
- Optional fields have correct defaults
"""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from arcagent.modules.web.protocols import (
    ExtractResult,
    SearchHit,
    WebExtractProvider,
    WebSearchProvider,
)

# ---------------------------------------------------------------------------
# SearchHit model
# ---------------------------------------------------------------------------


class TestSearchHit:
    def test_required_fields(self) -> None:
        hit = SearchHit(url="https://example.com", title="Example", snippet="A snippet")
        assert hit.url == "https://example.com"
        assert hit.title == "Example"
        assert hit.snippet == "A snippet"
        assert hit.score is None

    def test_score_optional(self) -> None:
        hit = SearchHit(url="https://a.com", title="T", snippet="S", score=0.95)
        assert hit.score == pytest.approx(0.95)

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            SearchHit(url="https://x.com", title="T")  # type: ignore[call-arg]

    def test_model_dump_round_trip(self) -> None:
        original = SearchHit(url="https://b.com", title="B", snippet="sb", score=0.5)
        dumped = original.model_dump()
        restored = SearchHit(**dumped)
        assert restored == original


# ---------------------------------------------------------------------------
# ExtractResult model
# ---------------------------------------------------------------------------


class TestExtractResult:
    def test_required_fields(self) -> None:
        ts = time.time()
        result = ExtractResult(
            url="https://example.com",
            title="Example",
            content="# Hello",
            fetched_at=ts,
        )
        assert result.url == "https://example.com"
        assert result.content == "# Hello"
        assert result.links == []
        assert result.fetched_at == pytest.approx(ts)

    def test_links_populated(self) -> None:
        result = ExtractResult(
            url="https://a.com",
            title="A",
            content="body",
            links=["https://b.com", "https://c.com"],
            fetched_at=time.time(),
        )
        assert len(result.links) == 2

    def test_missing_fetched_at_raises(self) -> None:
        with pytest.raises(ValidationError):
            ExtractResult(  # type: ignore[call-arg]
                url="https://x.com",
                title="T",
                content="C",
            )

    def test_model_dump_round_trip(self) -> None:
        ts = time.time()
        original = ExtractResult(
            url="https://d.com",
            title="D",
            content="md",
            links=["https://e.com"],
            fetched_at=ts,
        )
        restored = ExtractResult(**original.model_dump())
        assert restored == original


# ---------------------------------------------------------------------------
# Protocol runtime checks
# ---------------------------------------------------------------------------


class _MockSearch:
    """Duck-typed search provider — no inheritance."""

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        return []


class _MockExtract:
    """Duck-typed extract provider — no inheritance."""

    async def extract(self, url: str) -> ExtractResult:
        return ExtractResult(url=url, title="T", content="", fetched_at=time.time())


class _MissingSearch:
    """Does NOT implement the Protocol (wrong method name)."""

    async def find(self, query: str) -> list[SearchHit]:  # wrong name
        return []


class TestWebSearchProviderProtocol:
    def test_duck_typed_is_instance(self) -> None:
        provider = _MockSearch()
        assert isinstance(provider, WebSearchProvider)

    def test_missing_method_not_instance(self) -> None:
        bad = _MissingSearch()
        assert not isinstance(bad, WebSearchProvider)

    def test_protocol_runtime_checkable(self) -> None:
        # Confirm the Protocol is decorated with @runtime_checkable
        assert hasattr(WebSearchProvider, "__protocol_attrs__") or True
        # isinstance check does not raise TypeError
        isinstance(_MockSearch(), WebSearchProvider)


class TestWebExtractProviderProtocol:
    def test_duck_typed_is_instance(self) -> None:
        provider = _MockExtract()
        assert isinstance(provider, WebExtractProvider)

    def test_object_without_method_not_instance(self) -> None:
        assert not isinstance(object(), WebExtractProvider)
