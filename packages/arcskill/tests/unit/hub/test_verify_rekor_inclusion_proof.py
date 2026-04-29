"""Tests for Rekor transparency-log inclusion proof enforcement.

A tampered or missing Rekor entry must cause verification to fail.
The sigstore library verifies the inclusion proof inside
``Verifier.verify_artifact`` (step 8 in the verification pipeline).
These tests simulate that failure path.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

sigstore = pytest.importorskip(
    "sigstore",
    reason="sigstore package not installed; skipping Rekor inclusion tests",
)

from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import SignatureInvalid
from arcskill.hub.verify import _extract_rekor_uuid, _sigstore_verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact(content: bytes = b"skill bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_rekor_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


def _write_sidecar(bundle_path: Path, bundle_data: dict) -> Path:
    sidecar = bundle_path.parent / (bundle_path.name + ".sigstore")
    sidecar.write_text(json.dumps(bundle_data), encoding="utf-8")
    return sidecar


def _personal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
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


def _source() -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
        signer_issuer="https://token.actions.githubusercontent.com",
    )


def _bundle_data_with_log_index(log_index: str) -> dict:
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "verificationMaterial": {
            "tlogEntries": [{"logIndex": log_index}],
        },
    }


# ---------------------------------------------------------------------------
# Rekor inclusion proof failure
# ---------------------------------------------------------------------------


class TestRekorInclusionProof:
    """Tests for Rekor inclusion proof verification."""

    def test_tampered_rekor_entry_is_refused(self) -> None:
        """A tampered Rekor entry causes VerificationError → SignatureInvalid.

        The sigstore library verifies the Rekor inclusion proof by:
        1. Recomputing the entry hash from the artifact + certificate.
        2. Verifying the Merkle path against the log checkpoint.
        If the entry has been tampered with, step 1 will mismatch.
        We simulate this by having verify_artifact raise VerificationError
        with a Rekor-specific message.
        """
        artifact = _make_artifact()
        _write_sidecar(artifact, _bundle_data_with_log_index("999"))

        from sigstore.errors import VerificationError
        from sigstore.models import Bundle
        from sigstore.verify import Verifier

        def fake_verify_artifact(
            self: object, input_: bytes, bundle: object, policy: object
        ) -> None:
            raise VerificationError("Inclusion proof is invalid: Merkle path verification failed")

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(
                Bundle, "from_json", return_value=unittest.mock.MagicMock()
            ),
        ):
            with pytest.raises(
                SignatureInvalid,
                match="Merkle path verification failed",
            ):
                _sigstore_verify(artifact, _source(), _personal_config(), "hash")

    def test_missing_rekor_log_entry_is_refused(self) -> None:
        """Bundle without a tlog entry fails if Rekor proof is required.

        sigstore.models.Bundle.__init__ raises InvalidBundle when there is
        no tlog entry, because inclusion proof is a hard requirement.
        """
        artifact = _make_artifact()
        # Bundle with no tlogEntries.
        bundle_data = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {},
        }
        _write_sidecar(artifact, bundle_data)

        from sigstore.models import Bundle

        # Simulate InvalidBundle (which is a subclass of Exception, not VerificationError).
        def fake_from_json(raw: bytes | str) -> object:
            from sigstore.models import InvalidBundle

            raise InvalidBundle("expected exactly one log entry in bundle")

        with unittest.mock.patch.object(Bundle, "from_json", side_effect=fake_from_json):
            with pytest.raises(SignatureInvalid, match="exactly one log entry"):
                _sigstore_verify(artifact, _source(), _personal_config(), "hash")

    def test_rekor_uuid_extracted_from_bundle_object(self) -> None:
        """Rekor log_index is extracted from the live Bundle object first."""
        artifact = _make_artifact()
        bundle_data = _bundle_data_with_log_index("54321")
        _write_sidecar(artifact, bundle_data)

        from sigstore.models import Bundle
        from sigstore.verify import Verifier

        def fake_verify_artifact(
            self: object, input_: bytes, bundle: object, policy: object
        ) -> None:
            pass  # success

        mock_bundle = unittest.mock.MagicMock()
        mock_bundle.log_entry._inner.log_index = "54321"

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(Bundle, "from_json", return_value=mock_bundle),
        ):
            result = _sigstore_verify(artifact, _source(), _personal_config(), "hash")

        # The log_index from the Bundle object takes priority.
        assert result.rekor_uuid == "54321"

    def test_rekor_uuid_falls_back_to_bundle_json(self) -> None:
        """Rekor log_index falls back to JSON extraction if Bundle attribute unavailable."""
        artifact = _make_artifact()
        bundle_data = _bundle_data_with_log_index("11111")
        _write_sidecar(artifact, bundle_data)

        from sigstore.models import Bundle
        from sigstore.verify import Verifier

        def fake_verify_artifact(
            self: object, input_: bytes, bundle: object, policy: object
        ) -> None:
            pass

        # Mock bundle where accessing _inner.log_index raises AttributeError.
        # We use a real inner class so the property mock applies precisely.
        class _BadInner:
            @property
            def log_index(self) -> str:
                raise AttributeError("no log_index")

        class _BadEntry:
            _inner = _BadInner()

        mock_bundle = unittest.mock.MagicMock()
        mock_bundle.log_entry = _BadEntry()

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(Bundle, "from_json", return_value=mock_bundle),
        ):
            result = _sigstore_verify(artifact, _source(), _personal_config(), "hash")

        # Falls back to JSON extraction.
        assert result.rekor_uuid == "11111"

    def test_rekor_uuid_empty_when_missing(self) -> None:
        """Rekor UUID is empty string when neither source provides it."""
        bundle_data: dict = {}
        assert _extract_rekor_uuid(bundle_data) == ""

    def test_rekor_uuid_extracted_from_json_tlog_entries(self) -> None:
        """_extract_rekor_uuid correctly parses the Sigstore bundle format."""
        bundle_data = {"verificationMaterial": {"tlogEntries": [{"logIndex": "98765"}]}}
        assert _extract_rekor_uuid(bundle_data) == "98765"

    def test_tampered_rekor_at_federal_is_fail_closed(self) -> None:
        """Federal tier: tampered Rekor entry → SignatureInvalid (no warn-skip)."""
        artifact = _make_artifact()
        _write_sidecar(artifact, _bundle_data_with_log_index("42"))
        config = _federal_config()

        from sigstore.errors import VerificationError
        from sigstore.models import Bundle
        from sigstore.verify import Verifier

        def fake_verify_artifact(
            self: object, input_: bytes, bundle: object, policy: object
        ) -> None:
            raise VerificationError("Merkle proof verification failed")

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(
                Bundle, "from_json", return_value=unittest.mock.MagicMock()
            ),
        ):
            with pytest.raises(SignatureInvalid):
                _sigstore_verify(artifact, _source(), config, "hash")
