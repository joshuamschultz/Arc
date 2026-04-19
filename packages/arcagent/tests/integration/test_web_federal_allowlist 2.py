"""Integration test — federal URL allowlist enforcement.

End-to-end scenario:
  - WebModule configured at federal tier with a specific URL allowlist
  - web_extract on allowed URL succeeds
  - web_extract on non-allowlisted URL raises URLNotAllowed (provider never called)
  - web_extract with empty allowlist raises RuntimeError at startup
  - URL audit events (web.url_denied) are emitted for denied requests

These tests do NOT make real network calls — providers are injected mocks.

Spec: SPEC-018 T4.8.5 / PRD Epic I2
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.web import URLNotAllowed, WebModule
from arcagent.modules.web.protocols import ExtractResult, SearchHit


def _make_ctx(bus: Any = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bus = bus or AsyncMock()
    ctx.tool_registry = MagicMock()
    ctx.vault_backend = None
    return ctx


def _make_extract_result(url: str) -> ExtractResult:
    return ExtractResult(url=url, title="Title", content="# Content", fetched_at=time.time())


class TestFederalAllowlistEnforcement:
    """Federal tier: deny-by-default; only allowlisted URLs succeed."""

    async def test_allowlisted_url_succeeds(self) -> None:
        module = WebModule(
            config={
                "tier": "federal",
                "url_allowlist": ["https://api.nist.gov/*", "https://csrc.nist.gov/*"],
            }
        )
        mock_extract = AsyncMock()
        mock_extract.extract = AsyncMock(
            return_value=_make_extract_result("https://api.nist.gov/data")
        )
        module.set_extract_provider(mock_extract)
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        result = await module.web_extract("https://api.nist.gov/data")
        assert result.url == "https://api.nist.gov/data"
        mock_extract.extract.assert_awaited_once()

    async def test_non_allowlisted_url_denied(self) -> None:
        module = WebModule(
            config={
                "tier": "federal",
                "url_allowlist": ["https://api.nist.gov/*"],
            }
        )
        mock_extract = AsyncMock()
        module.set_extract_provider(mock_extract)
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        with pytest.raises(URLNotAllowed) as exc_info:
            await module.web_extract("https://attacker.com/exfil")

        # Provider must NOT be called — denial happens before network
        mock_extract.extract.assert_not_awaited()

        # Exception carries URL and tier in details
        assert exc_info.value.details["url"] == "https://attacker.com/exfil"
        assert exc_info.value.details["tier"] == "federal"

    async def test_empty_allowlist_raises_at_startup(self) -> None:
        module = WebModule(config={"tier": "federal", "url_allowlist": []})
        module.set_extract_provider(AsyncMock())
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()

        with pytest.raises(RuntimeError, match="url_allowlist"):
            await module.startup(ctx)

    async def test_denied_url_emits_web_url_denied_event(self) -> None:
        module = WebModule(
            config={
                "tier": "federal",
                "url_allowlist": ["https://trusted.gov/*"],
            }
        )
        mock_extract = AsyncMock()
        module.set_extract_provider(mock_extract)
        module.set_search_provider(AsyncMock())
        mock_bus = AsyncMock()
        ctx = _make_ctx(bus=mock_bus)
        await module.startup(ctx)
        mock_bus.emit.reset_mock()

        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://denied.example.com/page")

        # Must have emitted web.url_denied
        emitted = [call[0][0] for call in mock_bus.emit.call_args_list]
        assert "web.url_denied" in emitted

        denied_call = next(
            call for call in mock_bus.emit.call_args_list if call[0][0] == "web.url_denied"
        )
        event_data = denied_call[0][1]
        assert event_data["tier"] == "federal"
        assert "denied.example.com" in event_data["url"]

    async def test_multiple_patterns_first_matching_allows(self) -> None:
        module = WebModule(
            config={
                "tier": "federal",
                "url_allowlist": ["https://a.gov/*", "https://b.gov/*", "https://c.gov/*"],
            }
        )
        mock_extract = AsyncMock()
        mock_extract.extract = AsyncMock(
            return_value=_make_extract_result("https://b.gov/resource")
        )
        module.set_extract_provider(mock_extract)
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        result = await module.web_extract("https://b.gov/resource")
        assert result.url == "https://b.gov/resource"

    async def test_glob_pattern_enforced_strictly(self) -> None:
        """URL must match the glob exactly; similar-looking URLs are denied."""
        module = WebModule(
            config={
                "tier": "federal",
                "url_allowlist": ["https://api.trusted.gov/v1/*"],
            }
        )
        module.set_extract_provider(AsyncMock())
        module.set_search_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        # Correct base path — allowed
        mock_extract = AsyncMock()
        mock_extract.extract = AsyncMock(
            return_value=_make_extract_result("https://api.trusted.gov/v1/endpoint")
        )
        module.set_extract_provider(mock_extract)

        result = await module.web_extract("https://api.trusted.gov/v1/endpoint")
        assert result is not None

        # Different path prefix — denied
        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://api.trusted.gov/v2/endpoint")


class TestFederalSearchAllowlist:
    """Federal tier: search queries do not need URL allowlist (search != extract)."""

    async def test_search_works_at_federal_tier(self) -> None:
        module = WebModule(
            config={
                "tier": "federal",
                "url_allowlist": ["https://api.gov/*"],  # required but only for extract
            }
        )
        mock_search = AsyncMock()
        mock_search.search = AsyncMock(
            return_value=[SearchHit(url="https://a.com", title="A", snippet="s")]
        )
        module.set_search_provider(mock_search)
        module.set_extract_provider(AsyncMock())
        ctx = _make_ctx()
        await module.startup(ctx)

        results = await module.web_search("federal query")
        assert len(results) == 1
