"""Tests for browser sandbox policy enforcement (T4.9).

Critical invariants:
- Federal tier ALWAYS enforces strict sandbox, even if config says loose
- strict sandbox + local mode → LocalBrowserNotAllowed
- loose sandbox + local mode → allowed
- strict sandbox + remote mode → allowed
"""

from __future__ import annotations

import pytest

from arcagent.modules.browser.config import PlaywrightConfig
from arcagent.modules.browser.errors import LocalBrowserNotAllowed
from arcagent.modules.browser.policy import effective_sandbox, enforce_sandbox_policy


class TestEffectiveSandbox:
    """effective_sandbox() returns the policy-adjusted sandbox mode."""

    def test_federal_always_strict_regardless_of_config(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        assert effective_sandbox("federal", cfg) == "strict"

    def test_federal_strict_config_stays_strict(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        assert effective_sandbox("federal", cfg) == "strict"

    def test_enterprise_loose_config_stays_loose(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        assert effective_sandbox("enterprise", cfg) == "loose"

    def test_enterprise_strict_config_stays_strict(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        assert effective_sandbox("enterprise", cfg) == "strict"

    def test_personal_loose_config_stays_loose(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        assert effective_sandbox("personal", cfg) == "loose"

    def test_personal_strict_config_stays_strict(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        assert effective_sandbox("personal", cfg) == "strict"


class TestEnforceSandboxPolicyFederal:
    """Federal tier: local mode MUST raise LocalBrowserNotAllowed."""

    def test_federal_local_mode_raises(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("federal", cfg)
        assert exc_info.value.details["tier"] == "federal"

    def test_federal_local_mode_with_strict_config_raises(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        with pytest.raises(LocalBrowserNotAllowed):
            enforce_sandbox_policy("federal", cfg)

    def test_federal_remote_mode_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="loose",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com",
        )
        enforce_sandbox_policy("federal", cfg)  # must not raise

    def test_federal_remote_mode_strict_config_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="strict",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com",
        )
        enforce_sandbox_policy("federal", cfg)  # must not raise


class TestEnforceSandboxPolicyEnterprise:
    """Enterprise tier: raises only when sandbox=strict + mode=local."""

    def test_enterprise_loose_local_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        enforce_sandbox_policy("enterprise", cfg)

    def test_enterprise_strict_local_raises(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("enterprise", cfg)
        assert exc_info.value.details["tier"] == "enterprise"

    def test_enterprise_strict_remote_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="strict",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com",
        )
        enforce_sandbox_policy("enterprise", cfg)

    def test_enterprise_loose_remote_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="loose",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com",
        )
        enforce_sandbox_policy("enterprise", cfg)


class TestEnforceSandboxPolicyPersonal:
    """Personal tier: local is always OK unless explicitly strict."""

    def test_personal_loose_local_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        enforce_sandbox_policy("personal", cfg)

    def test_personal_strict_local_raises(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="strict")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("personal", cfg)
        assert exc_info.value.details["tier"] == "personal"

    def test_personal_loose_remote_does_not_raise(self) -> None:
        cfg = PlaywrightConfig(
            mode="remote",
            sandbox="loose",
            remote_provider="browserbase",
            remote_endpoint="wss://connect.browserbase.com",
        )
        enforce_sandbox_policy("personal", cfg)


class TestLocalBrowserNotAllowedErrorDetails:
    """LocalBrowserNotAllowed carries useful diagnostic context."""

    def test_error_carries_tier_and_mode(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("federal", cfg)
        err = exc_info.value
        assert err.details["tier"] == "federal"
        assert err.details["mode"] == "local"
        assert err.details["sandbox"] == "strict"

    def test_error_code_is_correct(self) -> None:
        cfg = PlaywrightConfig(mode="local", sandbox="loose")
        with pytest.raises(LocalBrowserNotAllowed) as exc_info:
            enforce_sandbox_policy("federal", cfg)
        assert exc_info.value.code == "BROWSER_LOCAL_NOT_ALLOWED"
