"""Unit tests for url_policy — federal allowlist enforcement.

Verifies:
- Federal tier denies all URLs when allowlist is empty
- Federal tier allows URLs matching glob patterns
- Federal tier denies URLs not matching any pattern
- Enterprise tier allows all when allowlist is empty
- Enterprise tier enforces allowlist when non-empty
- Personal tier always allows regardless of allowlist
- Glob patterns (wildcards) match correctly
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
        # "api.gov" prefix must be an exact glob match; "evilapi.gov" must not pass
        assert is_url_allowed("https://evilapi.gov/data", allowlist=allowlist, tier="federal") is False

    def test_glob_star_star_pattern(self) -> None:
        allowlist = ["https://*.nist.gov/*"]
        assert is_url_allowed("https://sub.nist.gov/data", allowlist=allowlist, tier="federal") is True

    def test_non_https_denied_when_not_in_allowlist(self) -> None:
        allowlist = ["https://trusted.gov/*"]
        assert is_url_allowed("http://trusted.gov/insecure", allowlist=allowlist, tier="federal") is False


class TestEnterpriseTier:
    """Enterprise tier: allow all when allowlist is empty; enforce when non-empty."""

    def test_empty_allowlist_allows_all(self) -> None:
        assert is_url_allowed("https://anything.com/path", allowlist=[], tier="enterprise") is True

    def test_non_empty_allowlist_enforced(self) -> None:
        allowlist = ["https://internal.corp/*"]
        assert is_url_allowed("https://internal.corp/api", allowlist=allowlist, tier="enterprise") is True
        assert is_url_allowed("https://external.com/api", allowlist=allowlist, tier="enterprise") is False

    def test_glob_patterns_work(self) -> None:
        allowlist = ["https://*.corp.internal/*"]
        assert is_url_allowed("https://api.corp.internal/v1/data", allowlist=allowlist, tier="enterprise") is True


class TestPersonalTier:
    """Personal tier: no URL restrictions regardless of allowlist."""

    def test_empty_allowlist_allows_all(self) -> None:
        assert is_url_allowed("https://example.com", allowlist=[], tier="personal") is True

    def test_non_matching_allowlist_still_allows(self) -> None:
        allowlist = ["https://only.this.com/*"]
        assert is_url_allowed("https://other.com/path", allowlist=allowlist, tier="personal") is True

    def test_any_url_allowed(self) -> None:
        for url in [
            "https://google.com",
            "http://localhost:8080",
            "https://192.168.1.1/api",
        ]:
            assert is_url_allowed(url, allowlist=[], tier="personal") is True


class TestTierCaseInsensitive:
    """Tier string matching is case-insensitive."""

    def test_federal_uppercase(self) -> None:
        assert is_url_allowed("https://x.com", allowlist=[], tier="FEDERAL") is False

    def test_personal_mixed_case(self) -> None:
        assert is_url_allowed("https://x.com", allowlist=[], tier="Personal") is True
