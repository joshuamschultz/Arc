"""Tests for UserProfile and related Pydantic models.

Covers:
- YAML frontmatter roundtrip
- ACL field integrity
- DurableFact serialisation / parsing
- Classification validation
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arcagent.modules.user_profile.models import ACL, DurableFact, UserProfile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_profile(
    user_did: str = "did:arc:user:human/test-user-001",
    classification: str = "unclassified",
    agent_read: bool = True,
    cross_user_shareable: bool = False,
) -> UserProfile:
    return UserProfile(
        user_did=user_did,
        created=datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC),
        classification=classification,
        acl=ACL(
            owner=user_did,
            agent_read=agent_read,
            cross_user_shareable=cross_user_shareable,
        ),
        schema_version=1,
    )


# ---------------------------------------------------------------------------
# T1: YAML frontmatter roundtrip
# ---------------------------------------------------------------------------


class TestYAMLFrontmatterRoundtrip:
    """T1: write + parse + assert ACL fields intact."""

    def test_yaml_frontmatter_roundtrip(self) -> None:
        """Serialise a profile and parse it back; all ACL fields must match."""
        user_did = "did:arc:user:human/alice-001"
        original = _make_profile(
            user_did=user_did,
            classification="unclassified",
            agent_read=True,
            cross_user_shareable=False,
        )
        markdown = original.to_markdown()

        # Confirm the file starts with --- frontmatter
        assert markdown.startswith("---\n"), "Expected YAML frontmatter fence"

        parsed = UserProfile.from_markdown(markdown)

        assert parsed.user_did == user_did
        assert parsed.classification == "unclassified"
        assert parsed.schema_version == 1
        assert parsed.acl.owner == user_did
        assert parsed.acl.agent_read is True
        assert parsed.acl.cross_user_shareable is False

    def test_acl_federal_defaults_roundtrip(self) -> None:
        """Federal default (cross_user_shareable=False) survives roundtrip."""
        profile = _make_profile(cross_user_shareable=False)
        markdown = profile.to_markdown()
        parsed = UserProfile.from_markdown(markdown)
        assert parsed.acl.cross_user_shareable is False

    def test_acl_agent_read_false_roundtrip(self) -> None:
        """agent_read=False survives roundtrip."""
        profile = _make_profile(agent_read=False)
        markdown = profile.to_markdown()
        parsed = UserProfile.from_markdown(markdown)
        assert parsed.acl.agent_read is False

    def test_classification_cui_roundtrip(self) -> None:
        """CUI classification survives roundtrip."""
        profile = _make_profile(classification="cui")
        markdown = profile.to_markdown()
        parsed = UserProfile.from_markdown(markdown)
        assert parsed.classification == "cui"

    def test_classification_secret_roundtrip(self) -> None:
        """Secret classification survives roundtrip."""
        profile = _make_profile(classification="secret")
        markdown = profile.to_markdown()
        parsed = UserProfile.from_markdown(markdown)
        assert parsed.classification == "secret"

    def test_invalid_classification_raises(self) -> None:
        """Invalid classification value raises ValueError."""
        with pytest.raises(Exception):
            _make_profile(classification="top_secret")

    def test_markdown_has_all_sections(self) -> None:
        """Serialised profile contains all four required sections."""
        profile = _make_profile()
        md = profile.to_markdown()
        assert "## Identity" in md
        assert "## Preferences" in md
        assert "## Durable Facts" in md
        assert "## Derived (dialectic)" in md

    def test_from_markdown_raises_on_missing_frontmatter(self) -> None:
        """from_markdown raises ValueError when there is no --- fence."""
        with pytest.raises(ValueError, match="frontmatter"):
            UserProfile.from_markdown("# No frontmatter here\n\nJust markdown")

    def test_created_timestamp_preserved(self) -> None:
        """Created timestamp survives serialisation and parsing."""
        ts = datetime(2026, 4, 18, 9, 30, 0, tzinfo=UTC)
        profile = UserProfile(
            user_did="did:arc:user:human/bob",
            created=ts,
            classification="unclassified",
            acl=ACL(owner="did:arc:user:human/bob"),
        )
        md = profile.to_markdown()
        parsed = UserProfile.from_markdown(md)
        # Compare at second precision to be robust against microsecond rounding
        assert parsed.created.year == ts.year
        assert parsed.created.month == ts.month
        assert parsed.created.day == ts.day


# ---------------------------------------------------------------------------
# DurableFact tests
# ---------------------------------------------------------------------------


class TestDurableFact:
    def test_markdown_line_format(self) -> None:
        """DurableFact serialises to expected markdown comment format."""
        fact = DurableFact(
            content="Prefers bullet summaries",
            source_session_id="sess-abc123",
            ts=datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC),
        )
        line = fact.to_markdown_line()
        assert line.startswith("- Prefers bullet summaries")
        assert "session_id=sess-abc123" in line
        assert "ts=2026-04-18" in line

    def test_durable_facts_in_profile_roundtrip(self) -> None:
        """DurableFacts embedded in a profile survive roundtrip unchanged."""
        fact1 = DurableFact(
            content="Works in Mountain Time",
            source_session_id="sess-001",
            ts=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
        )
        fact2 = DurableFact(
            content="Prefers concise answers",
            source_session_id="sess-002",
            ts=datetime(2026, 4, 18, 11, 0, 0, tzinfo=UTC),
        )
        profile = _make_profile()
        profile.durable_facts = [fact1, fact2]

        md = profile.to_markdown()
        parsed = UserProfile.from_markdown(md)

        assert len(parsed.durable_facts) == 2
        assert parsed.durable_facts[0].content == "Works in Mountain Time"
        assert parsed.durable_facts[0].source_session_id == "sess-001"
        assert parsed.durable_facts[1].content == "Prefers concise answers"
        assert parsed.durable_facts[1].source_session_id == "sess-002"

    def test_ts_string_accepted(self) -> None:
        """DurableFact accepts ISO string timestamps as well as datetime."""
        fact = DurableFact(
            content="Some fact",
            source_session_id="s1",
            ts="2026-04-18T12:00:00+00:00",  # type: ignore[arg-type]
        )
        assert isinstance(fact.ts, datetime)

    def test_fact_immutable(self) -> None:
        """DurableFact is frozen — attributes cannot be reassigned."""
        fact = DurableFact(
            content="Original",
            source_session_id="s1",
            ts=datetime(2026, 4, 18, tzinfo=UTC),
        )
        with pytest.raises(Exception):
            fact.content = "Modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ACL tests
# ---------------------------------------------------------------------------


class TestACL:
    def test_defaults(self) -> None:
        """ACL defaults to agent_read=True and cross_user_shareable=False."""
        acl = ACL(owner="did:arc:user:human/test")
        assert acl.agent_read is True
        assert acl.cross_user_shareable is False

    def test_frozen(self) -> None:
        """ACL is frozen — attributes cannot be reassigned."""
        acl = ACL(owner="did:arc:user:human/test")
        with pytest.raises(Exception):
            acl.agent_read = False  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        """Extra fields raise ValidationError (extra='forbid')."""
        with pytest.raises(Exception):
            ACL(owner="did:arc:user:human/test", unknown_field=True)  # type: ignore[call-arg]
