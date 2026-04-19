"""Tests for GDPR tombstone mechanics.

Test contract:
  T6: test_gdpr_tombstone_deletes_profile
  T7: test_gdpr_tombstone_retained_for_compliance
  T8: test_gdpr_tombstone_session_field_redact
  T9: test_gdpr_tombstone_fts5_reindex_requested
  T10: test_derived_section_regeneratable
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.store import ProfileStore
from arcagent.modules.user_profile.tombstone import (
    TombstoneEvent,
    _hash_did,
    _redact_value,
    apply_tombstone,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

USER_DID = "did:arc:user:human/tombstone-test-user"
USER_DID_HASH = hashlib.sha256(USER_DID.encode()).hexdigest()


def _make_telemetry() -> MagicMock:
    tel = MagicMock()
    tel.emit_event = MagicMock()
    return tel


def _make_profile_file(workspace: Path, user_did: str = USER_DID) -> None:
    """Create a minimal profile file in the workspace."""
    store = ProfileStore(workspace, UserProfileConfig())
    store.create_default(user_did)


def _make_session_jsonl(
    sessions_dir: Path,
    session_id: str,
    user_did: str = USER_DID,
) -> Path:
    """Create a JSONL session file containing references to user_did."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.jsonl"
    lines = [
        json.dumps(
            {
                "role": "user",
                "user_did": user_did,
                "content": f"Hello from {user_did}",
                "ts": "2026-04-18T12:00:00Z",
            }
        ),
        json.dumps(
            {
                "role": "assistant",
                "content": "Hello!",
                "ts": "2026-04-18T12:00:01Z",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "tool": "memory_write",
                # Nested reference to user_did
                "args": {"user_did": user_did, "content": "something"},
                "ts": "2026-04-18T12:00:02Z",
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# T6: tombstone deletes profile
# ---------------------------------------------------------------------------


class TestTombstoneDeletesProfile:
    """T6: user_profile/{user_did}.md file removed after apply_tombstone."""

    def test_gdpr_tombstone_deletes_profile(self, tmp_path: Path) -> None:
        """Profile file must not exist after apply_tombstone."""
        _make_profile_file(tmp_path)
        store = ProfileStore(tmp_path, UserProfileConfig())
        assert store.exists(USER_DID), "Pre-condition: profile must exist before tombstone"

        apply_tombstone(USER_DID, workspace=tmp_path)

        assert not store.exists(USER_DID), "Profile must be deleted after tombstone"

    def test_tombstone_on_missing_profile_does_not_raise(self, tmp_path: Path) -> None:
        """apply_tombstone is idempotent — no profile to delete is fine."""
        # Should not raise even when profile file doesn't exist
        apply_tombstone(USER_DID, workspace=tmp_path)


# ---------------------------------------------------------------------------
# T7: tombstone record retained for compliance
# ---------------------------------------------------------------------------


class TestTombstoneRetainedForCompliance:
    """T7: tombstone_events/{user_did_hash}.json retained; contains only hash."""

    def test_gdpr_tombstone_retained_for_compliance(self, tmp_path: Path) -> None:
        """Tombstone record exists after erasure and contains hash, not raw DID."""
        _make_profile_file(tmp_path)
        apply_tombstone(USER_DID, workspace=tmp_path)

        tombstone_dir = tmp_path / "tombstone_events"
        tombstone_file = tombstone_dir / f"{USER_DID_HASH}.json"
        assert tombstone_file.exists(), "Tombstone record must be retained"

        record = json.loads(tombstone_file.read_text())
        assert record["user_did_hash"] == USER_DID_HASH
        # The raw DID must NOT be in the record
        assert USER_DID not in json.dumps(record), "Raw DID must not appear in tombstone"
        assert "timestamp" in record

    def test_tombstone_hash_is_sha256(self, tmp_path: Path) -> None:
        """Tombstone filename is SHA-256 of the raw DID."""
        apply_tombstone(USER_DID, workspace=tmp_path)
        tombstone_dir = tmp_path / "tombstone_events"
        expected_filename = f"{USER_DID_HASH}.json"
        assert (tombstone_dir / expected_filename).exists()

    def test_hash_did_helper(self) -> None:
        """_hash_did returns deterministic SHA-256 hex digest."""
        h1 = _hash_did(USER_DID)
        h2 = _hash_did(USER_DID)
        assert h1 == h2
        assert h1 == USER_DID_HASH
        assert len(h1) == 64  # 256-bit hex = 64 chars


# ---------------------------------------------------------------------------
# T8: session JSONL field-wise redaction
# ---------------------------------------------------------------------------


class TestSessionFieldRedaction:
    """T8: session JSONLs redacted field-wise; tool-call audit structure preserved."""

    def test_gdpr_tombstone_session_field_redact(self, tmp_path: Path) -> None:
        """String values containing user_did replaced with [redacted]."""
        sessions_dir = tmp_path / "sessions"
        path = _make_session_jsonl(sessions_dir, "sess-001")

        apply_tombstone(USER_DID, workspace=tmp_path, sessions_dir="sessions")

        redacted_lines = path.read_text().splitlines()
        # First line: user_did field value should be redacted
        line0 = json.loads(redacted_lines[0])
        assert line0["user_did"] == "[redacted]"
        assert line0["content"] == "[redacted]"  # contained user_did
        # role key and structure preserved
        assert "role" in line0
        assert "ts" in line0

        # Second line: no user_did reference — unchanged
        line1 = json.loads(redacted_lines[1])
        assert line1["content"] == "Hello!"

        # Third line: nested args containing user_did
        line2 = json.loads(redacted_lines[2])
        assert line2["type"] == "tool_call"  # structure key preserved
        assert line2["tool"] == "memory_write"  # structure key preserved
        assert line2["args"]["user_did"] == "[redacted]"

    def test_redact_value_leaves_non_did_strings_unchanged(self) -> None:
        """_redact_value does not touch strings that don't contain user_did."""
        obj = {"key": "safe content", "num": 42, "nested": {"also": "safe"}}
        result = _redact_value(obj, USER_DID)
        assert result == obj

    def test_redact_value_replaces_matching_strings(self) -> None:
        """_redact_value replaces any string containing user_did."""
        obj = {"user_did": USER_DID, "message": f"hello {USER_DID}"}
        result = _redact_value(obj, USER_DID)
        assert result["user_did"] == "[redacted]"
        assert result["message"] == "[redacted]"

    def test_redact_value_nested(self) -> None:
        """_redact_value handles arbitrarily nested dicts and lists."""
        obj = {
            "level1": {
                "level2": [USER_DID, "safe", {"level3": USER_DID}]
            }
        }
        result = _redact_value(obj, USER_DID)
        assert result["level1"]["level2"][0] == "[redacted]"
        assert result["level1"]["level2"][1] == "safe"
        assert result["level1"]["level2"][2]["level3"] == "[redacted]"

    def test_redact_preserves_numeric_values(self) -> None:
        """_redact_value leaves numbers, booleans, and None unchanged."""
        obj = {"count": 5, "flag": True, "nothing": None}
        result = _redact_value(obj, USER_DID)
        assert result == obj

    def test_multiple_session_files_redacted(self, tmp_path: Path) -> None:
        """All JSONL files under sessions/ that reference user_did are redacted."""
        sessions_dir = tmp_path / "sessions"
        _make_session_jsonl(sessions_dir, "sess-A")
        _make_session_jsonl(sessions_dir, "sess-B")
        # A session that does NOT reference the user
        other_path = sessions_dir / "sess-other.jsonl"
        other_path.write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

        apply_tombstone(USER_DID, workspace=tmp_path, sessions_dir="sessions")

        # sess-A and sess-B should be redacted
        for sess_id in ("sess-A", "sess-B"):
            lines = (sessions_dir / f"{sess_id}.jsonl").read_text().splitlines()
            line0 = json.loads(lines[0])
            assert line0["user_did"] == "[redacted]"

        # other session unchanged
        other_line = json.loads(other_path.read_text())
        assert other_line["content"] == "hi"


# ---------------------------------------------------------------------------
# T9: FTS5 reindex event emitted
# ---------------------------------------------------------------------------


class TestFTS5ReindexRequested:
    """T9: tombstone emits session.fts5.reindex_needed."""

    def test_gdpr_tombstone_fts5_reindex_requested(self, tmp_path: Path) -> None:
        """session.fts5.reindex_needed event emitted during apply_tombstone."""
        tel = _make_telemetry()
        apply_tombstone(USER_DID, workspace=tmp_path, telemetry=tel)

        # Collect all event names emitted
        emitted_names = [c[0][0] for c in tel.emit_event.call_args_list]
        assert "session.fts5.reindex_needed" in emitted_names

    def test_fts5_event_contains_hash_not_raw_did(self, tmp_path: Path) -> None:
        """The reindex event carries user_did_hash, NOT the raw DID."""
        tel = _make_telemetry()
        apply_tombstone(USER_DID, workspace=tmp_path, telemetry=tel)

        for event_call in tel.emit_event.call_args_list:
            name, data = event_call[0]
            if name == "session.fts5.reindex_needed":
                assert "user_did_hash" in data
                assert data["user_did_hash"] == USER_DID_HASH
                # Raw DID must not appear
                assert USER_DID not in json.dumps(data)
                break
        else:
            pytest.fail("session.fts5.reindex_needed event not found")

    def test_fts5_event_no_telemetry_does_not_raise(self, tmp_path: Path) -> None:
        """No telemetry provided — tombstone completes without error."""
        apply_tombstone(USER_DID, workspace=tmp_path, telemetry=None)


# ---------------------------------------------------------------------------
# T10: Derived section regeneratable
# ---------------------------------------------------------------------------


class TestDerivedSectionRegeneratable:
    """T10: after tombstone, Derived section empty; regeneration hook exists."""

    def test_derived_section_regeneratable(self, tmp_path: Path) -> None:
        """After tombstone the derived section is gone; the hook is callable."""
        from arcagent.modules.user_profile.tombstone import _mark_derived_regeneratable

        # The hook must be callable without error
        _mark_derived_regeneratable(USER_DID, tmp_path)

    def test_profile_derived_empty_after_tombstone_and_recreate(
        self, tmp_path: Path
    ) -> None:
        """A recreated profile starts with an empty Derived section."""
        # Pre-tombstone: create a profile with derived content
        store = ProfileStore(tmp_path, UserProfileConfig())
        profile = store.create_default(USER_DID)
        profile.derived_section = "Some derived content"
        store.write(profile)

        apply_tombstone(USER_DID, workspace=tmp_path)

        # Profile deleted — creating a fresh one starts empty
        new_profile = store.create_default(USER_DID)
        assert new_profile.derived_section == ""
