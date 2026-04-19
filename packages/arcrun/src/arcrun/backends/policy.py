"""Backend loader policy — tier-driven access control for backend loading.

This module extracts all tier-decision logic from ``loader.py`` into pure
functions.  ``loader.py`` calls these functions; it never branches on tier
strings directly.

This is the ``policy.py`` pattern documented in
``docs/architecture/policy-modules.md``: every arcagent/arcrun module with
tier-dependent behaviour MUST extract a ``policy.py`` with pure functions;
business logic calls into policy.

Functions
---------
allow_entry_points(tier)
    Whether ``setuptools entry_points`` discovery is permitted.
require_manifest(tier)
    Whether a signed ``allowed_backends`` manifest is mandatory.
"""

from __future__ import annotations


def allow_entry_points(tier: str) -> bool:
    """Return True if setuptools entry_points discovery is permitted.

    Entry-points are disabled at federal tier because they allow arbitrary
    third-party code to register backends without explicit operator review
    and signing (OWASP LLM03, ASI04).

    Args:
        tier: Deployment tier string (``"federal"`` / ``"enterprise"`` / ``"personal"``).

    Returns:
        True when entry_points resolution is allowed; False at federal tier.
    """
    return tier != "federal"


def require_manifest(tier: str) -> bool:
    """Return True if a signed ``allowed_backends`` manifest is mandatory.

    At federal tier the manifest is the ONLY mechanism for authorising
    non-built-in backends.  Unsigned ``allowed_backends`` dicts are never
    accepted (fail-closed per SPEC-018 HIGH-3).

    Args:
        tier: Deployment tier string.

    Returns:
        True at federal tier; False otherwise.
    """
    return tier == "federal"


__all__ = [
    "allow_entry_points",
    "require_manifest",
]
