"""Integration test: federal tier + local config → LocalBrowserNotAllowed.

Covers PRD §Epic I3 AC2: "Sandbox mode `strict` forces BROWSERBASE_REMOTE
config; local headless disabled."

Gate G4 equivalent — verifies that the policy layer correctly rejects
any attempt to use a local headless browser at federal tier, regardless
of what the PlaywrightConfig says.
"""

from __future__ import annotations

import pytest

from arcagent.modules.browser.config import PlaywrightConfig
from arcagent.modules.browser.errors import LocalBrowserNotAllowed
from arcagent.modules.browser.policy import enforce_sandbox_policy


class TestFederalTierLocalBrowserForbidden:
    """Federal tier + local mode → LocalBrowserNotAllowed regardless of config."""

    def test_federal_local_loose_raises(self) -> None:
        """Federal overrides loose config to strict; local still forbidden."""
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("federal", cfg)

        err = exc_info.value
        assert err.code == "BROWSER_LOCAL_NOT_ALLOWED"
        assert err.details["tier"] == "federal"

    def test_federal_local_strict_raises(self) -> None:
        """Federal + strict config + local mode → raises."""
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        with pytest.raises(LocalBrowserNotAllowed):
            enforce_sandbox_policy("federal", cfg)

    def test_federal_remote_loose_config_allowed(self) -> None:
        """Federal + remote mode → allowed even with loose config (remote is safe)."""
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="loose",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com?apiKey=test",
        )
        enforce_sandbox_policy("federal", cfg)  # must not raise

    def test_federal_remote_strict_config_allowed(self) -> None:
        """Federal + remote + strict → allowed."""
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="strict",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com?apiKey=test",
        )
        enforce_sandbox_policy("federal", cfg)  # must not raise

    def test_error_contains_actionable_message(self) -> None:
        """Error message tells the operator what to configure."""
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("federal", cfg)
        assert "remote" in exc_info.value.message.lower()
        assert "remote_provider" in exc_info.value.message or "endpoint" in exc_info.value.message

    def test_error_component_is_browser(self) -> None:
        """Error component is 'browser' for consistent audit trail routing."""
        cfg = PlaywrightConfig(mode="local")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("federal", cfg)
        assert exc_info.value.component == "browser"


class TestNonFederalLocalBrowserAllowed:
    """Non-federal loose tier allows local headless."""

    def test_enterprise_loose_local_allowed(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        enforce_sandbox_policy("enterprise", cfg)  # must not raise

    def test_personal_loose_local_allowed(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        enforce_sandbox_policy("personal", cfg)  # must not raise
