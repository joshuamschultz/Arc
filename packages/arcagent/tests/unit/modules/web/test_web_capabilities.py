"""Unit tests for the web capability surface (the live @tool path).

Covers:
- WebConfig validation and defaults.
- Provider construction wiring: web_search / web_extract build their provider
  lazily via _runtime on the real capability path (regression: providers used
  to be None on the live path, so both tools always raised).
- Missing API key surfaces ProviderConfigMissing.
- Content truncation at max_content_bytes.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from arcagent.modules.web import _runtime, capabilities
from arcagent.modules.web.capabilities import _truncate_content
from arcagent.modules.web.config import WebConfig
from arcagent.modules.web.errors import ProviderConfigMissing
from arcagent.modules.web.protocols import ExtractResult, SearchHit


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(
    tier: str = "personal",
    *,
    allowlist: list[str] | None = None,
    pii: bool = True,
    search_provider: str = "tavily",
    extract_provider: str = "firecrawl",
    max_content_bytes: int | None = None,
    telemetry: Any = None,
) -> None:
    cfg: dict[str, Any] = {
        "tier": tier,
        "pii_redaction_enabled": pii,
        "search_provider": search_provider,
        "extract_provider": extract_provider,
    }
    if allowlist is not None:
        cfg["url_allowlist"] = allowlist
    if max_content_bytes is not None:
        cfg["max_content_bytes"] = max_content_bytes
    _runtime.configure(config=cfg, telemetry=telemetry, agent_name="test-agent")


# ---------------------------------------------------------------------------
# WebConfig
# ---------------------------------------------------------------------------


class TestWebConfig:
    def test_defaults(self) -> None:
        cfg = WebConfig()
        assert cfg.search_provider == "tavily"
        assert cfg.extract_provider == "firecrawl"
        assert cfg.tier == "personal"
        assert cfg.url_allowlist == []
        assert cfg.max_content_bytes == 1_000_000
        # OFF by default at personal/enterprise; federal forces it on at the
        # call site regardless of this flag.
        assert cfg.pii_redaction_enabled is False

    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WebConfig(search_provider="not-a-provider")

    def test_max_content_bytes_floor(self) -> None:
        with pytest.raises(ValidationError):
            WebConfig(max_content_bytes=1)


# ---------------------------------------------------------------------------
# Provider construction wiring (the real capability path)
# ---------------------------------------------------------------------------


class TestProviderConstructionWiring:
    """web_search / web_extract must build a provider on the live path."""

    async def test_web_search_builds_provider_and_returns_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: configure -> lazy build -> provider.search -> JSON."""
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")

        stub = MagicMock()
        stub.search = AsyncMock(
            return_value=[SearchHit(url="https://a.com", title="A", snippet="s")]
        )

        built_with: dict[str, Any] = {}

        def _fake_make_provider(name: str, api_key: str, timeout_s: float) -> Any:
            built_with["name"] = name
            built_with["api_key"] = api_key
            return stub

        monkeypatch.setattr(_runtime, "_make_provider", _fake_make_provider)

        _configure(tier="personal", allowlist=["*"], search_provider="tavily")

        raw = await capabilities.web_search("hello")
        results = json.loads(raw)

        assert results[0]["url"] == "https://a.com"
        stub.search.assert_awaited_once()
        # The provider was actually constructed from the resolved key.
        assert built_with["name"] == "tavily"
        assert built_with["api_key"] == "test-key"

    async def test_provider_built_once_and_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        stub = MagicMock()
        stub.search = AsyncMock(return_value=[])
        make_calls = 0

        def _fake_make_provider(name: str, api_key: str, timeout_s: float) -> Any:
            nonlocal make_calls
            make_calls += 1
            return stub

        monkeypatch.setattr(_runtime, "_make_provider", _fake_make_provider)
        _configure(tier="personal", allowlist=["*"], search_provider="tavily")

        await capabilities.web_search("one")
        await capabilities.web_search("two")

        assert make_calls == 1

    async def test_missing_api_key_raises_provider_config_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        _configure(tier="personal", allowlist=["*"], search_provider="tavily")

        with pytest.raises(ProviderConfigMissing):
            await capabilities.web_search("hello")


# ---------------------------------------------------------------------------
# Content truncation
# ---------------------------------------------------------------------------


class TestContentTruncation:
    def test_truncate_shrinks_oversized_content(self) -> None:
        content = "x" * 100
        out = _truncate_content(content, 10, "https://a.com")
        assert len(out.encode("utf-8")) == 10

    def test_truncate_leaves_small_content_unchanged(self) -> None:
        content = "small"
        assert _truncate_content(content, 1024, "https://a.com") == content

    async def test_extract_applies_size_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = MagicMock()
        stub.extract = AsyncMock(
            return_value=ExtractResult(
                url="https://a.com",
                title="T",
                content="y" * 5000,
                fetched_at=time.time(),
            )
        )
        _configure(tier="personal", allowlist=["*"], pii=False, max_content_bytes=1024)
        _runtime.state().extract_provider = stub

        raw = await capabilities.web_extract("https://a.com")
        result = json.loads(raw)
        assert len(result["content"].encode("utf-8")) == 1024
