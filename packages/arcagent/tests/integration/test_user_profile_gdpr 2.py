"""End-to-end GDPR tombstone integration test.

Scenario:
  1. Write a user profile with identity, preferences, and durable facts.
  2. Create session JSONL files referencing the user's DID.
  3. Call apply_tombstone(user_did).
  4. Assert:
     a. Profile file deleted.
     b. Tombstone record retained (hash only).
     c. Session JSONLs redacted field-wise (tool-call structure preserved).
     d. FTS5 reindex event emitted.
     e. Derived section can be flagged for regeneration.

This is the KEY compliance deliverable for SPEC-018 M2 / T2.4.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.models import ACL, DurableFact, UserProfile
from arcagent.modules.user_profile.store import ProfileStore
from arcagent.modules.user_profile.tombstone import apply_tombstone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_DID = "did:arc:user:human/e2e-gdpr-test-user"
AGENT_DID = "did:arc:agent/test-agent-001"
USER_DID_HASH = hashlib.sha256(USER_DID.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def tel() -> MagicMock:
    mock = MagicMock()
    mock.emit_event = MagicMock()
    return mock


@pytest.fixture()
def populated_workspace(workspace: Path) -> Path:
    """Create a workspace with a full profile + session JSONL files."""
    config = UserProfileConfig()
    store = ProfileStore(workspace, config)

    # Create profile with all sections populated
    profile = UserProfile(
        user_did=USER_DID,
        created=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
        classification="unclassified",
        acl=ACL(owner=USER_DID),
        schema_version=1,
        identity_section="Alice Engineer, Staff at ArcLabs",
        preferences_section="Prefers concise bullet summaries. Uses Mountain Time.",
        durable_facts=[
            DurableFact(
                content="Works primarily in Python and Go",
                source_session_id="sess-001",
                ts=datetime(2026, 4, 18, 10, 1, 0, tzinfo=UTC),
            ),
            DurableFact(
                content="Has a PhD in ML from Stanford",
                source_session_id="sess-002",
                ts=datetime(2026, 4, 18, 10, 2, 0, tzinfo=UTC),
            ),
        ],
        derived_section="Expert in distributed systems. Responds well to analogies.",
    )
    store.write(profile)

    # Create session JSONL files
    sessions_dir = workspace / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session1 = sessions_dir / "sess-001.jsonl"
    session1.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "user_did": USER_DID,
                        "content": f"Hello, I am {USER_DID}",
                        "ts": "2026-04-18T10:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "Hello Alice!",
                        "ts": "2026-04-18T10:00:01Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "tool": "memory_write",
                        "args": {
                            "user_did": USER_DID,
                            "content": "Works in Python and Go",
                        },
                        "result": "ok",
                        "ts": "2026-04-18T10:00:02Z",
                    }
                ),
            ]
        )
        + "\n"
    )

    session2 = sessions_dir / "sess-002.jsonl"
    session2.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "user_did": USER_DID,
                        "content": "Follow-up question",
                        "ts": "2026-04-18T11:00:00Z",
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_call",
                        "tool": "profile_read",
                        "args": {"user_did": USER_DID},
                        "result": "profile_content",
                        "ts": "2026-04-18T11:00:01Z",
                    }
                ),
            ]
        )
        + "\n"
    )

    # An unrelated session (different user, must remain untouched)
    other_did = "did:arc:user:human/unrelated-user"
    other_session = sessions_dir / "sess-other.jsonl"
    other_session.write_text(
        json.dumps(
            {
                "role": "user",
                "user_did": other_did,
                "content": "Unrelated content",
            }
        )
        + "\n"
    )

    return workspace


# ---------------------------------------------------------------------------
# Integration test: full GDPR tombstone E2E
# ---------------------------------------------------------------------------


class TestGDPRTombstoneE2E:
    """End-to-end: write profile → apply_tombstone → verify all guarantees."""

    def test_e2e_tombstone_profile_deleted(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Step 1: Profile file must not exist after tombstone."""
        store = ProfileStore(populated_workspace, UserProfileConfig())
        assert store.exists(USER_DID), "Pre-condition: profile exists"

        apply_tombstone(USER_DID, workspace=populated_workspace, telemetry=tel)

        assert not store.exists(USER_DID), "Post-condition: profile deleted"

    def test_e2e_tombstone_record_retained(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Step 2: Tombstone record persisted with hash only."""
        apply_tombstone(USER_DID, workspace=populated_workspace, telemetry=tel)

        tombstone_dir = populated_workspace / "tombstone_events"
        record_path = tombstone_dir / f"{USER_DID_HASH}.json"
        assert record_path.exists(), "Tombstone record must be retained"

        record = json.loads(record_path.read_text())
        assert record["user_did_hash"] == USER_DID_HASH
        # Raw DID must NOT appear anywhere in the record
        assert USER_DID not in json.dumps(record), "Raw DID must not be in tombstone"
        assert "timestamp" in record
        assert "sessions_redacted" in record

    def test_e2e_session_field_redaction(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Step 3: Session JSONLs redacted field-wise; audit structure preserved."""
        sessions_dir = populated_workspace / "sessions"
        apply_tombstone(
            USER_DID,
            workspace=populated_workspace,
            telemetry=tel,
            sessions_dir="sessions",
        )

        # sess-001.jsonl
        sess1_lines = (sessions_dir / "sess-001.jsonl").read_text().splitlines()
        # Line 0: user message
        line0 = json.loads(sess1_lines[0])
        assert line0["user_did"] == "[redacted]"
        assert line0["content"] == "[redacted]"  # contained USER_DID
        assert "role" in line0  # key preserved
        assert "ts" in line0   # key preserved

        # Line 1: assistant message (no user_did reference)
        line1 = json.loads(sess1_lines[1])
        assert line1["content"] == "Hello Alice!"  # untouched
        assert "role" in line1

        # Line 2: tool_call — structure preserved, user_did args redacted
        line2 = json.loads(sess1_lines[2])
        assert line2["type"] == "tool_call"       # structure key preserved
        assert line2["tool"] == "memory_write"    # structure key preserved
        assert line2["args"]["user_did"] == "[redacted]"
        assert line2["result"] == "ok"            # non-DID value unchanged

        # sess-002.jsonl
        sess2_lines = (sessions_dir / "sess-002.jsonl").read_text().splitlines()
        line0_s2 = json.loads(sess2_lines[0])
        assert line0_s2["user_did"] == "[redacted]"

    def test_e2e_unrelated_session_untouched(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Unrelated session (different user) must not be modified."""
        sessions_dir = populated_workspace / "sessions"
        original = (sessions_dir / "sess-other.jsonl").read_text()

        apply_tombstone(
            USER_DID,
            workspace=populated_workspace,
            telemetry=tel,
            sessions_dir="sessions",
        )

        after = (sessions_dir / "sess-other.jsonl").read_text()
        assert after == original, "Unrelated session must not be modified"

    def test_e2e_fts5_reindex_event_emitted(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Step 4: session.fts5.reindex_needed emitted with hash."""
        apply_tombstone(USER_DID, workspace=populated_workspace, telemetry=tel)

        emitted_names = [c[0][0] for c in tel.emit_event.call_args_list]
        assert "session.fts5.reindex_needed" in emitted_names

        # Verify event data
        for event_call in tel.emit_event.call_args_list:
            name, data = event_call[0]
            if name == "session.fts5.reindex_needed":
                assert data["user_did_hash"] == USER_DID_HASH
                assert USER_DID not in json.dumps(data)
                break

    def test_e2e_derived_hook_callable(self, populated_workspace: Path) -> None:
        """Step 5: _mark_derived_regeneratable hook is callable without error."""
        from arcagent.modules.user_profile.tombstone import _mark_derived_regeneratable

        # Must not raise
        _mark_derived_regeneratable(USER_DID, populated_workspace)

    def test_e2e_tombstone_count_reported(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Tombstone record includes the number of sessions redacted."""
        apply_tombstone(
            USER_DID,
            workspace=populated_workspace,
            telemetry=tel,
            sessions_dir="sessions",
        )

        tombstone_dir = populated_workspace / "tombstone_events"
        record = json.loads(
            (tombstone_dir / f"{USER_DID_HASH}.json").read_text()
        )
        # 2 sessions (sess-001 and sess-002) reference the user
        assert record["sessions_redacted"] == 2

    def test_e2e_idempotent_second_tombstone(
        self, populated_workspace: Path, tel: MagicMock
    ) -> None:
        """Applying tombstone twice does not raise."""
        apply_tombstone(USER_DID, workspace=populated_workspace, telemetry=tel)
        # Second call — no profile file exists anymore
        apply_tombstone(USER_DID, workspace=populated_workspace, telemetry=tel)
        # Still only one tombstone record
        tombstone_dir = populated_workspace / "tombstone_events"
        records = list(tombstone_dir.glob("*.json"))
        assert len(records) == 1
