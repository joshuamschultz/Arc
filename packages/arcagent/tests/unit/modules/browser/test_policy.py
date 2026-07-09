"""Tests for the browser federal remote-browser policy.

Critical invariants:
- Federal tier + local (empty cdp_url) → LocalBrowserNotAllowedError
- Federal tier + remote cdp_url → allowed
- Non-federal tiers → local always allowed
"""

from __future__ import annotations

import pytest

from arcagent.modules.browser.config import BrowserConnectionConfig
from arcagent.modules.browser.errors import LocalBrowserNotAllowedError
from arcagent.modules.browser.policy import enforce_sandbox_policy

_REMOTE = "ws://remote-browser.internal:9222/devtools/browser/abc"


class TestEnforceSandboxPolicyFederal:
    """Federal tier: a local auto-launched browser MUST raise."""

    def test_federal_local_raises(self) -> None:
        conn = BrowserConnectionConfig()  # empty cdp_url → local launch
        with pytest.raises(LocalBrowserNotAllowedError) as exc_info:
            enforce_sandbox_policy("federal", conn)
        assert exc_info.value.details["tier"] == "federal"

    def test_federal_remote_does_not_raise(self) -> None:
        conn = BrowserConnectionConfig(cdp_url=_REMOTE)
        enforce_sandbox_policy("federal", conn)  # must not raise


class TestEnforceSandboxPolicyNonFederal:
    """Non-federal tiers allow local browsers."""

    def test_enterprise_local_does_not_raise(self) -> None:
        enforce_sandbox_policy("enterprise", BrowserConnectionConfig())

    def test_personal_local_does_not_raise(self) -> None:
        enforce_sandbox_policy("personal", BrowserConnectionConfig())

    def test_enterprise_remote_does_not_raise(self) -> None:
        enforce_sandbox_policy("enterprise", BrowserConnectionConfig(cdp_url=_REMOTE))


class TestLocalBrowserNotAllowedErrorDetails:
    """The error carries useful diagnostic context."""

    def test_error_carries_tier(self) -> None:
        with pytest.raises(LocalBrowserNotAllowedError) as exc_info:
            enforce_sandbox_policy("federal", BrowserConnectionConfig())
        assert exc_info.value.details["tier"] == "federal"

    def test_error_code_is_correct(self) -> None:
        with pytest.raises(LocalBrowserNotAllowedError) as exc_info:
            enforce_sandbox_policy("federal", BrowserConnectionConfig())
        assert exc_info.value.code == "BROWSER_LOCAL_NOT_ALLOWED"

    def test_error_message_is_actionable(self) -> None:
        with pytest.raises(LocalBrowserNotAllowedError) as exc_info:
            enforce_sandbox_policy("federal", BrowserConnectionConfig())
        msg = exc_info.value.message.lower()
        assert "remote" in msg
        assert "endpoint" in msg
