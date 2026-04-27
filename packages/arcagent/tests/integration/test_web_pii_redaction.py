"""Integration test — PII redaction for web search and extraction.

Verifies that PII in queries and extracted content is redacted before
being processed or returned, using arcllm._pii.RegexPiiDetector.

Scenarios:
  - Search query containing SSN is redacted before being sent to provider
  - Extracted content containing email address is redacted in returned result
  - PII in query does not appear in audit event (query_hash used instead)
  - Personal tier with pii_redaction_enabled=False bypasses redaction

These tests do NOT make real network calls — providers are injected mocks.

Spec: SPEC-018 T4.8.4
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from arcagent.modules.web import WebModule
from arcagent.modules.web.protocols import ExtractResult, SearchHit


def _make_ctx(bus: Any = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bus = bus or AsyncMock()
    ctx.tool_registry = MagicMock()
    ctx.vault_backend = None
    return ctx


class TestSearchQueryPiiRedaction:
    """PII in search queries must be redacted before reaching the provider."""

    async def test_ssn_in_query_redacted(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["*"], "pii_redaction_enabled": True})
        captured_queries: list[str] = []

        async def _capture_search(query: str, *, limit: int = 10) -> list[SearchHit]:
            captured_queries.append(query)
            return []

        mock_search = MagicMock()
        mock_search.search = _capture_search
        module.set_search_provider(mock_search)
        module.set_extract_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        await module.web_search("Find records for SSN 123-45-6789")

        assert len(captured_queries) == 1
        sent_query = captured_queries[0]
        # SSN must be redacted; raw SSN must not be in the sent query
        assert "123-45-6789" not in sent_query
        assert "[PII:SSN]" in sent_query

    async def test_email_in_query_redacted(self) -> None:
        module = WebModule(config={"tier": "enterprise", "pii_redaction_enabled": True})
        captured_queries: list[str] = []

        async def _capture_search(query: str, *, limit: int = 10) -> list[SearchHit]:
            captured_queries.append(query)
            return []

        mock_search = MagicMock()
        mock_search.search = _capture_search
        module.set_search_provider(mock_search)
        module.set_extract_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        await module.web_search("Find info about user@example.com")

        sent_query = captured_queries[0]
        assert "user@example.com" not in sent_query
        assert "[PII:EMAIL]" in sent_query

    async def test_clean_query_passes_through_unchanged(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["*"], "pii_redaction_enabled": True})
        captured_queries: list[str] = []

        async def _capture_search(query: str, *, limit: int = 10) -> list[SearchHit]:
            captured_queries.append(query)
            return []

        mock_search = MagicMock()
        mock_search.search = _capture_search
        module.set_search_provider(mock_search)
        module.set_extract_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        clean_query = "Python async programming best practices"
        await module.web_search(clean_query)

        # Clean query should pass through unchanged
        assert captured_queries[0] == clean_query


class TestExtractedContentPiiRedaction:
    """PII in extracted content must be redacted in the returned ExtractResult."""

    async def test_email_in_content_redacted(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["https://example.gov/*"]})
        raw_content = "Contact admin@example.gov for support.\n\nSee our FAQ."
        mock_extract = AsyncMock()
        mock_extract.extract = AsyncMock(
            return_value=ExtractResult(
                url="https://example.gov/contact",
                title="Contact",
                content=raw_content,
                fetched_at=time.time(),
            )
        )
        module.set_extract_provider(mock_extract)
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        result = await module.web_extract("https://example.gov/contact")

        # Email must be redacted in returned content
        assert "admin@example.gov" not in result.content
        assert "[PII:EMAIL]" in result.content
        # Non-PII content preserved
        assert "See our FAQ" in result.content

    async def test_ssn_in_content_redacted(self) -> None:
        # Enterprise with explicit allowlist (deny-by-default requires explicit allow)
        module = WebModule(config={
            "tier": "enterprise",
            "pii_redaction_enabled": True,
            "url_allowlist": ["https://records.example.com/*"],
        })
        raw_content = "Record found: SSN 987-65-4321 belongs to subject."
        mock_extract = AsyncMock()
        mock_extract.extract = AsyncMock(
            return_value=ExtractResult(
                url="https://records.example.com/file",
                title="Record",
                content=raw_content,
                fetched_at=time.time(),
            )
        )
        module.set_extract_provider(mock_extract)
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        result = await module.web_extract("https://records.example.com/file")

        assert "987-65-4321" not in result.content
        assert "[PII:SSN]" in result.content


class TestPiiRedactionAuditPrivacy:
    """PII must not appear in audit events — only query hash."""

    async def test_audit_event_contains_hash_not_plaintext(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["*"], "pii_redaction_enabled": True})
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value=[])
        module.set_search_provider(mock_search)
        module.set_extract_provider(AsyncMock())
        mock_bus = AsyncMock()
        ctx = _make_ctx(bus=mock_bus)
        await module.startup(ctx)
        mock_bus.emit.reset_mock()

        sensitive_query = "records for John SSN 123-45-6789"
        await module.web_search(sensitive_query)

        # Find the web.search audit event
        search_calls = [
            c for c in mock_bus.emit.call_args_list if c[0][0] == "web.search"
        ]
        assert len(search_calls) == 1
        event_data = search_calls[0][0][1]

        # Must not contain the SSN or the name
        data_str = str(event_data)
        assert "123-45-6789" not in data_str
        assert "John" not in data_str
        # Must contain a hash
        assert "query_hash" in event_data
        assert len(event_data["query_hash"]) == 16  # 16 hex chars


class TestPiiRedactionDisabled:
    """Personal tier with pii_redaction_enabled=False sends content as-is."""

    async def test_pii_redaction_disabled_passes_raw_query(self) -> None:
        module = WebModule(config={"tier": "personal", "pii_redaction_enabled": False})
        captured_queries: list[str] = []

        async def _capture(query: str, *, limit: int = 10) -> list[SearchHit]:
            captured_queries.append(query)
            return []

        mock_search = MagicMock()
        mock_search.search = _capture
        module.set_search_provider(mock_search)
        module.set_extract_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        raw_query = "email test@example.com info"
        await module.web_search(raw_query)

        # With redaction disabled at personal tier, query should pass through
        assert captured_queries[0] == raw_query
