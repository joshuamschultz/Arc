"""Tests for SLSA Build Level enforcement at federal tier.

Federal tier MUST refuse any skill whose SLSA Build Level is below 3.
These tests verify the enforcement gate in ``verify_bundle`` and the
SLSA level extraction logic that feeds into it.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from arcskill.hub.config import (
    HubConfig,
    HubPolicy,
    RevocationConfig,
    SkillSource,
    TierPolicy,
)
from arcskill.hub.errors import SignatureInvalid
from arcskill.hub.verify import VerifyResult, _extract_slsa_level, verify_bundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _federal_config(require_slsa_level: int = 3) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        policy=HubPolicy(require_slsa_level=require_slsa_level),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=True,
        ),
    )


def _personal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        policy=HubPolicy(require_slsa_level=0),  # no SLSA requirement
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )


def _source() -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
        signer_issuer="https://token.actions.githubusercontent.com",
    )


def _make_bundle_file(content: bytes = b"fake bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_slsa_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


def _mock_sigstore_result(slsa_level: int) -> VerifyResult:
    return VerifyResult(
        content_hash="abc123",
        rekor_uuid="42",
        slsa_level=slsa_level,
        signature_valid=True,
        skipped=False,
    )


# ---------------------------------------------------------------------------
# SLSA level extraction unit tests
# ---------------------------------------------------------------------------


class TestSLSALevelExtraction:
    """Unit tests for ``_extract_slsa_level``."""

    def _make_bundle_data(
        self, builder_id: str, build_type: str = "https://slsa.dev/provenance/v1"
    ) -> dict:
        attestation = {
            "predicate": {
                "buildType": build_type,
                "runDetails": {"builder": {"id": builder_id}},
            }
        }
        payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
        return {"verificationMaterial": {"dsseEnvelope": {"payload": payload_b64}}}

    def test_slsa_l3_from_github_generator_workflow(self) -> None:
        """slsa-github-generator builder → L3."""
        bundle = self._make_bundle_data(
            "https://github.com/slsa-framework/slsa-github-generator/"
            ".github/workflows/builder_go_slsa3.yml@v1.9.0"
        )
        assert _extract_slsa_level(bundle) == 3

    def test_slsa_l3_from_explicit_buildlevel_annotation(self) -> None:
        """buildLevel@v1=3 in builder ID → L3."""
        bundle = self._make_bundle_data("https://build.example.com?buildLevel@v1=3")
        assert _extract_slsa_level(bundle) == 3

    def test_slsa_l2_from_explicit_annotation(self) -> None:
        """buildLevel@v1=2 in builder ID → L2."""
        bundle = self._make_bundle_data("https://build.example.com?buildLevel@v1=2")
        assert _extract_slsa_level(bundle) == 2

    def test_slsa_l1_from_generic_slsa_build_type(self) -> None:
        """Generic SLSA build type with unrecognised builder → L1."""
        bundle = self._make_bundle_data(
            "https://some.ci.example.com/generic-builder",
            build_type="https://slsa.dev/provenance/v1",
        )
        # Generic builder without explicit level or slsa-github-generator → L1.
        level = _extract_slsa_level(bundle)
        assert level == 1

    def test_slsa_l0_when_no_dsse_envelope(self) -> None:
        """No dsseEnvelope → L0."""
        assert _extract_slsa_level({}) == 0

    def test_slsa_l0_when_non_slsa_build_type(self) -> None:
        """Non-SLSA buildType → L0."""
        bundle = self._make_bundle_data(
            "https://build.example.com/generic",
            build_type="https://example.com/custom-build",
        )
        assert _extract_slsa_level(bundle) == 0

    def test_slsa_l0_on_malformed_payload(self) -> None:
        """Malformed base64/JSON payload → L0 (no exception)."""
        bundle_data = {
            "verificationMaterial": {"dsseEnvelope": {"payload": "!!!invalid base64!!!"}}
        }
        # Must not raise; returns 0.
        assert _extract_slsa_level(bundle_data) == 0


# ---------------------------------------------------------------------------
# Enforcement gate: verify_bundle SLSA level check
# ---------------------------------------------------------------------------


class TestFederalSLSAEnforcement:
    """Federal tier MUST refuse skills below SLSA L3."""

    def test_slsa_l1_refused_at_federal(self) -> None:
        """SLSA L1 bundle is refused at federal tier (below L3 minimum)."""
        bundle = _make_bundle_file()
        config = _federal_config(require_slsa_level=3)

        with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
            mock_run.return_value = _mock_sigstore_result(slsa_level=1)
            with unittest.mock.patch("arcskill.hub.verify._check_crl", side_effect=lambda r, _: r):
                with pytest.raises(
                    SignatureInvalid, match="SLSA level 1 is below required level 3"
                ):
                    verify_bundle(
                        bundle_path=bundle,
                        source=_source(),
                        config=config,
                        content_hash="abc123",
                    )

    def test_slsa_l2_refused_at_federal(self) -> None:
        """SLSA L2 bundle is refused at federal tier (below L3 minimum)."""
        bundle = _make_bundle_file()
        config = _federal_config(require_slsa_level=3)

        with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
            mock_run.return_value = _mock_sigstore_result(slsa_level=2)
            with unittest.mock.patch("arcskill.hub.verify._check_crl", side_effect=lambda r, _: r):
                with pytest.raises(
                    SignatureInvalid, match="SLSA level 2 is below required level 3"
                ):
                    verify_bundle(
                        bundle_path=bundle,
                        source=_source(),
                        config=config,
                        content_hash="abc123",
                    )

    def test_slsa_l3_accepted_at_federal(self) -> None:
        """SLSA L3 bundle is accepted at federal tier."""
        bundle = _make_bundle_file()
        config = _federal_config(require_slsa_level=3)

        with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
            mock_run.return_value = _mock_sigstore_result(slsa_level=3)
            with unittest.mock.patch("arcskill.hub.verify._fetch_crl", return_value=frozenset()):
                result = verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc123",
                )

        assert result.slsa_level == 3
        assert result.signature_valid is True

    def test_slsa_l0_refused_at_federal_when_l3_required(self) -> None:
        """No attestation (L0) is refused at federal when L3 is required."""
        bundle = _make_bundle_file()
        config = _federal_config(require_slsa_level=3)

        with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
            mock_run.return_value = _mock_sigstore_result(slsa_level=0)
            with unittest.mock.patch("arcskill.hub.verify._check_crl", side_effect=lambda r, _: r):
                with pytest.raises(
                    SignatureInvalid, match="SLSA level 0 is below required level 3"
                ):
                    verify_bundle(
                        bundle_path=bundle,
                        source=_source(),
                        config=config,
                        content_hash="abc123",
                    )

    def test_slsa_l1_accepted_at_personal(self) -> None:
        """SLSA L1 is accepted at personal tier (no minimum enforced when require_slsa_level=0)."""
        bundle = _make_bundle_file()
        config = _personal_config()

        with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
            mock_run.return_value = _mock_sigstore_result(slsa_level=1)
            with unittest.mock.patch("arcskill.hub.verify._fetch_crl", return_value=frozenset()):
                result = verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc123",
                )

        assert result.slsa_level == 1
        assert result.signature_valid is True

    def test_error_message_includes_tier_name(self) -> None:
        """SignatureInvalid message includes tier name for operator clarity."""
        bundle = _make_bundle_file()
        config = _federal_config(require_slsa_level=3)

        with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
            mock_run.return_value = _mock_sigstore_result(slsa_level=2)
            with unittest.mock.patch("arcskill.hub.verify._check_crl", side_effect=lambda r, _: r):
                with pytest.raises(SignatureInvalid) as exc_info:
                    verify_bundle(
                        bundle_path=bundle,
                        source=_source(),
                        config=config,
                        content_hash="abc123",
                    )

        assert "federal" in str(exc_info.value)
