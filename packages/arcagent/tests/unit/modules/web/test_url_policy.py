"""Unit tests for url_policy — deny-by-default at all tiers.

Policy: empty allowlist = deny all URLs at every tier (ASI04 + LLM10).
Operators must explicitly configure which destinations are allowed.

Verifies:
- All tiers deny when allowlist is empty (deny-by-default)
- Federal tier allows URLs matching glob patterns
- Enterprise tier allows URLs matching glob patterns
- Personal tier allows URLs matching glob patterns
- Non-matching URLs are denied at all tiers
- Wildcard (*) opens all traffic at personal when configured
"""

from __future__ import annotations

import pytest

from arcagent.modules.web.url_policy import is_url_allowed


class TestFederalTier:
    """Federal tier: deny-by-default; empty allowlist = deny all."""

    def test_empty_allowlist_denies_all(self) -> None:
        assert is_url_allowed("https://example.com/page", allowlist=[], tier="federal") is False

    def test_exact_match_allowed(self) -> None:
        allowlist = ["https://api.nist.gov/data"]
        assert is_url_allowed("https://api.nist.gov/data", allowlist=allowlist, tier="federal") is True

    def test_glob_wildcard_allowed(self) -> None:
        allowlist = ["https://api.nist.gov/*"]
        assert is_url_allowed("https://api.nist.gov/data/123", allowlist=allowlist, tier="federal") is True

    def test_glob_not_matching_denied(self) -> None:
        allowlist = ["https://api.nist.gov/*"]
        assert is_url_allowed("https://evil.com/attack", allowlist=allowlist, tier="federal") is False

    def test_multiple_patterns_first_match_wins(self) -> None:
        allowlist = ["https://a.gov/*", "https://b.gov/*"]
        assert is_url_allowed("https://b.gov/path", allowlist=allowlist, tier="federal") is True

    def test_partial_domain_does_not_match(self) -> None:
        allowlist = ["https://api.gov/*"]
        assert is_url_allowed("https://evilapi.gov/data", allowlist=allowlist, tier="federal") is False

    def test_glob_star_star_pattern(self) -> None:
        allowlist = ["https://*.nist.gov/*"]
        assert is_url_allowed("https://sub.nist.gov/data", allowlist=allowlist, tier="federal") is True

    def test_non_https_denied_when_not_in_allowlist(self) -> None:
        allowlist = ["https://trusted.gov/*"]
        assert is_url_allowed("http://trusted.gov/insecure", allowlist=allowlist, tier="federal") is False


class TestEnterpriseTier:
    """Enterprise tier: deny-by-default; empty allowlist = deny all."""

    def test_empty_allowlist_denies_all(self) -> None:
        # Changed: empty allowlist now denies all at enterprise (ASI04 + LLM10)
        assert is_url_allowed("https://anything.com/path", allowlist=[], tier="enterprise") is False

    def test_non_empty_allowlist_enforced(self) -> None:
        allowlist = ["https://internal.corp/*"]
        assert is_url_allowed("https://internal.corp/api", allowlist=allowlist, tier="enterprise") is True
        assert is_url_allowed("https://external.com/api", allowlist=allowlist, tier="enterprise") is False

    def test_glob_patterns_work(self) -> None:
        allowlist = ["https://*.corp.internal/*"]
        assert is_url_allowed("https://api.corp.internal/v1/data", allowlist=allowlist, tier="enterprise") is True

    def test_non_matching_url_denied(self) -> None:
        allowlist = ["https://allowed.example.com/*"]
        assert is_url_allowed("https://other.com/path", allowlist=allowlist, tier="enterprise") is False


class TestPersonalTier:
    """Personal tier: deny-by-default; explicit allowlist required."""

    def test_empty_allowlist_denies_all(self) -> None:
        # Changed: personal tier now denies with empty allowlist (ASI04 + LLM10)
        assert is_url_allowed("https://example.com", allowlist=[], tier="personal") is False

    def test_configured_allowlist_permits_matching_url(self) -> None:
        allowlist = ["https://only.this.com/*"]
        assert is_url_allowed("https://only.this.com/path", allowlist=allowlist, tier="personal") is True

    def test_configured_allowlist_denies_non_matching(self) -> None:
        # With an allowlist, only matching URLs pass
        allowlist = ["https://only.this.com/*"]
        assert is_url_allowed("https://other.com/path", allowlist=allowlist, tier="personal") is False

    def test_explicit_wildcard_allows_any_url(self) -> None:
        """Operator can opt in to open internet with explicit wildcard."""
        allowlist = ["*"]
        for url in [
            "https://google.com",
            "http://localhost:8080",
            "https://192.168.1.1/api",
        ]:
            assert is_url_allowed(url, allowlist=allowlist, tier="personal") is True


class TestTierCaseInsensitive:
    """Tier string matching is case-insensitive."""

    def test_federal_uppercase(self) -> None:
        assert is_url_allowed("https://x.com", allowlist=[], tier="FEDERAL") is False

    def test_personal_mixed_case_empty_allowlist_denies(self) -> None:
        # Changed: deny-by-default applies regardless of case
        assert is_url_allowed("https://x.com", allowlist=[], tier="Personal") is False

    def test_personal_mixed_case_with_allowlist(self) -> None:
        allowlist = ["https://x.com"]
        assert is_url_allowed("https://x.com", allowlist=allowlist, tier="Personal") is True
