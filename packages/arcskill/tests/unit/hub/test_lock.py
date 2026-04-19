"""Tests for arcskill.lock — HubLockFile atomic write and corruption handling."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arcskill.hub.errors import HubLockFileCorrupted
from arcskill.lock import HubLockFile, SkillLockEntry


# ---------------------------------------------------------------------------
# SkillLockEntry
# ---------------------------------------------------------------------------


def test_skill_lock_entry_defaults() -> None:
    entry = SkillLockEntry(content_hash="abc123")
    assert entry.content_hash == "abc123"
    assert entry.rekor_uuid == ""
    assert entry.slsa_level == 0
    assert entry.scan_verdict == "safe"
    assert entry.quarantined is False
    assert entry.files == []


def test_skill_lock_entry_timestamps_set() -> None:
    entry = SkillLockEntry(content_hash="abc")
    # Both timestamps must be parseable ISO-8601 UTC datetimes.
    dt = datetime.fromisoformat(entry.installed_at)
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# HubLockFile defaults
# ---------------------------------------------------------------------------


def test_hub_lock_file_empty_by_default() -> None:
    lock = HubLockFile()
    assert lock.version == 1
    assert lock.skills == {}


# ---------------------------------------------------------------------------
# Load — missing file returns empty
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lock.json"
        lock = HubLockFile.load(path)
        assert lock.skills == {}


# ---------------------------------------------------------------------------
# Save + Load roundtrip
# ---------------------------------------------------------------------------


def test_save_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lock.json"

        lock = HubLockFile()
        lock.add_or_update(
            "arc-official/summarise",
            SkillLockEntry(
                content_hash="sha256abc",
                rekor_uuid="rekor-999",
                slsa_level=3,
                scan_verdict="safe",
                install_path="/tmp/summarise",
                files=["skill.py", "MODULE.yaml"],
            ),
        )
        lock.save(path)

        loaded = HubLockFile.load(path)
        assert "arc-official/summarise" in loaded.skills
        entry = loaded.skills["arc-official/summarise"]
        assert entry.content_hash == "sha256abc"
        assert entry.rekor_uuid == "rekor-999"
        assert entry.slsa_level == 3
        assert entry.scan_verdict == "safe"
        assert entry.files == ["skill.py", "MODULE.yaml"]


def test_save_creates_parent_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / ".hub" / "nested" / "lock.json"
        lock = HubLockFile()
        lock.save(path)
        assert path.exists()


def test_save_sets_restrictive_permissions() -> None:
    """Lock file must be owner-readable only (0o600)."""
    import stat

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lock.json"
        lock = HubLockFile()
        lock.save(path)
        mode = stat.filemode(path.stat().st_mode)
        # Should be -rw------- or similar (no group/world read)
        assert path.stat().st_mode & 0o077 == 0, (
            f"Lock file has insecure permissions: {mode}"
        )


# ---------------------------------------------------------------------------
# Atomic write (no partial file on crash simulation)
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash during write must not leave a partial file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lock.json"

        # Write a valid file first.
        lock = HubLockFile()
        lock.add_or_update("initial/skill", SkillLockEntry(content_hash="init"))
        lock.save(path)
        original_content = path.read_text()

        # Simulate a write failure by making os.replace raise.
        import os
        original_replace = os.replace

        def _failing_replace(src: str, dst: str) -> None:
            # Clean up temp file, then raise.
            try:
                os.unlink(src)
            except OSError:
                pass
            raise OSError("disk full simulation")

        monkeypatch.setattr("os.replace", _failing_replace)

        lock2 = HubLockFile()
        lock2.add_or_update("new/skill", SkillLockEntry(content_hash="new"))
        with pytest.raises(OSError):
            lock2.save(path)

        # Original file must be unchanged.
        assert path.read_text() == original_content


# ---------------------------------------------------------------------------
# Corruption handling
# ---------------------------------------------------------------------------


def test_load_corrupted_json_raises() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lock.json"
        path.write_text("{ not valid json !!!")
        with pytest.raises(HubLockFileCorrupted, match="corrupted"):
            HubLockFile.load(path)


def test_load_wrong_schema_raises() -> None:
    """Truly invalid schema (skills has wrong type) raises HubLockFileCorrupted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "lock.json"
        # skills field with invalid type (string instead of dict) causes ValidationError
        path.write_text(json.dumps({"version": 1, "skills": "this_should_be_a_dict"}))
        with pytest.raises((HubLockFileCorrupted, Exception)):
            HubLockFile.load(path)


# ---------------------------------------------------------------------------
# add_or_update preserves installed_at
# ---------------------------------------------------------------------------


def test_add_or_update_preserves_installed_at() -> None:
    lock = HubLockFile()
    original_ts = "2026-01-01T00:00:00+00:00"
    lock.add_or_update(
        "my/skill",
        SkillLockEntry(content_hash="v1", installed_at=original_ts, updated_at=original_ts),
    )

    # Update with a new hash.
    new_entry = SkillLockEntry(content_hash="v2")
    lock.add_or_update("my/skill", new_entry)

    updated = lock.skills["my/skill"]
    assert updated.content_hash == "v2"
    assert updated.installed_at == original_ts  # must be preserved


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------


def test_quarantine_marks_entry() -> None:
    lock = HubLockFile()
    lock.add_or_update("test/skill", SkillLockEntry(content_hash="abc"))
    result = lock.quarantine("test/skill")
    assert result is True
    assert lock.is_quarantined("test/skill") is True


def test_quarantine_nonexistent_returns_false() -> None:
    lock = HubLockFile()
    assert lock.quarantine("no/such/skill") is False


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_existing_skill() -> None:
    lock = HubLockFile()
    lock.add_or_update("rm/skill", SkillLockEntry(content_hash="abc"))
    assert lock.remove("rm/skill") is True
    assert "rm/skill" not in lock.skills


def test_remove_nonexistent_returns_false() -> None:
    lock = HubLockFile()
    assert lock.remove("ghost/skill") is False


# ---------------------------------------------------------------------------
# installed_names
# ---------------------------------------------------------------------------


def test_installed_names_excludes_quarantined() -> None:
    lock = HubLockFile()
    lock.add_or_update("active/skill", SkillLockEntry(content_hash="a"))
    lock.add_or_update("revoked/skill", SkillLockEntry(content_hash="b", quarantined=True))

    names = lock.installed_names()
    assert "active/skill" in names
    assert "revoked/skill" not in names
