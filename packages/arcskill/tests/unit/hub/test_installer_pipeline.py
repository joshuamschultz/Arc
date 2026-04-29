"""Tests for arcskill.hub.installer — end-to-end quarantine→activate pipeline."""

from __future__ import annotations

import tarfile
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from arcskill.hub.config import (
    FindingsAllowed,
    HubConfig,
    HubPolicy,
    RevocationConfig,
    SkillSource,
    TierPolicy,
)
from arcskill.hub.dry_run import DryRunResult
from arcskill.hub.errors import HubDisabled, SourceNotAllowed
from arcskill.hub.installer import install, uninstall, update
from arcskill.hub.scanner import ScanResult
from arcskill.hub.sources import FetchResult
from arcskill.hub.verify import VerifyResult
from arcskill.lock import HubLockFile


def _make_clean_bundle(tmpdir: Path) -> Path:
    """Create a minimal clean skill bundle."""
    bundle = tmpdir / "clean_skill.tar.gz"
    skill_dir = tmpdir / "skill"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "skill.py").write_text("def run(): pass\n")
    (skill_dir / "MODULE.yaml").write_text("name: test-skill\nversion: '1.0'\n")
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(skill_dir / "skill.py", arcname="skill.py")
        tf.add(skill_dir / "MODULE.yaml", arcname="MODULE.yaml")
    return bundle


def _personal_config(sources: list[SkillSource] | None = None) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        policy=HubPolicy(
            require_signature=False,
            require_slsa_level=0,
            require_scan_pass=True,
            max_findings_allowed=FindingsAllowed(critical=0, high=0, medium=5),
        ),
        sources=sources
        or [
            SkillSource(
                name="local-test",
                type="local",
                trust="local",
            )
        ],
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )


# ---------------------------------------------------------------------------
# Hub disabled
# ---------------------------------------------------------------------------


def test_install_raises_when_hub_disabled() -> None:
    config = HubConfig(enabled=False)
    with pytest.raises(HubDisabled):
        install("test/skill", "local-test", config)


# ---------------------------------------------------------------------------
# Source not allowed
# ---------------------------------------------------------------------------


def test_install_raises_on_unknown_source() -> None:
    config = _personal_config(sources=[])
    with pytest.raises(SourceNotAllowed):
        install("test/skill", "nonexistent-source", config)


# ---------------------------------------------------------------------------
# Full pipeline orchestration (mocked stages)
# ---------------------------------------------------------------------------


def test_full_pipeline_success() -> None:
    """All stages mocked — verify, scan, dry-run, activate, lock-write all run."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_clean_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config = _personal_config()

        # Patch the fetch stage to return a pre-made bundle.
        mock_fetch_result = FetchResult(
            local_path=bundle,
            content_hash="abc123",
            source_name="local-test",
            bundle_url="",
            version="1.0.0",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                return_value=VerifyResult(
                    content_hash="abc123",
                    rekor_uuid="rekor-123",
                    slsa_level=0,
                    signature_valid=True,
                    crl_checked=True,
                    revoked=False,
                ),
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.scan",
                return_value=ScanResult(
                    verdict="safe",
                    findings=[],
                    counts={},
                    scanner_passes=["regex", "ast"],
                ),
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.run_dry_run",
                return_value=DryRunResult(
                    passed=True,
                    exit_code=0,
                    backend_used="docker",
                ),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch_result

            result = install(
                "test/skill",
                "local-test",
                config,
                install_base=install_base,
                lock_path=lock_path,
                skip_sandbox=True,
            )

        assert result.success is True
        assert result.name == "test/skill"
        assert result.install_path is not None

        # Lock file must have been written.
        lock = HubLockFile.load(lock_path)
        assert "test/skill" in lock.skills
        entry = lock.skills["test/skill"]
        assert entry.content_hash == "abc123"
        assert entry.scan_verdict == "safe"


def test_pipeline_fails_on_scan_verdict_dangerous() -> None:
    """Dangerous scan verdict causes install failure."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_clean_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config = _personal_config()

        from arcskill.hub.scanner import Finding

        mock_fetch_result = FetchResult(
            local_path=bundle,
            content_hash="evil123",
            source_name="local-test",
            bundle_url="",
            version="1.0.0",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                return_value=VerifyResult(
                    content_hash="evil123",
                    signature_valid=True,
                    crl_checked=True,
                    revoked=False,
                ),
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.scan",
                return_value=ScanResult(
                    verdict="dangerous",
                    findings=[
                        Finding(
                            severity="critical",
                            category="structural",
                            rule_id="write_claude_md",
                            message="Covert write",
                            path="skill.py",
                            line=5,
                        )
                    ],
                    counts={"critical": 1},
                    scanner_passes=["regex"],
                ),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch_result

            result = install(
                "evil/skill",
                "local-test",
                config,
                install_base=install_base,
                lock_path=lock_path,
                skip_sandbox=True,
            )

        assert result.success is False
        assert "dangerous" in result.error.lower() or "scan" in result.error.lower()

        # Lock file must NOT have this skill.
        lock = HubLockFile.load(lock_path)
        assert "evil/skill" not in lock.skills


def test_pipeline_fails_on_revoked_bundle() -> None:
    """Bundle hash in CRL causes install failure."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_clean_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config = _personal_config()

        mock_fetch_result = FetchResult(
            local_path=bundle,
            content_hash="revoked_hash_abc",
            source_name="local-test",
            bundle_url="",
            version="1.0.0",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                return_value=VerifyResult(
                    content_hash="revoked_hash_abc",
                    signature_valid=True,
                    crl_checked=True,
                    revoked=True,  # ← CRL hit
                ),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch_result

            result = install(
                "revoked/skill",
                "local-test",
                config,
                install_base=install_base,
                lock_path=lock_path,
            )

        assert result.success is False
        assert "crl" in result.error.lower() or "revoked" in result.error.lower()


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_skill_and_lock_entry() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"

        # Create a fake installed skill directory.
        safe_name = "test__skill"
        skill_dir = install_base / safe_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.py").write_text("# installed\n")

        # Pre-populate lock file.
        lock = HubLockFile()
        from arcskill.lock import SkillLockEntry

        lock.add_or_update(
            "test/skill",
            SkillLockEntry(
                content_hash="abc",
                install_path=str(skill_dir),
            ),
        )
        lock.save(lock_path)

        config = _personal_config()
        removed = uninstall("test/skill", config, install_base=install_base, lock_path=lock_path)

        assert removed is True
        assert not skill_dir.exists()

        lock2 = HubLockFile.load(lock_path)
        assert "test/skill" not in lock2.skills


# ---------------------------------------------------------------------------
# Update — no-op when hash unchanged
# ---------------------------------------------------------------------------


def test_update_marks_already_up_to_date() -> None:
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_clean_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config = _personal_config()

        same_hash = "samehash123"
        mock_fetch_result = FetchResult(
            local_path=bundle,
            content_hash=same_hash,
            source_name="local-test",
            bundle_url="",
            version="1.0.0",
        )

        # Pre-seed lock with the same hash.
        from arcskill.lock import SkillLockEntry

        lock = HubLockFile()
        lock.add_or_update(
            "test/skill",
            SkillLockEntry(content_hash=same_hash, install_path=str(install_base)),
        )
        lock.save(lock_path)

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                return_value=VerifyResult(
                    content_hash=same_hash,
                    signature_valid=True,
                    crl_checked=True,
                ),
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.scan",
                return_value=ScanResult(
                    verdict="safe",
                    findings=[],
                    counts={},
                    scanner_passes=["regex"],
                ),
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.run_dry_run",
                return_value=DryRunResult(passed=True, exit_code=0, backend_used="docker"),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch_result
            result = update(
                "test/skill",
                "local-test",
                config,
                install_base=install_base,
                lock_path=lock_path,
                skip_sandbox=True,
            )

        assert result.success is True
        assert result.error == "already_up_to_date"
