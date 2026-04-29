"""Integration tests — revocation lifecycle (G4.6).

Simulates:
1. Skill installed and active.
2. CRL updated with that skill's content_hash.
3. Next agent start detects the revocation → skill quarantined.
4. Module bus check confirms skill should not load.
"""

from __future__ import annotations

import tempfile
import unittest.mock
from pathlib import Path

import pytest
from arcskill.hub.config import HubConfig, RevocationConfig, TierPolicy
from arcskill.hub.lifecycle import (
    _crl_state,
    check_revocation_on_boot,
    should_unload,
)
from arcskill.lock import HubLockFile, SkillLockEntry


def _personal_config(*, fail_closed: bool = False) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        revocation=RevocationConfig(
            crl_url="https://test.skills.arcagent.dev/v1/crl.json",
            fail_closed_if_unreachable=fail_closed,
            crl_refresh_interval_seconds=3600,
        ),
    )


def _federal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        revocation=RevocationConfig(
            crl_url="https://test.skills.arcagent.dev/v1/crl.json",
            fail_closed_if_unreachable=True,
            crl_refresh_interval_seconds=3600,
        ),
    )


# ---------------------------------------------------------------------------
# G4.6: CRL update → next-start quarantine
# ---------------------------------------------------------------------------


class TestRevocationLifecycle:
    def test_installed_skill_quarantined_on_crl_hit(self) -> None:
        """Skill installed → CRL updated with its hash → next boot quarantines it."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            install_base = tmpdir / "skills"
            install_base.mkdir()

            skill_hash = "cafecafe" * 8
            safe_name = "arc__summarise"
            skill_dir = install_base / safe_name
            skill_dir.mkdir()
            (skill_dir / "skill.py").write_text("def run(t): return t\n")

            # Simulate installed state in lock file.
            lock = HubLockFile()
            lock.add_or_update(
                "arc/summarise",
                SkillLockEntry(
                    content_hash=skill_hash,
                    install_path=str(skill_dir),
                    scan_verdict="safe",
                    slsa_level=3,
                ),
            )
            lock_path = tmpdir / "lock.json"
            lock.save(lock_path)

            # Verify skill is active before revocation.
            assert not lock.is_quarantined("arc/summarise")

            # Simulate CRL being updated to include this skill's hash.
            _crl_state.clear()
            with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
                mock_fetch.return_value = frozenset([skill_hash])
                quarantined = check_revocation_on_boot(
                    _personal_config(),
                    install_base=install_base,
                    lock_path=lock_path,
                )

            # Skill must be quarantined.
            assert "arc/summarise" in quarantined

            # Lock file must reflect quarantine.
            updated_lock = HubLockFile.load(lock_path)
            assert updated_lock.is_quarantined("arc/summarise")

            # Skill directory should have been moved to revoked/.
            assert not skill_dir.exists()
            assert (install_base / "revoked" / safe_name).exists()

    def test_next_boot_should_not_load_quarantined(self) -> None:
        """After quarantine, should_unload returns True for module bus."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            lock_path = tmpdir / "lock.json"

            # Pre-populate lock as quarantined (simulates post-revocation state).
            lock = HubLockFile()
            lock.add_or_update(
                "arc/summarise",
                SkillLockEntry(
                    content_hash="cafecafe" * 8,
                    quarantined=True,
                ),
            )
            lock.save(lock_path)

            # Module bus calls should_unload before loading any skill module.
            assert should_unload("arc/summarise", lock_path) is True
            assert should_unload("other/skill", lock_path) is False

    def test_non_revoked_skill_stays_active(self) -> None:
        """Skills not in the CRL remain active after boot check."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            install_base = tmpdir / "skills"
            install_base.mkdir()

            good_hash = "goodhash" * 8
            bad_hash = "badhash00" * 7 + "0000000"

            lock = HubLockFile()
            lock.add_or_update(
                "arc/good-skill",
                SkillLockEntry(content_hash=good_hash, install_path=str(install_base / "good")),
            )
            lock_path = tmpdir / "lock.json"
            lock.save(lock_path)

            _crl_state.clear()
            with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
                mock_fetch.return_value = frozenset([bad_hash])  # different hash
                quarantined = check_revocation_on_boot(
                    _personal_config(),
                    install_base=install_base,
                    lock_path=lock_path,
                )

            assert quarantined == []
            updated_lock = HubLockFile.load(lock_path)
            assert not updated_lock.is_quarantined("arc/good-skill")

    def test_multiple_skills_some_revoked(self) -> None:
        """Only revoked skills are quarantined; others remain active."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            install_base = tmpdir / "skills"
            install_base.mkdir()

            revoked_hash = "revokedhash" * 5
            clean_hash = "cleanhash01" * 5

            # Create skill directories.
            for safe in ("revoked__skill", "clean__skill"):
                d = install_base / safe
                d.mkdir()
                (d / "skill.py").write_text("pass\n")

            lock = HubLockFile()
            lock.add_or_update(
                "revoked/skill",
                SkillLockEntry(
                    content_hash=revoked_hash,
                    install_path=str(install_base / "revoked__skill"),
                ),
            )
            lock.add_or_update(
                "clean/skill",
                SkillLockEntry(
                    content_hash=clean_hash,
                    install_path=str(install_base / "clean__skill"),
                ),
            )
            lock_path = tmpdir / "lock.json"
            lock.save(lock_path)

            _crl_state.clear()
            with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
                mock_fetch.return_value = frozenset([revoked_hash])
                quarantined = check_revocation_on_boot(
                    _personal_config(),
                    install_base=install_base,
                    lock_path=lock_path,
                )

            assert "revoked/skill" in quarantined
            assert "clean/skill" not in quarantined

            updated_lock = HubLockFile.load(lock_path)
            assert updated_lock.is_quarantined("revoked/skill")
            assert not updated_lock.is_quarantined("clean/skill")

    def test_already_quarantined_skill_not_reprocessed(self) -> None:
        """Skills already quarantined are not re-processed on subsequent boots."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            install_base = tmpdir / "skills"
            install_base.mkdir()

            revoked_hash = "alreadyrevoked" * 4

            lock = HubLockFile()
            lock.add_or_update(
                "old/revoked",
                SkillLockEntry(content_hash=revoked_hash, quarantined=True),
            )
            lock_path = tmpdir / "lock.json"
            lock.save(lock_path)

            _crl_state.clear()
            with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
                mock_fetch.return_value = frozenset([revoked_hash])
                quarantined = check_revocation_on_boot(
                    _personal_config(),
                    install_base=install_base,
                    lock_path=lock_path,
                )

            # No new quarantine events (already quarantined).
            assert "old/revoked" not in quarantined


# ---------------------------------------------------------------------------
# Federal tier revocation
# ---------------------------------------------------------------------------


class TestFederalRevocation:
    def test_federal_revocation_on_crl_hit(self) -> None:
        """Federal tier: CRL hit quarantines skill."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            install_base = tmpdir / "skills"
            install_base.mkdir()

            federal_hash = "federalrevoked" * 4
            safe_name = "federal__skill"
            skill_dir = install_base / safe_name
            skill_dir.mkdir()

            lock = HubLockFile()
            lock.add_or_update(
                "federal/skill",
                SkillLockEntry(content_hash=federal_hash),
            )
            lock_path = tmpdir / "lock.json"
            lock.save(lock_path)

            _crl_state.clear()
            with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
                mock_fetch.return_value = frozenset([federal_hash])
                quarantined = check_revocation_on_boot(
                    _federal_config(),
                    install_base=install_base,
                    lock_path=lock_path,
                )

            assert "federal/skill" in quarantined
            assert HubLockFile.load(lock_path).is_quarantined("federal/skill")

    def test_federal_crl_unreachable_hard_fails(self) -> None:
        """Federal with fail_closed=True: CRL unreachable at boot → raises."""
        import urllib.error

        from arcskill.hub.errors import CRLUnreachable

        _crl_state.clear()

        with unittest.mock.patch("arcskill.hub.lifecycle._fetch_crl_remote") as mock_fetch:
            mock_fetch.side_effect = urllib.error.URLError("timeout")
            with pytest.raises(CRLUnreachable):
                check_revocation_on_boot(_federal_config())
