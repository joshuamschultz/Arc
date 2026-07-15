"""Cross-session ACL data model + frontmatter parsing (moved from arcagent memory_acl).

The ACL is memory-implementation policy, so it lives with arcmemory. arcagent's generic
memory adapter only asks the Brain provider to :meth:`~arcmemory.ArcMemoryBrain.authorize`
an operation; this engine is how arcmemory answers and gates cross-session visibility.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory import ArcMemoryBrain
from arcmemory.acl import (
    MemoryACLConfig,
    SessionACL,
    extract_acl_from_session_data,
)


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


class TestFromFrontmatter:
    def test_parses_private_visibility(self) -> None:
        acl = SessionACL.from_frontmatter(_frontmatter("private"), _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_parses_shared_with_agent(self) -> None:
        acl = SessionACL.from_frontmatter(_frontmatter("shared-with-agent"), _config("personal"))
        assert acl.cross_session_visibility == "shared-with-agent"

    def test_parses_shared_with_others_via_agent(self) -> None:
        acl = SessionACL.from_frontmatter(
            _frontmatter("shared-with-others-via-agent"), _config("enterprise")
        )
        assert acl.cross_session_visibility == "shared-with-others-via-agent"

    def test_no_frontmatter_uses_tier_default(self) -> None:
        acl = SessionACL.from_frontmatter("# Just markdown", _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_unknown_visibility_falls_back_to_tier_default(self) -> None:
        content = "---\nacl:\n  cross_session_visibility: unknown_value\n---\n"
        acl = SessionACL.from_frontmatter(content, _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_malformed_frontmatter_falls_back(self) -> None:
        acl = SessionACL.from_frontmatter("---\n{bad: [yaml: here\n---\n", _config("federal"))
        assert acl.cross_session_visibility == "private"

    def test_owner_did_extracted_from_frontmatter(self) -> None:
        owner = "did:arc:org:user/testowner"
        acl = SessionACL.from_frontmatter(_frontmatter("private", owner_did=owner), _config("federal"))
        assert acl.owner_did == owner

    def test_owner_did_falls_back_to_kwarg(self) -> None:
        content = "---\nacl:\n  cross_session_visibility: private\n---\n"
        acl = SessionACL.from_frontmatter(content, _config("federal"), owner_did="did:arc:fallback")
        assert acl.owner_did == "did:arc:fallback"


class TestTierDefaults:
    def test_federal_default_is_private(self) -> None:
        assert _config("federal").default_for_tier() == "private"

    def test_enterprise_default_is_shared_with_agent(self) -> None:
        assert _config("enterprise").default_for_tier() == "shared-with-agent"

    def test_personal_default_is_shared_with_agent(self) -> None:
        assert _config("personal").default_for_tier() == "shared-with-agent"

    def test_session_acl_default_uses_tier(self) -> None:
        acl = SessionACL.default(_config("federal"), owner_did="did:arc:user")
        assert acl.cross_session_visibility == "private"


class TestExtractFromSessionData:
    def test_extracts_private(self) -> None:
        data = {"acl": {"cross_session_visibility": "private"}}
        acl = extract_acl_from_session_data(data, _config("personal"))
        assert acl.cross_session_visibility == "private"

    def test_missing_acl_key_uses_tier_default(self) -> None:
        acl = extract_acl_from_session_data({}, _config("personal"))
        assert acl.cross_session_visibility == "shared-with-agent"

    def test_invalid_acl_type_uses_tier_default(self) -> None:
        acl = extract_acl_from_session_data({"acl": "not_a_dict"}, _config("federal"))
        assert acl.cross_session_visibility == "private"


class TestBrainAuthorize:
    """The provider-side ``authorize`` seam arcagent's generic adapter consults."""

    async def test_bound_agent_is_authorized(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path, "did:arc:agent/a")
        assert await brain.authorize("memory.write", caller_did="did:arc:agent/a") is True

    async def test_empty_caller_is_authorized(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path, "did:arc:agent/a")
        assert await brain.authorize("memory.search", caller_did="") is True

    async def test_other_caller_is_denied(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path, "did:arc:agent/a")
        assert await brain.authorize("memory.search", caller_did="did:arc:agent/other") is False

    async def test_denial_emits_structured_audit(self, tmp_path: Path) -> None:
        events: list[object] = []

        class _Sink:
            def write(self, event: object) -> None:
                events.append(event)

        brain = ArcMemoryBrain(tmp_path, "did:arc:agent/a", audit_sink=_Sink())
        await brain.authorize("memory.read", caller_did="did:arc:agent/other")
        assert any(getattr(e, "action", "") == "memory.acl.denied" for e in events)
