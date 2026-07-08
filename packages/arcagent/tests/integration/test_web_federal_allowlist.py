"""Integration test — federal URL allowlist enforcement.

End-to-end scenario (driven through the live web_extract / web_search
capabilities + _runtime, not the deleted WebModule class):
  - federal tier with a specific URL allowlist
  - web_extract on allowed URL succeeds
  - web_extract on non-allowlisted URL raises URLNotAllowed (provider never called)
  - empty allowlist raises RuntimeError at configure time
  - URL audit events (web.url_denied) are emitted for denied requests

These tests do NOT make real network calls — providers are injected stubs.

Spec: SPEC-018 T4.8.5 / PRD Epic I2
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.web import _runtime, capabilities
from arcagent.modules.web.errors import URLNotAllowed
from arcagent.modules.web.protocols import ExtractResult, SearchHit


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _make_extract_result(url: str) -> ExtractResult:
    return ExtractResult(url=url, title="Title", content="# Content", fetched_at=time.time())


def _configure(tier: str, allowlist: list[str], telemetry: Any = None) -> Any:
    tel = telemetry if telemetry is not None else MagicMock()
    _runtime.configure(
        config={"tier": tier, "url_allowlist": allowlist},
        telemetry=tel,
        agent_name="test-agent",
    )
    return tel


class TestFederalAllowlistEnforcement:
    """Federal tier: deny-by-default; only allowlisted URLs succeed."""

    async def test_allowlisted_url_succeeds(self) -> None:
        _configure(
            "federal",
            ["https://api.nist.gov/*", "https://csrc.nist.gov/*"],
        )
        stub = MagicMock()
        stub.extract = AsyncMock(return_value=_make_extract_result("https://api.nist.gov/data"))
        _runtime.state().extract_provider = stub

        raw = await capabilities.web_extract("https://api.nist.gov/data")
        result = json.loads(raw)
        assert result["url"] == "https://api.nist.gov/data"
        stub.extract.assert_awaited_once()

    async def test_non_allowlisted_url_denied(self) -> None:
        _configure("federal", ["https://api.nist.gov/*"])
        stub = MagicMock()
        stub.extract = AsyncMock()
        _runtime.state().extract_provider = stub

        with pytest.raises(URLNotAllowed) as exc_info:
            await capabilities.web_extract("https://attacker.com/exfil")

        # Provider must NOT be called — denial happens before network.
        stub.extract.assert_not_awaited()
        assert exc_info.value.details["url"] == "https://attacker.com/exfil"
        assert exc_info.value.details["tier"] == "federal"

    async def test_empty_allowlist_raises_at_configure(self) -> None:
        with pytest.raises(RuntimeError, match="url_allowlist"):
            _configure("federal", [])

    async def test_denied_url_emits_web_url_denied_event(self) -> None:
        tel = _configure("federal", ["https://trusted.gov/*"])
        stub = MagicMock()
        stub.extract = AsyncMock()
        _runtime.state().extract_provider = stub

        with pytest.raises(URLNotAllowed):
            await capabilities.web_extract("https://denied.example.com/page")

        emitted = [c[0][0] for c in tel.audit_event.call_args_list]
        assert "web.url_denied" in emitted
        denied_call = next(
            c for c in tel.audit_event.call_args_list if c[0][0] == "web.url_denied"
        )
        event_data = denied_call[0][1]
        assert event_data["tier"] == "federal"
        assert "denied.example.com" in event_data["url"]

    async def test_glob_pattern_enforced_strictly(self) -> None:
        """URL must match the glob exactly; similar-looking URLs are denied."""
        _configure("federal", ["https://api.trusted.gov/v1/*"])
        stub = MagicMock()
        stub.extract = AsyncMock(
            return_value=_make_extract_result("https://api.trusted.gov/v1/endpoint")
        )
        _runtime.state().extract_provider = stub

        raw = await capabilities.web_extract("https://api.trusted.gov/v1/endpoint")
        assert json.loads(raw)["url"] == "https://api.trusted.gov/v1/endpoint"

        with pytest.raises(URLNotAllowed):
            await capabilities.web_extract("https://api.trusted.gov/v2/endpoint")


class TestFederalSearchAllowlist:
    """Federal tier: search queries do not need URL allowlist (search != extract)."""

    async def test_search_works_at_federal_tier(self) -> None:
        _configure("federal", ["https://api.gov/*"])
        stub = MagicMock()
        stub.search = AsyncMock(
            return_value=[SearchHit(url="https://a.com", title="A", snippet="s")]
        )
        _runtime.state().search_provider = stub

        raw = await capabilities.web_search("federal query")
        assert len(json.loads(raw)) == 1
