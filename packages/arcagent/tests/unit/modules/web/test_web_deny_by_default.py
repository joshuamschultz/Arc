"""Tests for §3: web_module deny-by-default empty allowlist at ALL tiers.

Empty allowlist must deny all URLs at any tier (personal, enterprise, federal).
Only a configured allowlist opens access.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.web.errors import URLNotAllowed
from arcagent.modules.web.protocols import ExtractResult
from arcagent.modules.web.web_module import WebModule


def _make_mock_ctx(bus: Any = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bus = bus or AsyncMock()
    ctx.tool_registry = MagicMock()
    ctx.vault_backend = None
    return ctx


def _make_module_with_providers(tier: str, allowlist: list[str]) -> WebModule:
    module = WebModule(config={"tier": tier, "url_allowlist": allowlist})
    mock_search = AsyncMock()
    mock_search.search = AsyncMock(return_value=[])
    mock_extract = AsyncMock()
    mock_extract.extract = AsyncMock(
        return_value=ExtractResult(url="u", title="t", content="c", fetched_at=time.time())
    )
    module.set_search_provider(mock_search)
    module.set_extract_provider(mock_extract)
    module._bus = AsyncMock()
    return module


class TestEmptyAllowlistDenyByDefault:
    """Empty allowlist → deny all URLs at every tier (ASI04 + LLM10 fix)."""

    @pytest.mark.parametrize("tier", ["personal", "enterprise"])
    async def test_empty_allowlist_denies_all_extracts(self, tier: str) -> None:
        """At personal and enterprise tiers, empty allowlist must block all URLs."""
        module = _make_module_with_providers(tier=tier, allowlist=[])
        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://example.com/page")

    @pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
    async def test_configured_allowlist_permits_matching_url(self, tier: str) -> None:
        """A configured allowlist pattern must permit matching URLs."""
        module = _make_module_with_providers(
            tier=tier,
            allowlist=["https://example.com/*"],
        )
        # Should not raise for the matching URL
        result = await module.web_extract("https://example.com/page")
        assert result is not None

    @pytest.mark.parametrize("tier", ["personal", "enterprise"])
    async def test_allowlist_blocks_non_matching_url(self, tier: str) -> None:
        """URLs outside the configured allowlist are denied even at personal tier."""
        module = _make_module_with_providers(
            tier=tier,
            allowlist=["https://allowed.example.com/*"],
        )
        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://other.example.com/page")

    async def test_federal_startup_still_fails_on_empty_allowlist(self) -> None:
        """Federal tier still raises at startup (not just on extract)."""
        module = WebModule(config={"tier": "federal", "url_allowlist": []})
        mock_search = AsyncMock()
        mock_extract = AsyncMock()
        module.set_search_provider(mock_search)
        module.set_extract_provider(mock_extract)
        ctx = _make_mock_ctx()
        with pytest.raises(RuntimeError, match="url_allowlist"):
            await module.startup(ctx)


class TestAllowlistAtPersonalTier:
    async def test_personal_empty_allowlist_denies_any_url(self) -> None:
        """Specific regression: old code allowed all at personal with empty list."""
        module = _make_module_with_providers(tier="personal", allowlist=[])
        with pytest.raises(URLNotAllowed):
            await module.web_extract("https://anything.com")

    async def test_personal_wildcard_allowlist_permits_any_url(self) -> None:
        """Explicit wildcard allowlist explicitly opens all traffic."""
        module = _make_module_with_providers(tier="personal", allowlist=["*"])
        result = await module.web_extract("https://anything.com")
        assert result is not None
