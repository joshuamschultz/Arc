"""Unit tests for url_policy — tier-differentiated default (ADR-019).

Policy: deny-by-default open-internet control is a FEDERAL stringency
requirement. Personal/enterprise allow by default (empty allowlist = allow all)
so ordinary research is not bricked; an operator may still set an allowlist to
opt into restriction. Federal denies by default and requires a non-empty list.

Verifies:
- Federal denies when the allowlist is empty (deny-by-default, locked down)
- Personal/enterprise ALLOW when the allowlist is empty (allow-by-default)
- A configured (non-empty) allowlist is enforced at every tier
- Non-matching URLs are denied when an allowlist is configured
"""

from __future__ import annotations

from arcagent.modules.web.url_policy import is_url_allowed


class TestFederalTier:
    """Federal tier: deny-by-default; empty allowlist = deny all (locked down)."""

    def test_empty_allowlist_denies_all(self) -> None:
        assert is_url_allowed("https://example.com/page", allowlist=[], tier="federal") is False

    def test_exact_match_allowed(self) -> None:
        allowlist = ["https://api.nist.gov/data"]
        assert (
            is_url_allowed("https://api.nist.gov/data", allowlist=allowlist, tier="federal")
            is True
        )

    def test_glob_wildcard_allowed(self) -> None:
        allowlist = ["https://api.nist.gov/*"]
        assert (
            is_url_allowed("https://api.nist.gov/data/123", allowlist=allowlist, tier="federal")
            is True
        )

    def test_glob_not_matching_denied(self) -> None:
        allowlist = ["https://api.nist.gov/*"]
        assert (
            is_url_allowed("https://evil.com/attack", allowlist=allowlist, tier="federal") is False
        )

    def test_multiple_patterns_first_match_wins(self) -> None:
        allowlist = ["https://a.gov/*", "https://b.gov/*"]
        assert is_url_allowed("https://b.gov/path", allowlist=allowlist, tier="federal") is True

    def test_partial_domain_does_not_match(self) -> None:
        allowlist = ["https://api.gov/*"]
        assert (
            is_url_allowed("https://evilapi.gov/data", allowlist=allowlist, tier="federal")
            is False
        )

    def test_glob_star_star_pattern(self) -> None:
        allowlist = ["https://*.nist.gov/*"]
        assert (
            is_url_allowed("https://sub.nist.gov/data", allowlist=allowlist, tier="federal")
            is True
        )

    def test_non_https_denied_when_not_in_allowlist(self) -> None:
        allowlist = ["https://trusted.gov/*"]
        assert (
            is_url_allowed("http://trusted.gov/insecure", allowlist=allowlist, tier="federal")
            is False
        )


class TestEnterpriseTier:
    """Enterprise tier: allow-by-default; a non-empty allowlist is enforced."""

    def test_empty_allowlist_allows_all(self) -> None:
        assert (
            is_url_allowed("https://anything.com/path", allowlist=[], tier="enterprise") is True
        )

    def test_non_empty_allowlist_enforced(self) -> None:
        allowlist = ["https://internal.corp/*"]
        assert (
            is_url_allowed("https://internal.corp/api", allowlist=allowlist, tier="enterprise")
            is True
        )
        assert (
            is_url_allowed("https://external.com/api", allowlist=allowlist, tier="enterprise")
            is False
        )

    def test_glob_patterns_work(self) -> None:
        allowlist = ["https://*.corp.internal/*"]
        assert (
            is_url_allowed(
                "https://api.corp.internal/v1/data", allowlist=allowlist, tier="enterprise"
            )
            is True
        )

    def test_non_matching_url_denied(self) -> None:
        allowlist = ["https://allowed.example.com/*"]
        assert (
            is_url_allowed("https://other.com/path", allowlist=allowlist, tier="enterprise")
            is False
        )


class TestPersonalTier:
    """Personal tier: allow-by-default; an operator may opt into restriction."""

    def test_empty_allowlist_allows_all(self) -> None:
        assert is_url_allowed("https://example.com", allowlist=[], tier="personal") is True

    def test_configured_allowlist_permits_matching_url(self) -> None:
        allowlist = ["https://only.this.com/*"]
        assert (
            is_url_allowed("https://only.this.com/path", allowlist=allowlist, tier="personal")
            is True
        )

    def test_configured_allowlist_denies_non_matching(self) -> None:
        # An operator who sets an allowlist opts into restriction — only matches pass.
        allowlist = ["https://only.this.com/*"]
        assert (
            is_url_allowed("https://other.com/path", allowlist=allowlist, tier="personal") is False
        )

    def test_open_internet_by_default(self) -> None:
        """No allowlist configured — any URL is reachable on personal."""
        for url in [
            "https://google.com",
            "http://localhost:8080",
            "https://192.168.1.1/api",
        ]:
            assert is_url_allowed(url, allowlist=[], tier="personal") is True


class TestTierCaseInsensitive:
    """Tier string matching is case-insensitive."""

    def test_federal_uppercase_empty_denies(self) -> None:
        assert is_url_allowed("https://x.com", allowlist=[], tier="FEDERAL") is False

    def test_personal_mixed_case_empty_allows(self) -> None:
        assert is_url_allowed("https://x.com", allowlist=[], tier="Personal") is True

    def test_personal_mixed_case_with_allowlist(self) -> None:
        allowlist = ["https://x.com"]
        assert is_url_allowed("https://x.com", allowlist=allowlist, tier="Personal") is True
