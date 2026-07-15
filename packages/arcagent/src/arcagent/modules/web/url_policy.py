"""URL allowlist policy for the web module.

Implements glob-pattern matching for outbound URL control.

Tier behaviour (stringency is tier metadata, not a universal gate — ADR-019):
    Federal    — deny by default; every URL must match at least one pattern.
                 Empty allowlist = deny all. Module startup rejects empty list.
    Enterprise — allow by default; empty allowlist = allow all. When non-empty,
                 the list becomes an allowlist (deny non-matching) and cross-org
                 URLs log a WARNING.
    Personal   — allow by default; empty allowlist = allow all. An operator MAY
                 still set an allowlist to opt into restriction.

Rationale: deny-by-default open-internet control is a FEDERAL stringency
requirement (ASI04 + LLM10). Personal/enterprise agents do ordinary research;
forcing an allowlist there bricks basic web use. The destination constraint
that matters for the lethal trifecta is enforced here, not by tagging a read
as an egress leg.

``is_url_allowed`` returns ``True`` when the URL is permitted, ``False``
when denied. Callers raise ``URLNotAllowed`` on ``False``.

Spec: SPEC-018 T4.8.5
"""

from __future__ import annotations

import fnmatch
import logging
import urllib.parse

_logger = logging.getLogger("arcagent.modules.web.url_policy")


def is_url_allowed(
    url: str,
    *,
    allowlist: list[str],
    tier: str,
) -> bool:
    """Return True if ``url`` is permitted under the given tier policy.

    Args:
        url: The outbound URL to check.
        allowlist: Glob patterns (e.g. ``["https://api.example.com/*"]``).
                   Matched against the full URL string.
        tier: Deployment tier — ``"federal"``, ``"enterprise"``, or
              ``"personal"``.

    Returns:
        True if the URL is allowed, False if it should be denied.
    """
    # Empty allowlist: federal denies (open-internet control is a federal
    # stringency requirement — startup also rejects an empty federal list);
    # personal/enterprise allow by default so ordinary research is not bricked.
    if not allowlist:
        return tier.lower() != "federal"

    if tier.lower() == "enterprise":
        _warn_cross_org_if_needed(url, allowlist)

    return _check_allowlist(url, allowlist)


def _check_allowlist(url: str, allowlist: list[str]) -> bool:
    """Return True if ``url`` matches any pattern in ``allowlist``."""
    return any(fnmatch.fnmatch(url, pattern) for pattern in allowlist)


def _warn_cross_org_if_needed(url: str, allowlist: list[str]) -> None:
    """Warn when a URL looks like it belongs to a different org.

    Best-effort heuristic for enterprise tier: compare the URL's host
    against the domain part of each allowlist pattern; warn when none match.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    # Check whether the host appears in any allowlist pattern's domain part
    for pattern in allowlist:
        pattern_host = urllib.parse.urlparse(pattern).netloc.lower()
        if pattern_host and pattern_host in host:
            return
    _logger.warning(
        "web.url_policy enterprise cross-org URL detected: %s "
        "(not in any allowlist pattern domain — verify intent)",
        host,
    )


__all__ = ["is_url_allowed"]
