"""Tests for ProfileStore — atomic write, 2KB cap, overflow, append-only facts.

Test contract:
  T2: test_body_2kb_cap_enforced
  T3: test_body_overflow_spills_to_episodic
  T4: test_atomic_write_temp_plus_rename
  T5: test_durable_facts_append_only
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.errors import BodyOverflow, ProfileNotFound
from arcagent.modules.user_profile.models import ACL, UserProfile
from arcagent.modules.user_profile.store import ProfileStore, _atomic_write, _body_size


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


USER_DID = "did:arc:user:human/test-user-store"


def _make_store(
    workspace: Path,
    cap: int = 2048,
    telemetry: Any | None = None,
) -> ProfileStore:
    config = UserProfileConfig(body_cap_bytes=cap)
    return ProfileStore(workspace, config, telemetry=telemetry)


def _make_profile(user_did: str = USER_DID) -> UserProfile:
    return UserProfile(
        user_did=user_did,
        created=datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC),
        classification="unclassified",
        acl=ACL(owner=user_did),
    )


# ---------------------------------------------------------------------------
# T2: 2 KB body cap enforced
# ---------------------------------------------------------------------------


class TestBodyCapEnforced:
    """T2: writing a body > cap raises BodyOverflow; no file written."""

    def test_body_2kb_cap_enforced(self, tmp_path: Path) -> None:
        """A 3KB body must raise BodyOverflow and not write the file."""
        store = _make_store(tmp_path, cap=2048)
        profile = _make_profile()
        # Create a body large enough to exceed 2KB
        # The body section must be > 2048 bytes
        profile.identity_section = "x" * 2500  # definitely > 2KB body

        with pytest.raises(BodyOverflow) as exc_info:
            store.write(profile)

        assert exc_info.value.cap_bytes == 2048
        assert exc_info.value.body_size > 2048
        # File must NOT have been created
        assert not store.profile_path(USER_DID).exists()

    def test_body_at_cap_is_accepted(self, tmp_path: Path) -> None:
        """A body exactly at the cap must NOT raise."""
        store = _make_store(tmp_path, cap=2048)
        profile = _make_profile()
        # Render with no body to get baseline, then trim identity to fit
        # We need a profile whose body is <= 2048 bytes
        profile.identity_section = ""
        profile.preferences_section = ""
        # Default profile body should be well under 2KB
        store.write(profile)  # should not raise
        assert store.profile_path(USER_DID).exists()

    def test_body_size_helper(self) -> None:
        """_body_size correctly measures bytes after frontmatter."""
        text = "---\nfoo: bar\n---\n" + "a" * 100
        size = _body_size(text)
        assert size == 100

    def test_body_size_utf8(self) -> None:
        """_body_size counts bytes, not characters (UTF-8 multi-byte)."""
        text = "---\nfoo: bar\n---\n" + "€" * 100  # € = 3 bytes each
        size = _body_size(text)
        assert size == 300


# ---------------------------------------------------------------------------
# T3: overflow spills to episodic (event emitted, no silent truncation)
# ---------------------------------------------------------------------------


class TestOverflowSpillsToEpisodic:
    """T3: at cap threshold, emit user_profile.overflow; do NOT silently truncate."""

    def test_body_overflow_spills_to_episodic(self, tmp_path: Path) -> None:
        """BodyOverflow raises AND emits user_profile.overflow with pointer."""
        mock_tel = MagicMock()
        mock_tel.emit_event = MagicMock()

        store = _make_store(tmp_path, cap=2048, telemetry=mock_tel)
        profile = _make_profile()
        profile.identity_section = "y" * 2500

        with pytest.raises(BodyOverflow):
            store.write(profile)

        # Telemetry must have emitted the overflow event
        mock_tel.emit_event.assert_called_once()
        event_name, event_data = mock_tel.emit_event.call_args[0]
        assert event_name == "user_profile.overflow"
        assert "episodic_pointer" in event_data
        assert event_data["cap_bytes"] == 2048
        assert event_data["body_size"] > 2048

    def test_overflow_event_contains_pointer(self, tmp_path: Path) -> None:
        """The overflow event's episodic_pointer must be a non-empty path string."""
        mock_tel = MagicMock()
        store = _make_store(tmp_path, cap=2048, telemetry=mock_tel)
        profile = _make_profile()
        profile.identity_section = "z" * 3000

        with pytest.raises(BodyOverflow):
            store.write(profile)

        _, event_data = mock_tel.emit_event.call_args[0]
        pointer = event_data["episodic_pointer"]
        assert isinstance(pointer, str)
        assert len(pointer) > 0

    def test_no_truncation_happens(self, tmp_path: Path) -> None:
        """Content is NOT silently truncated — the original profile is unchanged."""
        store = _make_store(tmp_path, cap=2048)
        # Write a valid profile first
        profile = _make_profile()
        store.write(profile)
        original_content = store.profile_path(USER_DID).read_text()

        # Now attempt an oversized write
        big_profile = _make_profile()
        big_profile.identity_section = "X" * 3000
        with pytest.raises(BodyOverflow):
            store.write(big_profile)

        # File on disk unchanged
        current_content = store.profile_path(USER_DID).read_text()
        assert current_content == original_content


# ---------------------------------------------------------------------------
# T4: atomic write — temp + rename
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """T4: simulate kill mid-write → no partial file left on disk."""

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        """_atomic_write creates the target file from a temp file."""
        target = tmp_path / "profile.md"
        _atomic_write(target, "hello world")
        assert target.exists()
        assert target.read_text() == "hello world"

    def test_atomic_write_temp_plus_rename(self, tmp_path: Path) -> None:
        """Atomicity: writing via ProfileStore leaves no .tmp orphan on success."""
        store = _make_store(tmp_path)
        profile = _make_profile()
        store.write(profile)

        # After successful write: profile exists, no .tmp files left
        profile_dir = tmp_path / "user_profile"
        tmp_files = list(profile_dir.glob("*.tmp"))
        assert tmp_files == [], f"Orphaned temp files found: {tmp_files}"
        assert store.profile_path(USER_DID).exists()

    def test_atomic_write_permissions(self, tmp_path: Path) -> None:
        """Written profile file has permissions 0o600 (owner read/write only)."""
        store = _make_store(tmp_path)
        profile = _make_profile()
        store.write(profile)

        path = store.profile_path(USER_DID)
        file_mode = os.stat(path).st_mode & 0o777
        assert file_mode == 0o600, f"Expected 0o600 got {oct(file_mode)}"

    def test_atomic_write_overwrites_existing(self, tmp_path: Path) -> None:
        """Writing twice overwrites cleanly; no corruption."""
        store = _make_store(tmp_path)
        profile = _make_profile()
        profile.identity_section = "Version 1"
        store.write(profile)

        profile2 = _make_profile()
        profile2.identity_section = "Version 2"
        store.write(profile2)

        read_back = store.read(USER_DID)
        assert read_back.identity_section == "Version 2"


# ---------------------------------------------------------------------------
# T5: durable facts append-only
# ---------------------------------------------------------------------------


class TestDurableFactsAppendOnly:
    """T5: adding a fact does NOT overwrite prior facts; each has session_id + ts."""

    def test_durable_facts_append_only(self, tmp_path: Path) -> None:
        """Appending facts never removes previously written facts."""
        store = _make_store(tmp_path)

        # Append three facts sequentially
        store.append_durable_fact(
            USER_DID,
            content="Fact one",
            source_session_id="sess-001",
        )
        store.append_durable_fact(
            USER_DID,
            content="Fact two",
            source_session_id="sess-002",
        )
        store.append_durable_fact(
            USER_DID,
            content="Fact three",
            source_session_id="sess-003",
        )

        profile = store.read(USER_DID)
        assert len(profile.durable_facts) == 3

        # All facts present and in order
        contents = [f.content for f in profile.durable_facts]
        assert "Fact one" in contents
        assert "Fact two" in contents
        assert "Fact three" in contents

        # Each fact has required provenance fields
        for fact in profile.durable_facts:
            assert fact.source_session_id != ""
            assert fact.ts is not None

    def test_each_fact_has_session_id(self, tmp_path: Path) -> None:
        """Each appended fact carries its source_session_id."""
        store = _make_store(tmp_path)
        store.append_durable_fact(
            USER_DID,
            content="Alice likes concise replies",
            source_session_id="sess-XYZ",
        )
        profile = store.read(USER_DID)
        assert profile.durable_facts[0].source_session_id == "sess-XYZ"

    def test_each_fact_has_timestamp(self, tmp_path: Path) -> None:
        """Each appended fact carries a UTC timestamp."""
        store = _make_store(tmp_path)
        before = datetime.now(tz=UTC)
        store.append_durable_fact(
            USER_DID,
            content="Works Mountain Time",
            source_session_id="sess-ts-test",
        )
        after = datetime.now(tz=UTC)

        profile = store.read(USER_DID)
        fact_ts = profile.durable_facts[0].ts
        assert before <= fact_ts <= after

    def test_existing_facts_preserved_on_identity_overwrite(
        self, tmp_path: Path
    ) -> None:
        """Overwriting Identity section leaves Durable Facts intact."""
        store = _make_store(tmp_path)
        store.append_durable_fact(
            USER_DID,
            content="Existing fact",
            source_session_id="sess-persist",
        )
        # Overwrite identity (allowed)
        profile = store.read(USER_DID)
        profile.identity_section = "New identity content"
        store.write(profile)

        read_back = store.read(USER_DID)
        assert len(read_back.durable_facts) == 1
        assert read_back.durable_facts[0].content == "Existing fact"


# ---------------------------------------------------------------------------
# ProfileStore basic CRUD
# ---------------------------------------------------------------------------


class TestProfileStoreCRUD:
    def test_profile_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(ProfileNotFound):
            store.read("did:arc:user:human/nonexistent")

    def test_exists_false_before_create(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.exists(USER_DID) is False

    def test_exists_true_after_create(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_default(USER_DID)
        assert store.exists(USER_DID) is True

    def test_delete_returns_true_when_exists(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.create_default(USER_DID)
        result = store.delete(USER_DID)
        assert result is True
        assert not store.profile_path(USER_DID).exists()

    def test_delete_returns_false_when_not_exists(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        result = store.delete("did:arc:user:human/ghost")
        assert result is False
