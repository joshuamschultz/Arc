"""Unit tests for each install pipeline stage in isolation.

Each stage function is tested independently using an InstallContext.
This validates that stage extraction did not alter behaviour and that
each stage can be reasoned about in isolation (single responsibility).
"""

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
from arcskill.hub.errors import ScanVerdictFailed, SignatureInvalid
from arcskill.hub.installer import (
    InstallContext,
    _stage_activate,
    _stage_audit,
    _stage_crl_check,
    _stage_dry_run,
    _stage_fetch,
    _stage_lock,
    _stage_scan,
    _stage_verify_signature,
)
from arcskill.hub.scanner import ScanResult
from arcskill.hub.sources import FetchResult
from arcskill.hub.verify import VerifyResult
from arcskill.lock import HubLockFile


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


def _make_bundle(tmpdir: Path) -> Path:
    bundle = tmpdir / "test_skill.tar.gz"
    skill_dir = tmpdir / "skill"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "skill.py").write_text("def run(): pass\n")
    (skill_dir / "MODULE.yaml").write_text("name: test-skill\nversion: '1.0'\n")
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(skill_dir / "skill.py", arcname="skill.py")
        tf.add(skill_dir / "MODULE.yaml", arcname="MODULE.yaml")
    return bundle


def _base_ctx(
    tmpdir: Path,
    name: str = "test/skill",
    source_name: str = "local-test",
) -> InstallContext:
    quarantine_dir = tmpdir / "quarantine" / name.replace("/", "__")
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    return InstallContext(
        name=name,
        source_name=source_name,
        config=_personal_config(),
        install_base=tmpdir / "skills",
        lock_path=tmpdir / "lock.json",
        skip_sandbox=True,
        quarantine_dir=quarantine_dir,
    )


# ---------------------------------------------------------------------------
# Stage 1: _stage_fetch
# ---------------------------------------------------------------------------


class TestStageFetch:
    def test_fetch_populates_ctx_fetch(self) -> None:
        """_stage_fetch sets ctx.fetch with a valid FetchResult."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)

            mock_result = FetchResult(
                local_path=bundle,
                content_hash="abc123",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            with unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter:
                mock_adapter.return_value.fetch.return_value = mock_result
                _stage_fetch(ctx)

            assert ctx.fetch is not None
            assert ctx.fetch.content_hash == "abc123"

    def test_fetch_none_before_stage_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            ctx = _base_ctx(Path(tmpdir_str))
            assert ctx.fetch is None


# ---------------------------------------------------------------------------
# Stage 2: _stage_verify_signature
# ---------------------------------------------------------------------------


class TestStageVerifySignature:
    def test_verify_populates_ctx_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="abc123",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            expected_verify = VerifyResult(
                content_hash="abc123",
                rekor_uuid="rekor-abc",
                slsa_level=1,
                signature_valid=True,
                crl_checked=True,
                revoked=False,
            )
            with unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                return_value=expected_verify,
            ):
                _stage_verify_signature(ctx)

            assert ctx.verify is not None
            assert ctx.verify.slsa_level == 1


# ---------------------------------------------------------------------------
# Stage 3: _stage_crl_check
# ---------------------------------------------------------------------------


class TestStageCrlCheck:
    def test_revoked_bundle_raises_signature_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="evil",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            ctx.verify = VerifyResult(
                content_hash="evil",
                signature_valid=True,
                crl_checked=True,
                revoked=True,
            )
            with pytest.raises(SignatureInvalid, match="CRL"):
                _stage_crl_check(ctx)

    def test_clean_bundle_passes_crl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="clean",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            ctx.verify = VerifyResult(
                content_hash="clean",
                signature_valid=True,
                crl_checked=True,
                revoked=False,
            )
            _stage_crl_check(ctx)  # Must not raise


# ---------------------------------------------------------------------------
# Stage 4: _stage_scan
# ---------------------------------------------------------------------------


class TestStageScan:
    def test_safe_verdict_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="abc",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            safe_scan = ScanResult(
                verdict="safe", findings=[], counts={}, scanner_passes=["regex"]
            )
            with unittest.mock.patch("arcskill.hub.installer.scan", return_value=safe_scan):
                _stage_scan(ctx)
            assert ctx.scan is not None
            assert ctx.scan.verdict == "safe"

    def test_dangerous_verdict_raises_when_policy_requires(self) -> None:
        from arcskill.hub.scanner import Finding

        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="abc",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            bad_scan = ScanResult(
                verdict="dangerous",
                findings=[
                    Finding(
                        severity="critical",
                        category="structural",
                        rule_id="test_rule",
                        message="Bad stuff",
                        path="skill.py",
                        line=1,
                    )
                ],
                counts={"critical": 1},
                scanner_passes=["regex"],
            )
            with unittest.mock.patch("arcskill.hub.installer.scan", return_value=bad_scan):
                with pytest.raises(ScanVerdictFailed):
                    _stage_scan(ctx)


# ---------------------------------------------------------------------------
# Stage 5: _stage_dry_run
# ---------------------------------------------------------------------------


class TestStageDryRun:
    def test_passed_dry_run_populates_ctx(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="abc",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            good_dry = DryRunResult(passed=True, exit_code=0, backend_used="docker")
            with unittest.mock.patch("arcskill.hub.installer.run_dry_run", return_value=good_dry):
                _stage_dry_run(ctx)
            assert ctx.dry_run is not None
            assert ctx.dry_run.passed is True

    def test_failed_dry_run_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="abc",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            bad_dry = DryRunResult(passed=False, exit_code=1, backend_used="docker", stdout="err")
            with unittest.mock.patch("arcskill.hub.installer.run_dry_run", return_value=bad_dry):
                with pytest.raises(RuntimeError, match="Dry-run failed"):
                    _stage_dry_run(ctx)


# ---------------------------------------------------------------------------
# Stage 6: _stage_activate
# ---------------------------------------------------------------------------


class TestStageActivate:
    def test_activate_extracts_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            install_base = tmpdir / "skills"
            install_base.mkdir()
            ctx = _base_ctx(tmpdir)
            ctx.install_base = install_base
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="abc",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            _stage_activate(ctx)
            assert ctx.install_path is not None
            assert ctx.install_path.exists()
            assert (ctx.install_path / "skill.py").exists()


# ---------------------------------------------------------------------------
# Stage 7: _stage_lock
# ---------------------------------------------------------------------------


class TestStageLock:
    def test_lock_file_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            install_base = tmpdir / "skills"
            install_base.mkdir()
            install_path = install_base / "test__skill"
            install_path.mkdir()
            (install_path / "skill.py").write_text("pass\n")

            ctx = _base_ctx(tmpdir)
            ctx.install_base = install_base
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="lock123",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            ctx.verify = VerifyResult(
                content_hash="lock123",
                rekor_uuid="rekor-lock",
                slsa_level=1,
                signature_valid=True,
                crl_checked=True,
            )
            ctx.scan = ScanResult(verdict="safe", findings=[], counts={}, scanner_passes=["regex"])
            ctx.install_path = install_path

            _stage_lock(ctx)

            lock = HubLockFile.load(ctx.lock_path)
            assert "test/skill" in lock.skills
            assert lock.skills["test/skill"].content_hash == "lock123"


# ---------------------------------------------------------------------------
# Stage 8: _stage_audit
# ---------------------------------------------------------------------------


class TestStageAudit:
    def test_audit_logs_completed_install(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            bundle = _make_bundle(tmpdir)
            install_base = tmpdir / "skills"
            install_base.mkdir()
            install_path = install_base / "test__skill"
            install_path.mkdir()

            ctx = _base_ctx(tmpdir)
            ctx.fetch = FetchResult(
                local_path=bundle,
                content_hash="audit_hash_abc",
                source_name="local-test",
                bundle_url="",
                version="1.0.0",
            )
            ctx.verify = VerifyResult(
                content_hash="audit_hash_abc",
                rekor_uuid="rekor-audit",
                slsa_level=2,
                signature_valid=True,
                crl_checked=True,
            )
            ctx.scan = ScanResult(verdict="safe", findings=[], counts={}, scanner_passes=["regex"])
            ctx.dry_run = DryRunResult(passed=True, exit_code=0, backend_used="docker")
            ctx.install_path = install_path

            with caplog.at_level(logging.INFO, logger="arcskill.hub.installer"):
                _stage_audit(ctx)

            messages = " ".join(r.getMessage() for r in caplog.records)
            assert "skills_hub.install_completed" in messages
            assert "test/skill" in messages
