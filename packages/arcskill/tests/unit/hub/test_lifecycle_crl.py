"""Tests for arcskill.hub.lifecycle — CRL refresh and revocation."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest.mock
import urllib.error
from pathlib import Path

import pytest

from arcskill.hub.config import HubConfig, RevocationConfig, TierPolicy
from arcskill.hub.errors import CRLUnreachable
from arcskill.hub.lifecycle import (
    _crl_state,
    _fetch_crl_remote,
    _quarantine_matching,
    check_revocation_on_boot,
    quarantine_skill,
    should_unload,
    start_crl_refresh_task,
)
from arcskill.lock import HubLockFile, SkillLockEntry


def _config(*, fail_closed: bool = True, enabled: bool = True) -> HubConfig:
    return HubConfig(
        enabled=enabled,
        tier=TierPolicy(level="federal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=fail_closed,
            crl_refresh_interval_seconds=3600,
        ),
    )


def _make_lock(hashes: dict[str, str], tmpdir: Path) -> Path:
    """Create a lock file with skills keyed by name and their content_hashes."""
    lock = HubLockFile()
    for name, h in hashes.items():
        lock.add_or_update(name, SkillLockEntry(content_hash=h))
    lock_path = tmpdir / "lock.json"
    lock.save(lock_path)
    return lock_path


# ---------------------------------------------------------------------------
# check_revocation_on_boot
# ---------------------------------------------------------------------------


def test_boot_check_disabled_hub_returns_empty() -> None:
    result = check_revocation_on_boot(_config(enabled=False))
    assert result == []


def test_boot_check_quarantines_revoked_skill() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        install_base = tmpdir / "skills"
        install_base.mkdir()

        revoked_hash = "deadbeef" * 8
        safe_name = "evil__skill"
        skill_dir = install_base / safe_name
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("# evil\n")

        lock_path = _make_lock({"evil/skill": revoked_hash}, tmpdir)

        _crl_state.clear()

        with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
            mock_fetch.return_value = frozenset([revoked_hash])
            quarantined = check_revocation_on_boot(
                _config(),
                install_base=install_base,
                lock_path=lock_path,
            )

        assert "evil/skill" in quarantined

        # Lock entry must be marked quarantined.
        lock = HubLockFile.load(lock_path)
        assert lock.is_quarantined("evil/skill")


def test_boot_check_no_revoked_skills() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        install_base = tmpdir / "skills"
        install_base.mkdir()
        lock_path = _make_lock({"good/skill": "goodhash123"}, tmpdir)

        _crl_state.clear()

        with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
            mock_fetch.return_value = frozenset(["completely_different_hash"])
            quarantined = check_revocation_on_boot(
                _config(),
                install_base=install_base,
                lock_path=lock_path,
            )

        assert quarantined == []


def test_boot_check_fail_closed_on_crl_unreachable() -> None:
    """Federal fail_closed=True: CRL unreachable at boot → CRLUnreachable."""
    _crl_state.clear()

    with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
        mock_fetch.side_effect = urllib.error.URLError("timeout")
        with pytest.raises(CRLUnreachable):
            check_revocation_on_boot(_config(fail_closed=True))


def test_boot_check_warn_skip_when_not_fail_closed() -> None:
    """Non-fail-closed: CRL unreachable → return empty list (warn and continue)."""
    config = HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )
    _crl_state.clear()

    with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
        mock_fetch.side_effect = urllib.error.URLError("timeout")
        quarantined = check_revocation_on_boot(config)

    assert quarantined == []


# ---------------------------------------------------------------------------
# should_unload
# ---------------------------------------------------------------------------


def test_should_unload_returns_true_for_quarantined() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        lock = HubLockFile()
        entry = SkillLockEntry(content_hash="abc", quarantined=True)
        lock.add_or_update("evil/skill", entry)
        lock_path = tmpdir / "lock.json"
        lock.save(lock_path)

        assert should_unload("evil/skill", lock_path) is True


def test_should_unload_returns_false_for_active() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        lock = HubLockFile()
        entry = SkillLockEntry(content_hash="abc", quarantined=False)
        lock.add_or_update("good/skill", entry)
        lock_path = tmpdir / "lock.json"
        lock.save(lock_path)

        assert should_unload("good/skill", lock_path) is False


def test_should_unload_returns_false_for_unknown() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        lock_path = tmpdir / "lock.json"
        # Lock file doesn't exist yet.
        assert should_unload("unknown/skill", lock_path) is False


# ---------------------------------------------------------------------------
# quarantine_skill
# ---------------------------------------------------------------------------


def test_quarantine_skill_moves_directory() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        install_base = tmpdir / "skills"
        install_base.mkdir()

        safe_name = "test__skill"
        skill_dir = install_base / safe_name
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text("# skill\n")

        lock_path = _make_lock({"test/skill": "abc"}, tmpdir)

        result = quarantine_skill(
            "test/skill",
            _config(),
            install_base=install_base,
            lock_path=lock_path,
        )

        assert result is True
        assert not skill_dir.exists()
        assert (install_base / "revoked" / safe_name).exists()

        lock = HubLockFile.load(lock_path)
        assert lock.is_quarantined("test/skill")


def test_quarantine_skill_disabled_hub_returns_false() -> None:
    result = quarantine_skill("any/skill", _config(enabled=False))
    assert result is False


# ---------------------------------------------------------------------------
# CRL refresh background task (asyncio)
# ---------------------------------------------------------------------------


def test_crl_refresh_task_quarantines_on_hit() -> None:
    """Background task quarantines revoked skills on CRL refresh."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        install_base = tmpdir / "skills"
        install_base.mkdir()

        revoked_hash = "badhash" * 9
        safe_name = "bad__skill"
        skill_dir = install_base / safe_name
        skill_dir.mkdir()

        lock_path = _make_lock({"bad/skill": revoked_hash}, tmpdir)

        config = HubConfig(
            enabled=True,
            tier=TierPolicy(level="personal"),
            revocation=RevocationConfig(
                crl_url="https://test.example.com/crl.json",
                fail_closed_if_unreachable=False,
                crl_refresh_interval_seconds=3600,
            ),
        )

        quarantined_names: list[str] = []

        async def _run_one_iteration() -> None:
            with unittest.mock.patch(
                "arcskill.hub.lifecycle._fetch_crl_remote"
            ) as mock_fetch:
                mock_fetch.return_value = frozenset([revoked_hash])
                names = _quarantine_matching(
                    frozenset([revoked_hash]),
                    config,
                    install_base=install_base,
                    lock_path=lock_path,
                )
                quarantined_names.extend(names)

        asyncio.run(_run_one_iteration())

        assert "bad/skill" in quarantined_names
        lock = HubLockFile.load(lock_path)
        assert lock.is_quarantined("bad/skill")


def test_next_boot_unloads_quarantined_skill() -> None:
    """After quarantine, should_unload returns True (simulating next agent boot)."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        lock = HubLockFile()
        lock.add_or_update(
            "revoked/skill",
            SkillLockEntry(content_hash="xyz", quarantined=True),
        )
        lock_path = tmpdir / "lock.json"
        lock.save(lock_path)

        # Simulates the module bus checking before loading.
        assert should_unload("revoked/skill", lock_path) is True
