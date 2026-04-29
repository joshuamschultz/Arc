"""Federation per-agent annotation tests for ProfileStore (TD-019).

SDD §3.6 adds per-agent annotations stored at:
    user_profile/{user_did}/agents/{agent_did}.md

These allow each agent to maintain isolated, agent-specific notes about
a user without polluting the shared base profile.

Five scenarios:
1. Write agent annotation → separate file created in agents/ subdir
2. Read without agent_did → base profile only (no merge)
3. Read with agent_did → base profile + annotation merged (annotations appended)
4. Tombstone user → agents/ subdir deleted together with base profile
5. Two agents have different annotations for same user → isolated files
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from arcagent.modules.user_profile.errors import ProfileNotFound
from arcagent.modules.user_profile.models import ACL, UserProfile
from arcagent.modules.user_profile.store import ProfileStore
from arcagent.modules.user_profile.tombstone import apply_tombstone

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return a fresh workspace directory for each test."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def store(workspace: Path) -> ProfileStore:
    """Return a ProfileStore backed by the test workspace."""
    return ProfileStore(workspace=workspace)


def _make_profile(user_did: str) -> UserProfile:
    """Build a minimal valid UserProfile for testing."""
    return UserProfile(
        user_did=user_did,
        created=datetime.now(tz=UTC),
        classification="unclassified",
        acl=ACL(owner=user_did),
    )


def _write_base_profile(store: ProfileStore, user_did: str) -> UserProfile:
    """Create and persist a default profile, returning the UserProfile."""
    return store.create_default(user_did)


# ---------------------------------------------------------------------------
# Scenario 1 — Write agent annotation → separate file created
# ---------------------------------------------------------------------------


class TestWriteAgentAnnotation:
    def test_annotation_file_created_in_agents_subdir(
        self, store: ProfileStore, workspace: Path
    ) -> None:
        """write_agent_annotation writes to user_profile/{user}/agents/{agent}.md."""
        user_did = "did:arc:user:alice"
        agent_did = "did:arc:agent:assistant"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(
            user_did, agent_did, section="Preferences", content="Alice likes brevity."
        )

        annotation_path = store.agent_annotation_path(user_did, agent_did)
        assert annotation_path.is_file(), f"Expected annotation file at {annotation_path}"

    def test_annotation_file_separate_from_base_profile(
        self, store: ProfileStore, workspace: Path
    ) -> None:
        """Annotation file must be different from the base profile file."""
        user_did = "did:arc:user:alice"
        agent_did = "did:arc:agent:assistant"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", "Some notes.")

        base_path = store.profile_path(user_did)
        annotation_path = store.agent_annotation_path(user_did, agent_did)

        assert base_path != annotation_path, "Annotation must not overwrite base profile"
        assert base_path.is_file(), "Base profile must still exist after annotation write"

    def test_annotation_file_contains_section_and_content(self, store: ProfileStore) -> None:
        """Annotation file should contain the section heading and body content."""
        user_did = "did:arc:user:bob"
        agent_did = "did:arc:agent:coder"
        content = "Bob prefers Python over JavaScript."

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Language Prefs", content)

        annotation_path = store.agent_annotation_path(user_did, agent_did)
        text = annotation_path.read_text(encoding="utf-8")

        assert "Language Prefs" in text
        assert content in text

    def test_annotation_file_path_layout(self, store: ProfileStore, workspace: Path) -> None:
        """Annotation path follows the {profile_dir}/{user}/agents/{agent}.md layout."""
        user_did = "did:arc:user:carol"
        agent_did = "did:arc:agent:planner"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Plan", "Long-term goals.")

        annotation_path = store.agent_annotation_path(user_did, agent_did)
        # The path must include the "agents" directory component
        assert "agents" in annotation_path.parts, f"Expected 'agents' in path {annotation_path}"


# ---------------------------------------------------------------------------
# Scenario 2 — Read without agent_did → base only
# ---------------------------------------------------------------------------


class TestReadBaseOnly:
    def test_read_without_agent_did_returns_base_profile(self, store: ProfileStore) -> None:
        """read_user_profile(user_did) with no agent_did returns base profile text."""
        user_did = "did:arc:user:diana"
        _write_base_profile(store, user_did)

        text = store.read_user_profile(user_did)

        assert user_did in text or "diana" in text.lower() or len(text) > 0

    def test_read_without_agent_did_excludes_annotation_content(self, store: ProfileStore) -> None:
        """When agent_did is None, annotation content must not appear in result."""
        user_did = "did:arc:user:diana"
        agent_did = "did:arc:agent:x"
        annotation_marker = "ANNOTATION_ONLY_CONTENT_MARKER"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", annotation_marker)

        text = store.read_user_profile(user_did)
        assert annotation_marker not in text, "Base-only read must not include annotation content"

    def test_read_without_agent_did_raises_for_missing_profile(self, store: ProfileStore) -> None:
        """read_user_profile raises ProfileNotFound when no base profile exists."""
        with pytest.raises(ProfileNotFound):
            store.read_user_profile("did:arc:user:ghost")


# ---------------------------------------------------------------------------
# Scenario 3 — Read with agent_did → base + annotations merged
# ---------------------------------------------------------------------------


class TestReadWithAnnotation:
    def test_read_with_agent_did_includes_annotation(self, store: ProfileStore) -> None:
        """read_user_profile(user_did, agent_did) merges base + annotation."""
        user_did = "did:arc:user:eve"
        agent_did = "did:arc:agent:planner"
        annotation_marker = "PLANNER_ANNOTATION_UNIQUE_MARKER"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", annotation_marker)

        merged = store.read_user_profile(user_did, agent_did=agent_did)

        assert annotation_marker in merged, "Merged result must include annotation content"

    def test_read_with_agent_did_includes_base_profile(self, store: ProfileStore) -> None:
        """The merged result must still contain base profile content."""
        user_did = "did:arc:user:eve"
        agent_did = "did:arc:agent:planner"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", "Some notes.")

        merged = store.read_user_profile(user_did, agent_did=agent_did)

        # Base profile contains the user_did in frontmatter
        assert user_did in merged, "Merged result must contain base profile content"

    def test_read_with_nonexistent_annotation_returns_base_only(self, store: ProfileStore) -> None:
        """When the annotation file doesn't exist, base profile is returned unchanged."""
        user_did = "did:arc:user:frank"
        _write_base_profile(store, user_did)

        text_with_agent = store.read_user_profile(user_did, agent_did="did:arc:agent:nobody")
        text_base = store.read_user_profile(user_did)

        assert text_with_agent == text_base, (
            "Missing annotation file should return base profile identical to no-agent read"
        )

    def test_annotation_content_appended_after_base(self, store: ProfileStore) -> None:
        """Annotation content appears after base profile content in merged output."""
        user_did = "did:arc:user:grace"
        agent_did = "did:arc:agent:writer"
        annotation_content = "ANNOTATION_MARKER_AFTER_BASE"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", annotation_content)

        merged = store.read_user_profile(user_did, agent_did=agent_did)

        base_text = store.read_user_profile(user_did)
        # Annotation must appear after the last character of the base profile
        base_end = merged.index(base_text[-20:].strip()) + len(base_text[-20:].strip())
        annotation_start = merged.index(annotation_content)

        assert annotation_start > base_end, (
            "Annotation content must be appended after base profile content"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — Tombstone user → agents/ subdir deleted
# ---------------------------------------------------------------------------


class TestTombstoneSweeopsAgentsDir:
    def test_tombstone_deletes_agents_subdir(self, store: ProfileStore, workspace: Path) -> None:
        """apply_tombstone deletes the agents/ annotation subdirectory."""
        user_did = "did:arc:user:harry"
        agent_did = "did:arc:agent:assistant"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", "Private notes.")

        agents_dir = store._agent_annotations_dir(user_did)
        assert agents_dir.is_dir(), "agents/ dir must exist before tombstone"

        apply_tombstone(user_did, workspace=workspace)

        assert not agents_dir.is_dir(), (
            "apply_tombstone must delete the agents/ annotation subdirectory"
        )

    def test_tombstone_deletes_base_profile_and_agents(
        self, store: ProfileStore, workspace: Path
    ) -> None:
        """Tombstone removes both the base profile file and agents/ subdir."""
        user_did = "did:arc:user:irene"
        agent_did = "did:arc:agent:coder"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", "Agent notes.")

        apply_tombstone(user_did, workspace=workspace)

        assert not store.profile_path(user_did).is_file(), "Base profile must be deleted"
        assert not store._agent_annotations_dir(user_did).is_dir(), "agents/ dir must be deleted"

    def test_tombstone_without_agents_dir_does_not_crash(
        self, store: ProfileStore, workspace: Path
    ) -> None:
        """apply_tombstone succeeds even when no agents/ subdir exists."""
        user_did = "did:arc:user:jake"
        _write_base_profile(store, user_did)
        # No annotation written — agents/ dir should not exist

        # Must not raise
        apply_tombstone(user_did, workspace=workspace)


# ---------------------------------------------------------------------------
# Scenario 5 — Two agents have different annotations for same user → isolated
# ---------------------------------------------------------------------------


class TestIsolatedAgentAnnotations:
    def test_two_agents_have_separate_annotation_files(self, store: ProfileStore) -> None:
        """Each agent's annotation is stored in a separate file."""
        user_did = "did:arc:user:kate"
        agent_a = "did:arc:agent:alpha"
        agent_b = "did:arc:agent:beta"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_a, "Notes", "Alpha notes.")
        store.write_agent_annotation(user_did, agent_b, "Notes", "Beta notes.")

        path_a = store.agent_annotation_path(user_did, agent_a)
        path_b = store.agent_annotation_path(user_did, agent_b)

        assert path_a != path_b, "Each agent must have a distinct annotation file"
        assert path_a.is_file(), "Agent A annotation file must exist"
        assert path_b.is_file(), "Agent B annotation file must exist"

    def test_agent_a_annotation_does_not_contain_agent_b_content(
        self, store: ProfileStore
    ) -> None:
        """Writing agent B's annotation must not affect agent A's file."""
        user_did = "did:arc:user:kate"
        agent_a = "did:arc:agent:alpha"
        agent_b = "did:arc:agent:beta"
        content_a = "UNIQUE_ALPHA_CONTENT"
        content_b = "UNIQUE_BETA_CONTENT"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_a, "Notes", content_a)
        store.write_agent_annotation(user_did, agent_b, "Notes", content_b)

        text_a = store.agent_annotation_path(user_did, agent_a).read_text()
        text_b = store.agent_annotation_path(user_did, agent_b).read_text()

        assert content_a in text_a
        assert content_b not in text_a
        assert content_b in text_b
        assert content_a not in text_b

    def test_merged_read_returns_only_requesting_agents_annotation(
        self, store: ProfileStore
    ) -> None:
        """read_user_profile merges only the requesting agent's annotation."""
        user_did = "did:arc:user:leo"
        agent_a = "did:arc:agent:alpha"
        agent_b = "did:arc:agent:beta"
        content_a = "ALPHA_PRIVATE_OBSERVATION"
        content_b = "BETA_PRIVATE_OBSERVATION"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_a, "Private", content_a)
        store.write_agent_annotation(user_did, agent_b, "Private", content_b)

        merged_a = store.read_user_profile(user_did, agent_did=agent_a)
        merged_b = store.read_user_profile(user_did, agent_did=agent_b)

        # Agent A's view must include A's content but not B's
        assert content_a in merged_a
        assert content_b not in merged_a

        # Agent B's view must include B's content but not A's
        assert content_b in merged_b
        assert content_a not in merged_b

    def test_overwrite_annotation_replaces_content(self, store: ProfileStore) -> None:
        """Writing a new annotation for the same agent replaces the old one."""
        user_did = "did:arc:user:mia"
        agent_did = "did:arc:agent:assistant"
        old_content = "OLD_ANNOTATION_CONTENT"
        new_content = "NEW_ANNOTATION_CONTENT"

        _write_base_profile(store, user_did)
        store.write_agent_annotation(user_did, agent_did, "Notes", old_content)
        store.write_agent_annotation(user_did, agent_did, "Notes", new_content)

        text = store.agent_annotation_path(user_did, agent_did).read_text()
        assert new_content in text
        assert old_content not in text
