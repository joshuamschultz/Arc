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

import logging
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from arcagent.utils.url_security import UnsafeURLError, validate_http_url

_logger = logging.getLogger("arcagent.modules.web.url_policy")


def is_url_allowed(
    url: str,
    *,
    allowlist: list[str],
    tier: str,
    resolve: bool = False,
    resolver: Callable[[str], Iterable[str]] | None = None,
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
    try:
        validated = validate_http_url(
            url,
            resolve=resolve,
            **({"resolver": resolver} if resolver is not None else {}),
        )
    except UnsafeURLError:
        return False

    # Empty allowlist: federal denies (open-internet control is a federal
    # stringency requirement — startup also rejects an empty federal list);
    # personal/enterprise allow by default so ordinary research is not bricked.
    if not allowlist:
        return tier.lower() != "federal"

    if tier.lower() == "enterprise":
        _warn_cross_org_if_needed(url, allowlist)

    return _check_allowlist(validated.raw, allowlist)


def _check_allowlist(url: str, allowlist: list[str]) -> bool:
    """Return True if ``url`` matches any pattern in ``allowlist``."""
    candidate = urlsplit(url)
    candidate_host = (candidate.hostname or "").rstrip(".").lower()
    for pattern in allowlist:
        if pattern == "*":
            return True
        parsed_pattern = urlsplit(pattern)
        pattern_host = (parsed_pattern.hostname or "").rstrip(".").lower()
        if candidate.scheme.lower() != parsed_pattern.scheme.lower():
            continue
        if pattern_host.startswith("*."):
            suffix = pattern_host[2:]
            host_matches = candidate_host != suffix and candidate_host.endswith(f".{suffix}")
        else:
            host_matches = candidate_host == pattern_host
        if not host_matches or candidate.port != parsed_pattern.port:
            continue
        pattern_path = parsed_pattern.path
        if pattern_path.endswith("*"):
            if candidate.path.startswith(pattern_path[:-1]):
                return True
        elif candidate.path == pattern_path:
            return True
    return False


def _warn_cross_org_if_needed(url: str, allowlist: list[str]) -> None:
    """Warn when a URL looks like it belongs to a different org.

    Best-effort heuristic for enterprise tier: compare the URL's host
    against the domain part of each allowlist pattern; warn when none match.
    """
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    # Check whether the host appears in any allowlist pattern's domain part
    for pattern in allowlist:
        pattern_host = urlsplit(pattern).hostname or ""
        pattern_host = pattern_host.removeprefix("*.").lower()
        if pattern_host and (host == pattern_host or host.endswith(f".{pattern_host}")):
            return
    _logger.warning(
        "web.url_policy enterprise cross-org URL detected: %s "
        "(not in any allowlist pattern domain — verify intent)",
        host,
    )


__all__ = ["is_url_allowed"]
