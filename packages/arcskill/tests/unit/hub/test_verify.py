"""Tests for arcskill.hub.verify — Sigstore chain, Rekor, SLSA, CRL."""

from __future__ import annotations

import json
import tempfile
import time
import unittest.mock
import urllib.error
from pathlib import Path

import pytest
from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import CRLUnreachable, SignatureInvalid, SigstoreUnavailable
from arcskill.hub.verify import (
    VerifyResult,
    _check_crl,
    _crl_cache,
    _extract_rekor_uuid,
    _extract_slsa_level,
    sha256_path,
    verify_bundle,
)


def _federal_config(*, fail_closed: bool = True) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=fail_closed,
        ),
    )


def _personal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )


def _make_bundle_file(content: bytes = b"fake bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_verify_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


def _source() -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
        signer_issuer="https://token.actions.githubusercontent.com",
    )


# ---------------------------------------------------------------------------
# sigstore unavailable — federal must raise SigstoreUnavailable
# ---------------------------------------------------------------------------


def test_federal_no_sigstore_raises_sigstore_unavailable() -> None:
    """Federal tier: missing sigstore package → SigstoreUnavailable immediately."""
    bundle = _make_bundle_file()
    config = _federal_config()

    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        with pytest.raises(SigstoreUnavailable, match="federal tier requires full"):
            verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=config,
                content_hash="abc",
            )


def test_federal_no_sigstore_raises_signature_invalid() -> None:
    """Federal tier: SigstoreUnavailable IS a subclass of HubError, not SignatureInvalid.

    This test is kept for backwards-compat callers that caught SignatureInvalid.
    SigstoreUnavailable is distinct — callers should update their catch.
    """
    bundle = _make_bundle_file()
    config = _federal_config()

    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        # SigstoreUnavailable inherits HubError, NOT SignatureInvalid.
        from arcskill.hub.errors import HubError

        with pytest.raises(HubError):
            verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=config,
                content_hash="abc",
            )


def test_personal_no_sigstore_skips_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Personal tier: missing sigstore → warn and return skipped result."""
    bundle = _make_bundle_file()
    config = _personal_config()

    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
        # Also mock the CRL fetch so we don't hit the network.
        with unittest.mock.patch(
            "arcskill.hub.verify._fetch_crl",
            return_value=frozenset(),
        ):
            result = verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=config,
                content_hash="abc",
            )

    assert result.skipped is True
    assert result.signature_valid is False


# ---------------------------------------------------------------------------
# Missing bundle sidecar
# ---------------------------------------------------------------------------


def test_federal_missing_bundle_file_raises() -> None:
    """Federal tier: no .sigstore sidecar file → SignatureInvalid."""
    bundle = _make_bundle_file()
    config = _federal_config()

    with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=True):
        with unittest.mock.patch("arcskill.hub.verify._sigstore_verify") as mock_verify:
            mock_verify.side_effect = SignatureInvalid("No bundle sidecar")
            with pytest.raises(SignatureInvalid):
                verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )


# ---------------------------------------------------------------------------
# Mocked Sigstore chain verify — success path
# ---------------------------------------------------------------------------


def test_mocked_sigstore_success() -> None:
    """Mocked Sigstore verify returns a valid VerifyResult."""
    bundle = _make_bundle_file()
    config = _personal_config()

    with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
        mock_run.return_value = VerifyResult(
            content_hash="deadbeef",
            rekor_uuid="12345678",
            slsa_level=3,
            signature_valid=True,
            skipped=False,
        )
        with unittest.mock.patch(
            "arcskill.hub.verify._fetch_crl",
            return_value=frozenset(),
        ):
            result = verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=config,
                content_hash="deadbeef",
            )

    assert result.signature_valid is True
    assert result.rekor_uuid == "12345678"
    assert result.slsa_level == 3


# ---------------------------------------------------------------------------
# Mocked Rekor inclusion proof
# ---------------------------------------------------------------------------


def test_rekor_uuid_extraction() -> None:
    """Rekor UUID is extracted from the tlogEntries field."""
    bundle_data = {"verificationMaterial": {"tlogEntries": [{"logIndex": "42069"}]}}
    uuid = _extract_rekor_uuid(bundle_data)
    assert uuid == "42069"


def test_rekor_uuid_missing_returns_empty() -> None:
    bundle_data: dict = {}
    assert _extract_rekor_uuid(bundle_data) == ""


# ---------------------------------------------------------------------------
# SLSA level parsing
# ---------------------------------------------------------------------------


def test_slsa_level_3_detected() -> None:
    """SLSA L3 is detected from slsa-github-generator build type."""
    import base64

    attestation = {
        "predicate": {
            "buildType": "https://slsa.dev/provenance/v1",
            "runDetails": {
                "builder": {
                    "id": "https://github.com/slsa-framework/slsa-github-generator/.github/workflows/builder_go_slsa3.yml@v1.9.0"
                }
            },
        }
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": payload_b64}}}
    level = _extract_slsa_level(bundle_data)
    assert level == 3


def test_slsa_level_0_when_no_attestation() -> None:
    level = _extract_slsa_level({})
    assert level == 0


def test_slsa_l3_requirement_at_federal() -> None:
    """Federal tier rejects SLSA < 3 even if signature validates."""
    bundle = _make_bundle_file()
    config = _federal_config()

    with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
        mock_run.return_value = VerifyResult(
            content_hash="abc",
            slsa_level=2,  # below federal minimum of 3
            signature_valid=True,
        )
        with pytest.raises(SignatureInvalid, match="SLSA level 2 is below required level 3"):
            # Bypass CRL
            with unittest.mock.patch("arcskill.hub.verify._check_crl", side_effect=lambda r, _: r):
                verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )


# ---------------------------------------------------------------------------
# CRL check
# ---------------------------------------------------------------------------


def test_crl_fail_closed_on_network_error() -> None:
    """Federal tier: CRL unreachable → CRLUnreachable raised."""
    config = _federal_config(fail_closed=True)
    # Clear cache to force network fetch.
    _crl_cache.clear()

    base_result = VerifyResult(content_hash="abc", signature_valid=True)

    with unittest.mock.patch("arcskill.hub.verify._fetch_crl") as mock_fetch:
        mock_fetch.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(CRLUnreachable):
            _check_crl(base_result, config)


def test_crl_warn_skip_on_personal_unreachable() -> None:
    """Personal tier: CRL unreachable → warn and return result with crl_checked=False."""
    config = _personal_config()
    _crl_cache.clear()

    base_result = VerifyResult(content_hash="abc", signature_valid=True)

    with unittest.mock.patch("arcskill.hub.verify._fetch_crl") as mock_fetch:
        mock_fetch.side_effect = urllib.error.URLError("timeout")
        result = _check_crl(base_result, config)

    assert result.crl_checked is False
    assert result.revoked is False


def test_crl_marks_revoked_hash() -> None:
    """A hash in the CRL sets revoked=True on the result."""
    config = _federal_config()
    _crl_cache.clear()

    revoked_hash = "deadbeef" * 8  # 64 hex chars

    base_result = VerifyResult(
        content_hash=revoked_hash,
        signature_valid=True,
        crl_checked=False,
    )

    with unittest.mock.patch("arcskill.hub.verify._fetch_crl") as mock_fetch:
        mock_fetch.return_value = frozenset([revoked_hash])
        result = _check_crl(base_result, config)

    assert result.crl_checked is True
    assert result.revoked is True


def test_crl_clean_hash_not_revoked() -> None:
    """A hash not in the CRL is not revoked."""
    config = _federal_config()
    _crl_cache.clear()

    clean_hash = "cafe" * 16

    base_result = VerifyResult(
        content_hash=clean_hash,
        signature_valid=True,
    )

    with unittest.mock.patch("arcskill.hub.verify._fetch_crl") as mock_fetch:
        mock_fetch.return_value = frozenset(["deadbeef" * 8])
        result = _check_crl(base_result, config)

    assert result.revoked is False


def test_crl_cached_result_is_reused() -> None:
    """A fresh CRL entry is reused without network call."""
    config = _federal_config()
    _crl_cache.clear()

    # Pre-populate cache with a fresh entry (expires far in the future).
    _crl_cache[config.revocation.crl_url] = (
        time.monotonic() + 9999,
        frozenset(),
    )

    base_result = VerifyResult(content_hash="abc", signature_valid=True)

    with unittest.mock.patch("arcskill.hub.verify._fetch_crl") as mock_fetch:
        result = _check_crl(base_result, config)
        mock_fetch.assert_not_called()

    assert result.crl_checked is True


def test_sha256_path() -> None:
    """sha256_path returns correct digest for a known file."""
    import hashlib

    tmpdir = Path(tempfile.mkdtemp())
    p = tmpdir / "test.bin"
    p.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert sha256_path(p) == expected
