"""Unit tests for WebModule — tool registration and config loading.

Verifies:
- WebConfig validates correctly with all field combinations
- WebModule construction with custom config
- Federal tier raises RuntimeError on empty url_allowlist at startup
- web_search calls provider and emits audit event
- web_extract enforces URL policy and emits audit events
- Content truncation at max_content_bytes
- Provider injection works (set_search_provider / set_extract_provider)
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from arcagent.modules.web.config import WebConfig
from arcagent.modules.web.errors import URLNotAllowed
from arcagent.modules.web.protocols import ExtractResult, SearchHit
from arcagent.modules.web.web_module import WebModule, _truncate_content

# ---------------------------------------------------------------------------
# WebConfig tests
# ---------------------------------------------------------------------------


class TestWebConfig:
    def test_defaults(self) -> None:
        cfg = WebConfig()
        assert cfg.search_provider == "tavily"
        assert cfg.extract_provider == "firecrawl"
        assert cfg.tier == "personal"
        assert cfg.url_allowlist == []
        assert cfg.max_content_bytes == 1_000_000
        assert cfg.pii_redaction_enabled is True

    def test_custom_values(self) -> None:
        cfg = WebConfig(
            search_provider="parallel",
            extract_provider="tavily",
            tier="federal",
            url_allowlist=["https://api.gov/*"],
            max_content_bytes=512_000,
        )
        assert cfg.search_provider == "parallel"
        assert cfg.tier == "federal"
        assert cfg.url_allowlist == ["https://api.gov/*"]
        assert cfg.max_content_bytes == 512_000

    def test_invalid_provider_raises(self) -> None:
        with pytest.raises(ValidationError):
            WebConfig(search_provider="unknown_provider")  # type: ignore[arg-type]

    def test_tier_stored_as_string(self) -> None:
        # tier is a freeform str; enforcement happens at runtime (startup/vault resolver)
        # All three standard tiers must be accepted
        for tier in ["federal", "enterprise", "personal"]:
            cfg = WebConfig(tier=tier)
            assert cfg.tier == tier

    def test_max_content_bytes_minimum(self) -> None:
        with pytest.raises(ValidationError):
            WebConfig(max_content_bytes=0)

    def test_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            WebConfig(nonexistent_field="value")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# WebModule construction
# ---------------------------------------------------------------------------


class TestWebModuleConstruction:
    def test_default_construction(self) -> None:
        module = WebModule()
        assert module.name == "web"

    def test_custom_config(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["https://gov.example/*"]})
        assert module._config.tier == "federal"

    def test_set_search_provider(self) -> None:
        module = WebModule()
        mock_provider = MagicMock()
        module.set_search_provider(mock_provider)
        assert module._search_provider is mock_provider

    def test_set_extract_provider(self) -> None:
        module = WebModule()
        mock_provider = MagicMock()
        module.set_extract_provider(mock_provider)
        assert module._extract_provider is mock_provider


# ---------------------------------------------------------------------------
# Startup: federal empty-allowlist rejection
# ---------------------------------------------------------------------------


def _make_mock_ctx(bus: Any = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bus = bus or AsyncMock()
    ctx.tool_registry = MagicMock()
    ctx.vault_backend = None
    return ctx


class TestFederalStartupEnforcement:
    async def test_federal_empty_allowlist_raises(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": []})
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(return_value=[])
        mock_extract = AsyncMock()
        mock_extract.extract = AsyncMock(
            return_value=ExtractResult(url="u", title="t", content="c", fetched_at=time.time())
        )
        module.set_search_provider(mock_search)
        module.set_extract_provider(mock_extract)
        ctx = _make_mock_ctx()

        with pytest.raises(RuntimeError, match="url_allowlist"):
            await module.startup(ctx)

    async def test_federal_with_allowlist_starts(self) -> None:
        module = WebModule(
            config={"tier": "federal", "url_allowlist": ["https://api.example.gov/*"]}
        )
        mock_search = AsyncMock()
        mock_extract = AsyncMock()
        module.set_search_provider(mock_search)
        module.set_extract_provider(mock_extract)
        ctx = _make_mock_ctx()

        await module.startup(ctx)
        ctx.bus.emit.assert_awaited()

    async def test_personal_empty_allowlist_starts(self) -> None:
        module = WebModule(config={"tier": "personal", "url_allowlist": []})
        mock_search = AsyncMock()
        mock_extract = AsyncMock()
        module.set_search_provider(mock_search)
        module.set_extract_provider(mock_extract)
        ctx = _make_mock_ctx()

        await module.startup(ctx)  # no error


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


class TestWebSearch:
    async def test_search_calls_provider(self) -> None:
        module = WebModule(config={"tier": "personal"})
        hits = [SearchHit(url="https://a.com", title="A", snippet="s")]
        mock_provider = AsyncMock()
        mock_provider.search = AsyncMock(return_value=hits)
        module.set_search_provider(mock_provider)
        module._bus = AsyncMock()

        results = await module.web_search("test query", limit=5)

        mock_provider.search.assert_awaited_once()
        assert len(results) == 1
        assert results[0].url == "https://a.com"

    async def test_search_emits_audit_event(self) -> None:
        module = WebModule(config={"tier": "personal"})
        mock_provider = AsyncMock()
        mock_provider.search = AsyncMock(return_value=[])
        module.set_search_provider(mock_provider)
        mock_bus = AsyncMock()
        module._bus = mock_bus

        await module.web_search("test query")

        mock_bus.emit.assert_awaited()
        call_args = mock_bus.emit.call_args
        event_name = call_args[0][0]
        event_data = call_args[0][1]
        assert event_name == "web.search"
        assert "query_hash" in event_data
        # Query hash must not be the plaintext query
        assert event_data["query_hash"] != "test query"
        assert "result_count" in event_data

    async def test_search_query_hashed_not_logged_plaintext(self) -> None:
        """Audit event must contain hash, not plaintext query."""
        module = WebModule(config={"tier": "federal", "url_allowlist": ["*"]})
        mock_provider = AsyncMock()
        mock_provider.search = AsyncMock(return_value=[])
        module.set_search_provider(mock_provider)
        mock_bus = AsyncMock()
        module._bus = mock_bus

        sensitive_query = "SSN 123-45-6789"
        await module.web_search(sensitive_query)

        call_args = mock_bus.emit.call_args
        event_data = call_args[0][1]
        # Must not contain the plaintext query in audit
        assert sensitive_query not in str(event_data)


# ---------------------------------------------------------------------------
# web_extract
# ---------------------------------------------------------------------------


class TestWebExtract:
    async def test_extract_allowed_url(self) -> None:
        # Personal tier with explicit allowlist — deny-by-default requires allowlist
        module = WebModule(config={"tier": "personal", "url_allowlist": ["https://a.com"]})
        ts = time.time()
        result = ExtractResult(url="https://a.com", title="A", content="content", fetched_at=ts)
        mock_provider = AsyncMock()
        mock_provider.extract = AsyncMock(return_value=result)
        module.set_extract_provider(mock_provider)
        module._bus = AsyncMock()

        out = await module.web_extract("https://a.com")
        assert out.url == "https://a.com"

    async def test_extract_federal_allowed_url(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["https://api.gov/*"]})
        ts = time.time()
        result = ExtractResult(url="https://api.gov/data", title="T", content="c", fetched_at=ts)
        mock_provider = AsyncMock()
        mock_provider.extract = AsyncMock(return_value=result)
        module.set_extract_provider(mock_provider)
        module._bus = AsyncMock()

        out = await module.web_extract("https://api.gov/data")
        assert out.url == "https://api.gov/data"

    async def test_extract_federal_denied_url_raises(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["https://api.gov/*"]})
        mock_provider = AsyncMock()
        module.set_extract_provider(mock_provider)
        mock_bus = AsyncMock()
        module._bus = mock_bus

        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://evil.com/steal")

        # Provider must NOT have been called
        mock_provider.extract.assert_not_awaited()

    async def test_extract_url_denied_emits_audit_event(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": ["https://trusted.gov/*"]})
        mock_provider = AsyncMock()
        module.set_extract_provider(mock_provider)
        mock_bus = AsyncMock()
        module._bus = mock_bus

        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://denied.com/page")

        mock_bus.emit.assert_awaited()
        event_name = mock_bus.emit.call_args[0][0]
        assert event_name == "web.url_denied"

    async def test_extract_emits_audit_event_on_success(self) -> None:
        # Personal tier requires explicit allowlist — deny-by-default
        module = WebModule(config={"tier": "personal", "url_allowlist": ["https://ok.com"]})
        ts = time.time()
        result = ExtractResult(url="https://ok.com", title="OK", content="body", fetched_at=ts)
        mock_provider = AsyncMock()
        mock_provider.extract = AsyncMock(return_value=result)
        module.set_extract_provider(mock_provider)
        mock_bus = AsyncMock()
        module._bus = mock_bus

        await module.web_extract("https://ok.com")

        # Should have emitted web.extract
        emitted_events = [call[0][0] for call in mock_bus.emit.call_args_list]
        assert "web.extract" in emitted_events


# ---------------------------------------------------------------------------
# Content truncation
# ---------------------------------------------------------------------------


class TestContentTruncation:
    def test_no_truncation_when_within_limit(self) -> None:
        content = "Hello world"
        result = _truncate_content(content, max_bytes=1_000, url="https://a.com")
        assert result == content

    def test_truncation_at_byte_limit(self) -> None:
        # 10 ASCII chars = 10 bytes; set limit to 5
        content = "0123456789"
        result = _truncate_content(content, max_bytes=5, url="https://a.com")
        assert len(result.encode("utf-8")) <= 5

    def test_truncation_logs_warning(self, caplog: Any) -> None:
        import logging

        content = "A" * 200
        with caplog.at_level(logging.WARNING, logger="arcagent.modules.web.web_module"):
            _truncate_content(content, max_bytes=10, url="https://big.com")
        assert "truncated" in caplog.text.lower() or "WEB_CONTENT_TOO_LARGE" in caplog.text
