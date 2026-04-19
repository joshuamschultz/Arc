"""Integration tests — federal install pipeline (G4.2).

Tests the full install pipeline end-to-end with mocked network calls:

1. Valid signed skill with SLSA L3 → installs successfully.
2. Missing signature → install refused (SignatureInvalid).
3. CRL unreachable at federal tier → hard error (CRLUnreachable).

All network I/O is mocked; no real Sigstore or HTTP calls are made.
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
from arcskill.hub.errors import CRLUnreachable, SignatureInvalid
from arcskill.hub.installer import install
from arcskill.hub.scanner import ScanResult
from arcskill.hub.sources import FetchResult
from arcskill.hub.verify import VerifyResult
from arcskill.lock import HubLockFile


def _make_skill_bundle(tmpdir: Path) -> Path:
    bundle = tmpdir / "sample_skill.tar.gz"
    skill_dir = tmpdir / "sample"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "skill.py").write_text("def run(text): return text[:100]\n")
    (skill_dir / "MODULE.yaml").write_text(
        "name: sample-skill\nversion: '1.0'\ndescription: 'A clean summarise skill'\n"
    )
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(skill_dir / "skill.py", arcname="skill.py")
        tf.add(skill_dir / "MODULE.yaml", arcname="MODULE.yaml")
    return bundle


def _federal_config_with_source() -> tuple[HubConfig, SkillSource]:
    source = SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        trust="builtin",
        signer_identity=(
            "https://github.com/arc-foundation/skills/"
            ".github/workflows/publish.yml@refs/heads/main"
        ),
        signer_issuer="https://token.actions.githubusercontent.com",
    )
    config = HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        policy=HubPolicy(
            require_signature=True,
            require_slsa_level=3,
            require_scan_pass=True,
            max_findings_allowed=FindingsAllowed(critical=0, high=0, medium=2),
        ),
        sources=[source],
        revocation=RevocationConfig(
            crl_url="https://skills.arcagent.dev/v1/crl.json",
            fail_closed_if_unreachable=True,
        ),
    )
    return config, source


# ---------------------------------------------------------------------------
# G4.2.1: Valid signed skill with SLSA L3 installs successfully
# ---------------------------------------------------------------------------


def test_federal_valid_signed_skill_installs() -> None:
    """Full pipeline: signed SLSA L3 skill installs at federal tier."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_skill_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config, _ = _federal_config_with_source()

        mock_fetch = FetchResult(
            local_path=bundle,
            content_hash="slsa3hashfederal",
            source_name="arc-official",
            bundle_url="https://github.com/arc-foundation/skills/releases/download/v1.0/sample.tar.gz",
            version="1.0.0",
        )
        mock_verify = VerifyResult(
            content_hash="slsa3hashfederal",
            rekor_uuid="rekor-federal-uuid-12345",
            slsa_level=3,
            signature_valid=True,
            crl_checked=True,
            revoked=False,
        )
        mock_scan = ScanResult(
            verdict="safe",
            findings=[],
            counts={},
            scanner_passes=["regex", "ast", "text_injection"],
        )
        mock_dry_run = DryRunResult(
            passed=True,
            exit_code=0,
            backend_used="firecracker",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle", return_value=mock_verify
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.scan", return_value=mock_scan
            ),
            unittest.mock.patch(
                "arcskill.hub.installer.run_dry_run", return_value=mock_dry_run
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch

            result = install(
                "arc-official/sample",
                "arc-official",
                config,
                install_base=install_base,
                lock_path=lock_path,
                skip_sandbox=False,
            )

        assert result.success is True, f"Expected success; got error: {result.error}"
        assert result.verify is not None
        assert result.verify.slsa_level == 3
        assert result.verify.signature_valid is True

        # Lock file must record the install.
        lock = HubLockFile.load(lock_path)
        assert "arc-official/sample" in lock.skills
        entry = lock.skills["arc-official/sample"]
        assert entry.slsa_level == 3
        assert entry.rekor_uuid == "rekor-federal-uuid-12345"
        assert entry.scan_verdict == "safe"


# ---------------------------------------------------------------------------
# G4.2.2: Missing signature → install refused
# ---------------------------------------------------------------------------


def test_federal_missing_signature_refused() -> None:
    """Federal tier: unsigned bundle → SignatureInvalid propagates to failure."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_skill_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config, _ = _federal_config_with_source()

        mock_fetch = FetchResult(
            local_path=bundle,
            content_hash="unsignedskillhash",
            source_name="arc-official",
            bundle_url="",
            version="1.0.0",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                side_effect=SignatureInvalid(
                    "No Sigstore bundle sidecar found for sample_skill.tar.gz"
                ),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch

            result = install(
                "arc-official/unsigned",
                "arc-official",
                config,
                install_base=install_base,
                lock_path=lock_path,
            )

        assert result.success is False
        assert "sigstore" in result.error.lower() or "signature" in result.error.lower() or "bundle" in result.error.lower()

        # Skill must NOT appear in lock file.
        lock = HubLockFile.load(lock_path)
        assert "arc-official/unsigned" not in lock.skills


# ---------------------------------------------------------------------------
# G4.2.3: CRL unreachable at federal → hard error
# ---------------------------------------------------------------------------


def test_federal_crl_unreachable_hard_error() -> None:
    """Federal tier: CRL endpoint unreachable → install fails with CRLUnreachable."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_skill_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config, _ = _federal_config_with_source()

        mock_fetch = FetchResult(
            local_path=bundle,
            content_hash="crl_test_hash",
            source_name="arc-official",
            bundle_url="",
            version="1.0.0",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                side_effect=CRLUnreachable(
                    "CRL endpoint unreachable: connection refused"
                ),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch

            result = install(
                "arc-official/crl-test",
                "arc-official",
                config,
                install_base=install_base,
                lock_path=lock_path,
            )

        assert result.success is False
        assert (
            "crl" in result.error.lower()
            or "unreachable" in result.error.lower()
        ), f"Expected CRL error; got: {result.error!r}"

        # Skill must NOT appear in lock file.
        lock = HubLockFile.load(lock_path)
        assert "arc-official/crl-test" not in lock.skills


# ---------------------------------------------------------------------------
# Federal tier: SLSA L2 < L3 requirement → install refused
# ---------------------------------------------------------------------------


def test_federal_slsa_level_2_refused() -> None:
    """Federal requires SLSA L3; L2 attestation → SignatureInvalid."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bundle = _make_skill_bundle(tmpdir)
        lock_path = tmpdir / "lock.json"
        install_base = tmpdir / "skills"
        install_base.mkdir()

        config, _ = _federal_config_with_source()

        mock_fetch = FetchResult(
            local_path=bundle,
            content_hash="slsa2hash",
            source_name="arc-official",
            bundle_url="",
            version="1.0.0",
        )

        with (
            unittest.mock.patch("arcskill.hub.installer.make_adapter") as mock_adapter,
            unittest.mock.patch(
                "arcskill.hub.installer.verify_bundle",
                side_effect=SignatureInvalid(
                    "SLSA level 2 is below required level 3 (tier='federal')"
                ),
            ),
        ):
            mock_adapter.return_value.fetch.return_value = mock_fetch

            result = install(
                "arc-official/slsa2skill",
                "arc-official",
                config,
                install_base=install_base,
                lock_path=lock_path,
            )

        assert result.success is False
        assert "slsa" in result.error.lower()
