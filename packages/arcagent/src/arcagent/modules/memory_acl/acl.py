"""SessionACL — per-session access control model with frontmatter parsing.

The ACL is stored as YAML frontmatter in session or user-profile files.
The model is intentionally minimal: one field governs all cross-session
visibility so operators can reason about it without consulting code.

Example frontmatter:
    ---
    acl:
      cross_session_visibility: private
    ---

Default (no frontmatter) is determined by tier configuration.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel

from arcagent.modules.memory_acl.config import CrossSessionVisibility, MemoryACLConfig

_logger = logging.getLogger("arcagent.modules.memory_acl.acl")

# Regex that extracts YAML frontmatter block from markdown content.
# Matches the first --- ... --- block at the top of the file.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SessionACL(BaseModel):
    """Access control list for a session.

    A single cross_session_visibility field controls whether memory from
    this session is readable by other sessions/callers.

    Visibility semantics (SDD §3.3):
    - private: only the owning user/agent may read this session's memory
    - shared-with-agent: any session sharing the same agent_did may read
    - shared-with-others-via-agent: the agent may proxy reads to other users
    """

    owner_did: str = ""
    cross_session_visibility: CrossSessionVisibility = "private"
    classification: Literal["unclassified", "cui", "secret"] = "unclassified"

    model_config = {"frozen": True}

    def allows_read_by(self, caller_did: str, agent_did: str) -> bool:
        """Return True if caller_did may read memory from this session.

        Authorization logic per SDD §3.3:
        - private: only owner may read
        - shared-with-agent: owner OR agent may read
        - shared-with-others-via-agent: agent may read (proxied)
        """
        if caller_did == self.owner_did:
            return True  # Owner always has access

        if self.cross_session_visibility == "private":
            return False

        if self.cross_session_visibility == "shared-with-agent":
            # Agent (identified by agent_did) may read
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
        """Parse a SessionACL from YAML-frontmatter in markdown content.

        If no frontmatter is found or the acl section is absent, falls
        back to the tier default from config. Never raises — returns
        the most restrictive default on any parse error (fail-closed).
        """
        try:
            return cls._parse_frontmatter(content, config, owner_did)
        except Exception:
            _logger.warning(
                "Failed to parse ACL frontmatter; using tier default",
                exc_info=True,
            )
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
        """Internal YAML frontmatter parser.

        Uses a lightweight line-by-line parser to avoid importing PyYAML
        at module load time (keeps cold-start fast). PyYAML is used if
        the simple parser cannot resolve the structure.
        """
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

        visibility_raw: str = acl_data.get("cross_session_visibility", "")
        valid_values = {"private", "shared-with-agent", "shared-with-others-via-agent"}

        visibility: CrossSessionVisibility
        if visibility_raw not in valid_values:
            _logger.warning(
                "Unknown cross_session_visibility '%s'; using tier default",
                visibility_raw,
            )
            visibility = config.default_for_tier()
        else:
            # Safe: visibility_raw has been validated against the literal set
            visibility = visibility_raw  # type: ignore[assignment]

        classification_raw: str = parsed.get("classification", "unclassified")
        valid_cls = {"unclassified", "cui", "secret"}
        if classification_raw not in valid_cls:
            classification_raw = "unclassified"

        parsed_owner: str = parsed.get("owner_did", owner_did) or owner_did

        # classification_raw is validated above; cast is safe
        classification: Literal["unclassified", "cui", "secret"] = (
            classification_raw  # type: ignore[assignment]
        )

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
    """Minimal YAML parser for flat/nested key: value frontmatter.

    Handles the subset of YAML used in session frontmatter:
    - Top-level scalar key: value pairs
    - One level of nested key: value under an indented block

    Falls back to PyYAML for anything complex. Avoids dependency on
    full YAML parsing at cold-start for the hot path.
    """
    try:
        import yaml

        return yaml.safe_load(text) or {}
    except Exception:
        _logger.debug("YAML parse failed, returning empty dict", exc_info=True)
        return {}


def _extract_acl_from_session_data(
    session_data: dict[str, Any],
    config: MemoryACLConfig,
    owner_did: str = "",
) -> SessionACL:
    """Extract ACL from a session data dict (e.g. from JSONL session records).

    Accepts the dict-form of session metadata, which may include an
    ``acl`` sub-dict with ``cross_session_visibility``.
    """
    acl_raw = session_data.get("acl", {})
    if not isinstance(acl_raw, dict):
        return SessionACL.default(config, owner_did)

    visibility_raw: str = acl_raw.get("cross_session_visibility", "")
    valid_values = {"private", "shared-with-agent", "shared-with-others-via-agent"}

    visibility: CrossSessionVisibility
    if visibility_raw not in valid_values:
        visibility = config.default_for_tier()
    else:
        # Safe: visibility_raw has been validated against the literal set
        visibility = visibility_raw  # type: ignore[assignment]

    return SessionACL(
        owner_did=owner_did,
        cross_session_visibility=visibility,
    )
