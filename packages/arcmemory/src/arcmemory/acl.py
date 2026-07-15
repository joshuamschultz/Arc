"""Cross-session access control — arcmemory's own memory-visibility policy.

This is memory-implementation policy, so it lives with the backend, not the agent
framework: arcagent only asks the configured Brain provider to authorize an operation
(a generic seam), and arcmemory answers with this engine.

The ACL is stored as YAML frontmatter in session or user-profile files. The model is
intentionally minimal: one field governs all cross-session visibility so operators can
reason about it without consulting code.

Example frontmatter::

    ---
    acl:
      cross_session_visibility: private
    ---

Default (no frontmatter) is determined by tier configuration. Federal defaults to the
most restrictive value (``private``) per NIST 800-53 AC-3 least-privilege and CMMC.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel

_logger = logging.getLogger("arcmemory.acl")

# Type alias for the three visibility levels.
CrossSessionVisibility = Literal[
    "private",
    "shared-with-agent",
    "shared-with-others-via-agent",
]

# The three visibility levels.
_VALID_VISIBILITY = {"private", "shared-with-agent", "shared-with-others-via-agent"}

# Regex that extracts the first YAML frontmatter block from markdown content.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class ACLViolation(Exception):  # noqa: N818
    """Raised when a memory operation violates the session ACL.

    Uses domain terminology rather than the ``*Error`` suffix convention because it
    signals a specific authorization condition, not a programming error.
    """

    def __init__(self, reason: str, caller_did: str = "", target_did: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.caller_did = caller_did
        self.target_did = target_did


class MemoryACLConfig(BaseModel):
    """Tier-driven defaults for cross-session visibility.

    Federal tier default is the most restrictive (private); enterprise and personal
    default to shared-with-agent.
    """

    tier: Literal["federal", "enterprise", "personal"] = "personal"

    federal_default: CrossSessionVisibility = "private"
    enterprise_default: CrossSessionVisibility = "shared-with-agent"
    personal_default: CrossSessionVisibility = "shared-with-agent"

    def default_for_tier(self) -> CrossSessionVisibility:
        """Return the default visibility for the configured tier."""
        if self.tier == "federal":
            return self.federal_default
        if self.tier == "enterprise":
            return self.enterprise_default
        return self.personal_default

    model_config = {"frozen": True, "extra": "forbid"}


def _coerce_visibility(raw: str, config: MemoryACLConfig) -> CrossSessionVisibility:
    """Validate a raw visibility string, falling back to the tier default.

    The single place that decides how an unknown or adversarial visibility string is
    resolved: reject it and use the configured tier default (``private`` for federal).
    Logs genuinely unknown values so the fallback is observable on every parse path.
    """
    if raw in _VALID_VISIBILITY:
        return raw  # type: ignore[return-value]  # reason: raw ∈ _VALID_VISIBILITY == the Literal members; mypy can't narrow str to Literal
    if raw:
        _logger.warning("Unknown cross_session_visibility '%s'; using tier default", raw)
    return config.default_for_tier()


class SessionACL(BaseModel):
    """Access control list for a session.

    A single ``cross_session_visibility`` field controls whether memory from this session
    is readable by other sessions/callers:

    - private: only the owning user/agent may read this session's memory
    - shared-with-agent: any session sharing the same agent_did may read
    - shared-with-others-via-agent: the agent may proxy reads to other users
    """

    owner_did: str = ""
    cross_session_visibility: CrossSessionVisibility = "private"
    classification: Literal["unclassified", "cui", "secret"] = "unclassified"

    model_config = {"frozen": True}

    def allows_read_by(self, caller_did: str, agent_did: str) -> bool:
        """Return True if ``caller_did`` may read memory from this session."""
        if caller_did == self.owner_did:
            return True  # Owner always has access

        if self.cross_session_visibility == "private":
            return False

        if self.cross_session_visibility == "shared-with-agent":
            return caller_did == agent_did

        if self.cross_session_visibility == "shared-with-others-via-agent":
            # Agent acts as proxy; agent itself may read on behalf of others
            return caller_did == agent_did

        return False  # Unknown visibility — deny

    @classmethod
    def from_frontmatter(
        cls,
        content: str,
        config: MemoryACLConfig,
        owner_did: str = "",
    ) -> SessionACL:
        """Parse a SessionACL from YAML frontmatter in markdown content.

        If no frontmatter is found, the ``acl`` section is absent, the visibility value
        is unknown, or parsing raises, falls back to ``config.default_for_tier()``. For
        the federal tier that default is ``private`` — the most restrictive value. Never
        raises.
        """
        try:
            return cls._parse_frontmatter(content, config, owner_did)
        except Exception:  # reason: never raise — fall back to the tier default
            _logger.warning("Failed to parse ACL frontmatter; using tier default", exc_info=True)
            return cls(
                owner_did=owner_did,
                cross_session_visibility=config.default_for_tier(),
            )

    @classmethod
    def _parse_frontmatter(
        cls,
        content: str,
        config: MemoryACLConfig,
        owner_did: str,
    ) -> SessionACL:
        """Internal YAML frontmatter parser."""
        match = _FRONTMATTER_RE.match(content)
        if not match:
            return cls(
                owner_did=owner_did,
                cross_session_visibility=config.default_for_tier(),
            )

        frontmatter_text = match.group(1)
        parsed: dict[str, Any] = _parse_simple_yaml(frontmatter_text)

        acl_data = parsed.get("acl", {})
        if not isinstance(acl_data, dict):
            return cls(
                owner_did=owner_did,
                cross_session_visibility=config.default_for_tier(),
            )

        visibility = _coerce_visibility(acl_data.get("cross_session_visibility", ""), config)

        classification_raw: str = parsed.get("classification", "unclassified")
        valid_cls = {"unclassified", "cui", "secret"}
        if classification_raw not in valid_cls:
            classification_raw = "unclassified"

        parsed_owner: str = parsed.get("owner_did", owner_did) or owner_did

        classification: Literal["unclassified", "cui", "secret"] = classification_raw  # type: ignore[assignment]  # reason: classification_raw validated against valid_cls above; mypy can't narrow str to Literal

        return cls(
            owner_did=parsed_owner,
            cross_session_visibility=visibility,
            classification=classification,
        )

    @classmethod
    def default(cls, config: MemoryACLConfig, owner_did: str = "") -> SessionACL:
        """Return the tier-default ACL for a session with no frontmatter."""
        return cls(
            owner_did=owner_did,
            cross_session_visibility=config.default_for_tier(),
        )


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse flat/nested ``key: value`` frontmatter via PyYAML; empty dict on failure."""
    try:
        import yaml

        return yaml.safe_load(text) or {}
    except Exception:  # reason: never raise — empty dict makes the caller use the tier default
        _logger.debug("YAML parse failed, returning empty dict", exc_info=True)
        return {}


def extract_acl_from_session_data(
    session_data: dict[str, Any],
    config: MemoryACLConfig,
    owner_did: str = "",
) -> SessionACL:
    """Extract a SessionACL from a session-metadata dict (e.g. a JSONL session record)."""
    acl_raw = session_data.get("acl", {})
    if not isinstance(acl_raw, dict):
        return SessionACL.default(config, owner_did)

    return SessionACL(
        owner_did=owner_did,
        cross_session_visibility=_coerce_visibility(
            acl_raw.get("cross_session_visibility", ""), config
        ),
    )


__all__ = [
    "ACLViolation",
    "CrossSessionVisibility",
    "MemoryACLConfig",
    "SessionACL",
    "extract_acl_from_session_data",
]
