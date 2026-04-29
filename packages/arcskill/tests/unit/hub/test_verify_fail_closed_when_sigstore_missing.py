"""Tests for sigstore-unavailable tier policy.

When the ``sigstore`` package is not installed:
- **Federal tier**: raises ``SigstoreUnavailable`` immediately with an
  install hint pointing to ``pip install arcskill[hub]``.
- **Personal / enterprise tiers**: logs a warning and returns
  ``VerifyResult(skipped=True, signature_valid=False)``.

These tests mock ``arcskill.hub.verify._sigstore_importable`` to return
``False``, simulating the absent-package scenario without actually
uninstalling sigstore.
"""

from __future__ import annotations

import logging
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import SigstoreUnavailable
from arcskill.hub.verify import VerifyResult, _crl_cache, _run_sigstore, verify_bundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bundle_file(content: bytes = b"fake bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_missing_"))
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


def _federal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=True,
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


def _enterprise_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="enterprise"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )


# ---------------------------------------------------------------------------
# Federal tier: hard-fail when sigstore is missing
# ---------------------------------------------------------------------------


class TestFederalSigstoreMissing:
    """Federal tier must raise SigstoreUnavailable when sigstore is absent."""

    def test_raises_sigstore_unavailable_not_signature_invalid(self) -> None:
        """SigstoreUnavailable (not SignatureInvalid) is raised at federal tier."""
        bundle = _make_bundle_file()
        config = _federal_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with pytest.raises(SigstoreUnavailable):
                verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )

    def test_error_message_contains_install_hint(self) -> None:
        """SigstoreUnavailable message contains pip install hint."""
        bundle = _make_bundle_file()
        config = _federal_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with pytest.raises(SigstoreUnavailable) as exc_info:
                verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )

        msg = str(exc_info.value)
        assert "arcskill[hub]" in msg, f"Install hint not found in: {msg!r}"

    def test_error_message_mentions_federal_tier(self) -> None:
        """Error message explicitly mentions federal tier requirement."""
        bundle = _make_bundle_file()
        config = _federal_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with pytest.raises(SigstoreUnavailable) as exc_info:
                verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )

        msg = str(exc_info.value).lower()
        assert "federal" in msg, f"'federal' not found in: {msg!r}"

    def test_run_sigstore_raises_sigstore_unavailable_directly(self) -> None:
        """_run_sigstore itself raises SigstoreUnavailable at federal when absent."""
        bundle = _make_bundle_file()
        config = _federal_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with pytest.raises(SigstoreUnavailable):
                _run_sigstore(bundle, _source(), config, "abc")

    def test_sigstore_unavailable_is_hub_error(self) -> None:
        """SigstoreUnavailable inherits from HubError for broad catch."""
        from arcskill.hub.errors import HubError

        assert issubclass(SigstoreUnavailable, HubError)

    def test_sigstore_unavailable_is_not_signature_invalid(self) -> None:
        """SigstoreUnavailable is distinct from SignatureInvalid.

        Callers that specifically catch SignatureInvalid for crypto failures
        should NOT catch SigstoreUnavailable — they need to upgrade their
        dependency or install arcskill[hub].
        """
        from arcskill.hub.errors import SignatureInvalid

        assert not issubclass(SigstoreUnavailable, SignatureInvalid)


# ---------------------------------------------------------------------------
# Personal tier: warn-skip when sigstore is missing
# ---------------------------------------------------------------------------


class TestPersonalSigstoreMissing:
    """Personal tier must warn and return skipped VerifyResult when sigstore absent."""

    def test_returns_skipped_result(self) -> None:
        """Personal tier: sigstore absent → VerifyResult(skipped=True)."""
        bundle = _make_bundle_file()
        config = _personal_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with unittest.mock.patch("arcskill.hub.verify._fetch_crl", return_value=frozenset()):
                result = verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )

        assert result.skipped is True
        assert result.signature_valid is False
        assert result.content_hash == "abc"

    def test_warning_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Personal tier: sigstore absent → WARNING is emitted."""
        bundle = _make_bundle_file()
        config = _personal_config()

        with caplog.at_level(logging.WARNING, logger="arcskill.hub.verify"):
            with unittest.mock.patch(
                "arcskill.hub.verify._sigstore_importable", return_value=False
            ):
                with unittest.mock.patch(
                    "arcskill.hub.verify._fetch_crl", return_value=frozenset()
                ):
                    verify_bundle(
                        bundle_path=bundle,
                        source=_source(),
                        config=config,
                        content_hash="abc",
                    )

        # At least one warning should mention sigstore.
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "No WARNING was logged when sigstore unavailable"
        combined = " ".join(r.message for r in warning_records)
        assert "sigstore" in combined.lower()

    def test_crl_still_checked_when_skipped(self) -> None:
        """CRL check runs even when sigstore verification was skipped."""
        bundle = _make_bundle_file()
        config = _personal_config()
        revoked_hash = "abc"
        _crl_cache.clear()  # Clear global cache to avoid cross-test pollution.

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with unittest.mock.patch(
                "arcskill.hub.verify._fetch_crl", return_value=frozenset([revoked_hash])
            ):
                result = verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash=revoked_hash,
                )

        assert result.skipped is True
        assert result.crl_checked is True
        assert result.revoked is True

    def test_no_exception_raised_for_personal(self) -> None:
        """Personal tier: sigstore absent does NOT raise any exception."""
        bundle = _make_bundle_file()
        config = _personal_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with unittest.mock.patch("arcskill.hub.verify._fetch_crl", return_value=frozenset()):
                # Must not raise.
                result = verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )
        assert isinstance(result, VerifyResult)


# ---------------------------------------------------------------------------
# Enterprise tier: warn-skip when sigstore is missing
# ---------------------------------------------------------------------------


class TestEnterpriseSigstoreMissing:
    """Enterprise tier (non-federal) should also warn-skip, not hard-fail."""

    def test_enterprise_returns_skipped_result(self) -> None:
        """Enterprise tier: sigstore absent → VerifyResult(skipped=True)."""
        bundle = _make_bundle_file()
        config = _enterprise_config()

        with unittest.mock.patch("arcskill.hub.verify._sigstore_importable", return_value=False):
            with unittest.mock.patch("arcskill.hub.verify._fetch_crl", return_value=frozenset()):
                result = verify_bundle(
                    bundle_path=bundle,
                    source=_source(),
                    config=config,
                    content_hash="abc",
                )

        assert result.skipped is True
        assert result.signature_valid is False
