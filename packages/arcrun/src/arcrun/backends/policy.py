"""Backend loader policy — tier-driven access control for backend loading.

This module extracts all tier-decision logic from ``loader.py`` into pure
functions.  ``loader.py`` calls these functions; it never branches on tier
strings directly.

This is the ``policy.py`` pattern documented in
``docs/architecture/policy-modules.md``: every arcagent/arcrun module with
tier-dependent behaviour MUST extract a ``policy.py`` with pure functions;
business logic calls into policy.

Phase C signing pillar (OWASP LLM03, ASI04)
--------------------------------------------
Manifest verification is now required at ALL tiers — not just federal.
The tier-stringency knob controls *which issuers are trusted*, not *whether
to verify*.  This closes a bypass where non-federal tiers could load
unsigned third-party backends from setuptools entry-points.

Entry-points are permanently disabled at ALL tiers.  Rationale: setuptools
entry-points allow any installed package to inject a backend without explicit
operator review or signing.  There is no safe mechanism to verify an
entry-point package's integrity without a manifest.  The complexity of
"allow-but-require-signature" exceeds the benefit, and the supply-chain
risk (ASI04, LLM03) is too high.  Operators who want third-party backends
must supply a signed manifest and use dotted import paths.

Functions
---------
allow_entry_points(tier)
    Whether ``setuptools entry_points`` discovery is permitted.
    Always returns False — entry-points disabled at all tiers (Phase C).
require_manifest(tier)
    Whether a signed ``allowed_backends`` manifest is mandatory.
    Always returns True — manifests required at all tiers (Phase C).
"""

from __future__ import annotations


def allow_entry_points(tier: str) -> bool:
    """Return True if setuptools entry_points discovery is permitted.

    Phase C decision: always returns False.  Entry-points are disabled at
    ALL tiers because they permit arbitrary third-party code to register
    backends without explicit operator review and signing (OWASP LLM03,
    ASI04).  There is no safe place to obtain a verification key for an
    arbitrary entry-point package.  Operators who want third-party backends
    must supply a signed manifest and use dotted import paths.

    Args:
        tier: Deployment tier string (unused — kept for API stability).

    Returns:
        Always False.
    """
    # Tier parameter retained for API stability; entry-points are denied
    # unconditionally at all tiers (Phase C supply-chain lockdown).
    _ = tier
    return False


def require_manifest(tier: str) -> bool:
    """Return True if a signed ``allowed_backends`` manifest is mandatory.

    Phase C decision: always returns True.  Manifest verification is
    required at ALL tiers — not just federal.  The tier-stringency knob
    now controls which *issuers* are trusted, not whether to verify.

    At federal tier only operator-signed manifests are accepted.
    At enterprise and personal tiers operator OR self-signed manifests are
    accepted, but a manifest is still required for any non-builtin backend.

    Args:
        tier: Deployment tier string (unused — kept for API stability).

    Returns:
        Always True.
    """
    # Tier parameter retained for API stability; manifests are required
    # unconditionally at all tiers (Phase C sign pillar).
    _ = tier
    return True


__all__ = [
    "allow_entry_points",
    "require_manifest",
]
