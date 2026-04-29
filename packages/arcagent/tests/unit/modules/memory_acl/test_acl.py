"""Unit tests for SessionACL data model and frontmatter parsing."""

from __future__ import annotations

from arcagent.modules.memory_acl.acl import (
    SessionACL,
    _extract_acl_from_session_data,
)
from arcagent.modules.memory_acl.config import MemoryACLConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(tier: str = "federal") -> MemoryACLConfig:
    return MemoryACLConfig(tier=tier)  # type: ignore[arg-type]


def _frontmatter(visibility: str, owner_did: str = "did:arc:org:user/abc") -> str:
    return f"""---
acl:
  cross_session_visibility: {visibility}
owner_did: {owner_did}
classification: unclassified
---

# Session content here
"""


# ---------------------------------------------------------------------------
# T2.1.3 — ACL data model
# ---------------------------------------------------------------------------


class TestSessionACLModel:
    def test_default_fields(self) -> None:
        acl = SessionACL()
        assert acl.owner_did == ""
        assert acl.cross_session_visibility == "private"
        assert acl.classification == "unclassified"

    def test_private_blocks_other_caller(self) -> None:
        acl = SessionACL(owner_did="did:arc:org:user/owner", cross_session_visibility="private")
        result = acl.allows_read_by("did:arc:org:user/stranger", agent_did="did:arc:org:agent/1")
        assert result is False

    def test_owner_always_allowed(self) -> None:
        owner = "did:arc:org:user/owner"
        acl = SessionACL(owner_did=owner, cross_session_visibility="private")
        assert acl.allows_read_by(owner, agent_did="did:arc:org:agent/1") is True

    def test_shared_with_agent_allows_agent(self) -> None:
        agent_did = "did:arc:org:agent/myagent"
        acl = SessionACL(
            owner_did="did:arc:org:user/owner",
            cross_session_visibility="shared-with-agent",
        )
        assert acl.allows_read_by(agent_did, agent_did=agent_did) is True

    def test_shared_with_agent_blocks_stranger(self) -> None:
        acl = SessionACL(
            owner_did="did:arc:org:user/owner",
            cross_session_visibility="shared-with-agent",
        )
        result = acl.allows_read_by(
            "did:arc:org:user/stranger",
            agent_did="did:arc:org:agent/myagent",
        )
        assert result is False

    def test_shared_with_others_via_agent_allows_agent(self) -> None:
        agent_did = "did:arc:org:agent/proxy"
        acl = SessionACL(
            owner_did="did:arc:org:user/owner",
            cross_session_visibility="shared-with-others-via-agent",
        )
        assert acl.allows_read_by(agent_did, agent_did=agent_did) is True

    def test_shared_with_others_via_agent_blocks_non_agent(self) -> None:
        acl = SessionACL(
            owner_did="did:arc:org:user/owner",
            cross_session_visibility="shared-with-others-via-agent",
        )
        result = acl.allows_read_by(
            "did:arc:org:user/third_party",
            agent_did="did:arc:org:agent/proxy",
        )
        assert result is False


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestFromFrontmatter:
    def test_parses_private_visibility(self) -> None:
        content = _frontmatter("private")
        acl = SessionACL.from_frontmatter(content, _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_parses_shared_with_agent(self) -> None:
        content = _frontmatter("shared-with-agent")
        acl = SessionACL.from_frontmatter(content, _config("personal"))
        assert acl.cross_session_visibility == "shared-with-agent"

    def test_parses_shared_with_others_via_agent(self) -> None:
        content = _frontmatter("shared-with-others-via-agent")
        acl = SessionACL.from_frontmatter(content, _config("enterprise"))
        assert acl.cross_session_visibility == "shared-with-others-via-agent"

    def test_no_frontmatter_uses_tier_default(self) -> None:
        acl = SessionACL.from_frontmatter("# Just markdown", _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_unknown_visibility_falls_back_to_tier_default(self) -> None:
        content = "---\nacl:\n  cross_session_visibility: unknown_value\n---\n"
        acl = SessionACL.from_frontmatter(content, _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_malformed_frontmatter_falls_back(self) -> None:
        # Should not raise; fail-closed to tier default
        acl = SessionACL.from_frontmatter("---\n{bad: [yaml: here\n---\n", _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_owner_did_extracted_from_frontmatter(self) -> None:
        owner = "did:arc:org:user/testowner"
        content = _frontmatter("private", owner_did=owner)
        acl = SessionACL.from_frontmatter(content, _config("federal"))
        assert acl.owner_did == owner

    def test_owner_did_falls_back_to_kwarg(self) -> None:
        # When frontmatter has no owner_did, use the kwarg
        content = "---\nacl:\n  cross_session_visibility: private\n---\n"
        acl = SessionACL.from_frontmatter(
            content, _config("federal"), owner_did="did:arc:fallback"
        )
        assert acl.owner_did == "did:arc:fallback"


# ---------------------------------------------------------------------------
# Tier defaults
# ---------------------------------------------------------------------------


class TestTierDefaults:
    def test_federal_default_is_private(self) -> None:
        cfg = _config("federal")
        assert cfg.default_for_tier() == "private"

    def test_enterprise_default_is_shared_with_agent(self) -> None:
        cfg = _config("enterprise")
        assert cfg.default_for_tier() == "shared-with-agent"

    def test_personal_default_is_shared_with_agent(self) -> None:
        cfg = _config("personal")
        assert cfg.default_for_tier() == "shared-with-agent"

    def test_session_acl_default_uses_tier(self) -> None:
        acl = SessionACL.default(_config("federal"), owner_did="did:arc:user")
        assert acl.cross_session_visibility == "private"


# ---------------------------------------------------------------------------
# Dict-form extraction
# ---------------------------------------------------------------------------


class TestExtractFromSessionData:
    def test_extracts_private(self) -> None:
        data = {"acl": {"cross_session_visibility": "private"}}
        acl = _extract_acl_from_session_data(data, _config("personal"))
        assert acl.cross_session_visibility == "private"

    def test_missing_acl_key_uses_tier_default(self) -> None:
        acl = _extract_acl_from_session_data({}, _config("personal"))
        assert acl.cross_session_visibility == "shared-with-agent"

    def test_invalid_acl_type_uses_tier_default(self) -> None:
        acl = _extract_acl_from_session_data({"acl": "not_a_dict"}, _config("federal"))
        assert acl.cross_session_visibility == "private"
