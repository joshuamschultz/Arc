"""Tests for CandidateStore — save, load, seed snapshot, audit log, rollback."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.models import Candidate, MutationEvent


def _make_candidate(
    cid: str = "c1",
    text: str = "# Skill\nDo stuff",
    parent_id: str | None = None,
    generation: int = 0,
) -> Candidate:
    return Candidate(
        id=cid,
        text=text,
        aggregate_scores={"accuracy": 3.5},
        token_count=len(text.split()),
        parent_id=parent_id,
        generation=generation,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def store(workspace: Path) -> CandidateStore:
    return CandidateStore(workspace)


class TestSaveAndLoad:
    """F6: Save, load, get frontier, get active, manifest."""

    def test_save_candidate(self, store: CandidateStore) -> None:
        c = _make_candidate("c1")
        store.save("test-skill", c)
        loaded = store.load("test-skill", "c1")
        assert loaded is not None
        assert loaded.id == "c1"

    def test_load_nonexistent(self, store: CandidateStore) -> None:
        loaded = store.load("test-skill", "aabbccddee")
        assert loaded is None

    def test_get_active(self, store: CandidateStore) -> None:
        c = _make_candidate("c1")
        store.save("test-skill", c, active=True)
        active = store.get_active("test-skill")
        assert active is not None
        assert active.id == "c1"

    def test_manifest_updated(self, store: CandidateStore) -> None:
        c = _make_candidate("c1")
        store.save("test-skill", c, active=True)
        manifest = store.load_manifest("test-skill")
        assert manifest is not None
        assert manifest["active_candidate_id"] == "c1"

    def test_frontier_ids(self, store: CandidateStore) -> None:
        c1 = _make_candidate("c1")
        c2 = _make_candidate("c2")
        store.save("test-skill", c1, active=True, frontier=True)
        store.save("test-skill", c2, frontier=True)
        manifest = store.load_manifest("test-skill")
        assert "c1" in manifest["frontier"]
        assert "c2" in manifest["frontier"]


class TestSeedSnapshot:
    """F7: Saved on first optimization, never modified."""

    def test_save_seed(self, store: CandidateStore) -> None:
        store.save_seed("test-skill", "# Original skill text\nDo the thing.")
        seed_path = store._skill_dir("test-skill") / "candidates" / "seed.md"
        assert seed_path.exists()
        assert "Original skill text" in seed_path.read_text()

    def test_seed_not_overwritten(self, store: CandidateStore) -> None:
        store.save_seed("test-skill", "First version")
        store.save_seed("test-skill", "Second version")
        seed_path = store._skill_dir("test-skill") / "candidates" / "seed.md"
        assert "First version" in seed_path.read_text()

    def test_load_seed(self, store: CandidateStore) -> None:
        store.save_seed("test-skill", "# Seed text")
        text = store.load_seed("test-skill")
        assert text == "# Seed text"

    def test_load_seed_nonexistent(self, store: CandidateStore) -> None:
        text = store.load_seed("nonexistent")
        assert text is None


class TestAuditLog:
    """F8: Append-only JSONL, MutationEvent serialization."""

    def test_append_audit(self, store: CandidateStore) -> None:
        event = MutationEvent(
            timestamp=datetime(2026, 2, 25, 14, 0, 0, tzinfo=UTC),
            skill_name="test-skill",
            previous_hash="aaa",
            new_hash="bbb",
            candidate_id="c1",
            generation=1,
            scores={"accuracy": 4.0},
            improvement={"accuracy": 0.5},
            stop_reason="stagnation",
            trace_ids=["t1"],
        )
        store.append_audit("test-skill", event)

        audit_path = store._skill_dir("test-skill") / "audit.jsonl"
        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["candidate_id"] == "c1"

    def test_audit_append_only(self, store: CandidateStore) -> None:
        for i in range(3):
            event = MutationEvent(
                timestamp=datetime(2026, 2, 25, 14, i, 0, tzinfo=UTC),
                skill_name="test-skill",
                previous_hash="a",
                new_hash="b",
                candidate_id=f"c{i}",
                generation=i,
                scores={},
                improvement={},
                stop_reason="max_iterations",
                trace_ids=[],
            )
            store.append_audit("test-skill", event)

        audit_path = store._skill_dir("test-skill") / "audit.jsonl"
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 3


class TestRollback:
    """F9: Activate previous candidate, set cooloff."""

    def test_rollback_changes_active(self, store: CandidateStore) -> None:
        c1 = _make_candidate("c1", text="Version 1")
        c2 = _make_candidate("c2", text="Version 2", parent_id="c1", generation=1)
        store.save("test-skill", c1)
        store.save("test-skill", c2, active=True)

        assert store.get_active("test-skill").id == "c2"  # type: ignore[union-attr]
        store.rollback("test-skill", "c1")
        assert store.get_active("test-skill").id == "c1"  # type: ignore[union-attr]

    def test_rollback_nonexistent_candidate(self, store: CandidateStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.rollback("test-skill", "aabbccddee")

    def test_rollback_updates_manifest(self, store: CandidateStore) -> None:
        c1 = _make_candidate("c1")
        store.save("test-skill", c1, active=True)
        c2 = _make_candidate("c2", parent_id="c1", generation=1)
        store.save("test-skill", c2, active=True)

        store.rollback("test-skill", "c1")
        manifest = store.load_manifest("test-skill")
        assert manifest["active_candidate_id"] == "c1"


class TestPathTraversalDefense:
    """C-1/C-2: Reject path traversal via skill_name and candidate_id (ASI-02)."""

    def test_reject_path_traversal_skill_name(self, store: CandidateStore) -> None:
        with pytest.raises(ValueError, match="Invalid skill name"):
            store.load_manifest("../../../etc/passwd")

    def test_reject_dotdot_skill_name(self, store: CandidateStore) -> None:
        with pytest.raises(ValueError, match="Invalid skill name"):
            store.load_manifest("..secret")

    def test_reject_invalid_candidate_id(self, store: CandidateStore) -> None:
        with pytest.raises(ValueError, match="Invalid candidate ID"):
            store.load("test-skill", "../escape")

    def test_reject_nonhex_candidate_id(self, store: CandidateStore) -> None:
        with pytest.raises(ValueError, match="Invalid candidate ID"):
            store.load("test-skill", "nonexistent")

    def test_accept_valid_skill_name(self, store: CandidateStore) -> None:
        # Should not raise — alphanumeric with dots, dashes, underscores
        manifest = store.load_manifest("my-skill.v2_test")
        assert manifest["skill_name"] == "my-skill.v2_test"

    def test_accept_seed_candidate_id(self, store: CandidateStore) -> None:
        # "seed" is explicitly allowed
        result = store.load("test-skill", "seed")
        assert result is None  # No file, but no validation error

    def test_accept_hex_candidate_id(self, store: CandidateStore) -> None:
        result = store.load("test-skill", "abcdef012345")
        assert result is None  # No file, but no validation error
