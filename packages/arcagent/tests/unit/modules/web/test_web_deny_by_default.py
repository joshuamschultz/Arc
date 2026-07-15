"""Tier-differentiated allowlist enforcement on the web capability path (ADR-019).

Federal denies by default (and rejects an empty allowlist at configure time);
personal/enterprise allow by default so ordinary research is not bricked. A
configured (non-empty) allowlist is enforced at every tier. Exercised through
the live web_extract capability + _runtime, not the deleted WebModule class.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.web import _runtime, capabilities
from arcagent.modules.web.errors import URLNotAllowed
from arcagent.modules.web.protocols import ExtractResult


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure_with_extract(tier: str, allowlist: list[str]) -> None:
    _runtime.configure(
        config={"tier": tier, "url_allowlist": allowlist},
        telemetry=MagicMock(),
        agent_name="test-agent",
    )
    stub = MagicMock()
    stub.extract = AsyncMock(
        return_value=ExtractResult(url="u", title="t", content="c", fetched_at=time.time())
    )
    _runtime.state().extract_provider = stub


class TestEmptyAllowlistTierDefault:
    """Empty allowlist -> allow at personal/enterprise, deny at federal."""

    @pytest.mark.parametrize("tier", ["personal", "enterprise"])
    async def test_empty_allowlist_allows_at_personal_enterprise(self, tier: str) -> None:
        _configure_with_extract(tier=tier, allowlist=[])
        result = await capabilities.web_extract("https://example.com/page")
        assert result is not None

    @pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
    async def test_configured_allowlist_permits_matching_url(self, tier: str) -> None:
        _configure_with_extract(tier=tier, allowlist=["https://example.com/*"])
        result = await capabilities.web_extract("https://example.com/page")
        assert result is not None

    @pytest.mark.parametrize("tier", ["personal", "enterprise"])
    async def test_allowlist_blocks_non_matching_url(self, tier: str) -> None:
        # An operator who configures an allowlist opts into restriction.
        _configure_with_extract(tier=tier, allowlist=["https://allowed.example.com/*"])
        with pytest.raises(URLNotAllowed):
            await capabilities.web_extract("https://other.example.com/page")

    async def test_federal_configure_fails_on_empty_allowlist(self) -> None:
        """Federal tier rejects an empty allowlist at configure time (locked down)."""
        with pytest.raises(RuntimeError, match="url_allowlist"):
            _runtime.configure(
                config={"tier": "federal", "url_allowlist": []},
                telemetry=MagicMock(),
                agent_name="test-agent",
            )


class TestAllowlistAtPersonalTier:
    async def test_personal_empty_allowlist_allows_any_url(self) -> None:
        _configure_with_extract(tier="personal", allowlist=[])
        result = await capabilities.web_extract("https://anything.com")
        assert result is not None

    async def test_personal_wildcard_allowlist_permits_any_url(self) -> None:
        _configure_with_extract(tier="personal", allowlist=["*"])
        result = await capabilities.web_extract("https://anything.com")
        assert result is not None
