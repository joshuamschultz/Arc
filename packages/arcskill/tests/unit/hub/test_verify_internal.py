"""Tests for arcskill.hub.verify internal helpers.

Covers the paths missed by test_verify.py:
- _sigstore_importable
- _locate_bundle_sidecar (both candidate paths)
- _detect_bundle_kind (dsse via tlog body, dsse via dsseEnvelope, hashedrekord)
- _assert_slsa_predicate_type (federal/non-federal, bad type, bad predicate, bad JSON)
- _extract_rekor_uuid_from_bundle (live bundle mock, attribute-error fallback)
- _sigstore_verify (full success path via mocked sigstore, UnsafeNoOp path,
  missing signer_identity at federal, parse error, unexpected exception)
- _fetch_crl (list format, dict format)
- sha256_path (large file chunking)
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import SignatureInvalid
from arcskill.hub.verify import (
    _assert_slsa_predicate_type,
    _detect_bundle_kind,
    _extract_rekor_uuid,
    _extract_rekor_uuid_from_bundle,
    _extract_slsa_level,
    _fetch_crl,
    _locate_bundle_sidecar,
    _sigstore_importable,
    sha256_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _source_with_identity() -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
        signer_issuer="https://token.actions.githubusercontent.com",
    )


def _source_without_identity() -> SkillSource:
    return SkillSource(
        name="community-source",
        type="registry",
        url="https://example.com/skills",
        signer_identity=None,
        signer_issuer=None,
    )


def _make_bundle_file(content: bytes = b"fake bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_verify_int_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


# ---------------------------------------------------------------------------
# _sigstore_importable
# ---------------------------------------------------------------------------


def test_sigstore_importable_returns_true_when_present() -> None:
    """When sigstore can be imported, returns True."""
    fake_sigstore = MagicMock()
    with patch.dict("sys.modules", {"sigstore": fake_sigstore}):
        result = _sigstore_importable()
    assert result is True


def test_sigstore_importable_returns_false_when_absent() -> None:
    """When sigstore raises ImportError, returns False."""
    with patch.dict("sys.modules", {"sigstore": None}):
        # Removing the key forces actual import attempt
        import sys

        original = sys.modules.pop("sigstore", None)
        try:
            with patch("builtins.__import__", side_effect=ImportError("no sigstore")):
                result = _sigstore_importable()
            assert result is False
        finally:
            if original is not None:
                sys.modules["sigstore"] = original


# ---------------------------------------------------------------------------
# _locate_bundle_sidecar
# ---------------------------------------------------------------------------


def test_locate_bundle_sidecar_prefers_full_name() -> None:
    """Returns <bundle>.sigstore if it exists (preferred convention)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_sidecar_"))
    bundle = tmpdir / "skill.tar.gz"
    bundle.write_bytes(b"data")
    sidecar = tmpdir / "skill.tar.gz.sigstore"
    sidecar.write_text("{}", encoding="utf-8")

    result = _locate_bundle_sidecar(bundle)
    assert result == sidecar


def test_locate_bundle_sidecar_falls_back_to_stem() -> None:
    """Returns <stem>.sigstore when full-name sidecar is absent.

    For skill.tar.gz, Path.stem is 'skill.tar', so the fallback sidecar
    is skill.tar.sigstore.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_sidecar_"))
    bundle = tmpdir / "skill.tar.gz"
    bundle.write_bytes(b"data")
    # stem of 'skill.tar.gz' is 'skill.tar'
    sidecar = tmpdir / "skill.tar.sigstore"
    sidecar.write_text("{}", encoding="utf-8")

    result = _locate_bundle_sidecar(bundle)
    assert result == sidecar


def test_locate_bundle_sidecar_returns_none_when_absent() -> None:
    """Returns None when neither sidecar convention is present."""
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_sidecar_"))
    bundle = tmpdir / "skill.tar.gz"
    bundle.write_bytes(b"data")

    result = _locate_bundle_sidecar(bundle)
    assert result is None


# ---------------------------------------------------------------------------
# _detect_bundle_kind
# ---------------------------------------------------------------------------


def test_detect_bundle_kind_dsse_via_tlog_body() -> None:
    """Detects dsse when tlog entry body has kind=dsse."""
    body = json.dumps({"kind": "dsse"})
    body_b64 = base64.b64encode(body.encode()).decode()
    bundle_data = {"verificationMaterial": {"tlogEntries": [{"canonicalizedBody": body_b64}]}}
    assert _detect_bundle_kind(bundle_data) == "dsse"


def test_detect_bundle_kind_dsse_via_envelope_field() -> None:
    """Detects dsse when dsseEnvelope is directly in verificationMaterial."""
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": "abc="}}}
    assert _detect_bundle_kind(bundle_data) == "dsse"


def test_detect_bundle_kind_defaults_to_hashedrekord() -> None:
    """Defaults to hashedrekord for any non-dsse bundle."""
    assert _detect_bundle_kind({}) == "hashedrekord"
    assert _detect_bundle_kind({"verificationMaterial": {}}) == "hashedrekord"


def test_detect_bundle_kind_hashedrekord_from_tlog() -> None:
    """Returns hashedrekord when tlog body has non-dsse kind."""
    body = json.dumps({"kind": "hashedrekord"})
    body_b64 = base64.b64encode(body.encode()).decode()
    bundle_data = {"verificationMaterial": {"tlogEntries": [{"canonicalizedBody": body_b64}]}}
    assert _detect_bundle_kind(bundle_data) == "hashedrekord"


def test_detect_bundle_kind_handles_malformed_tlog_gracefully() -> None:
    """Malformed tlog body falls through to hashedrekord without raising."""
    bundle_data = {
        "verificationMaterial": {"tlogEntries": [{"canonicalizedBody": "!!not-base64!!"}]}
    }
    # Should not raise; defaults to hashedrekord
    assert _detect_bundle_kind(bundle_data) == "hashedrekord"


# ---------------------------------------------------------------------------
# _assert_slsa_predicate_type
# ---------------------------------------------------------------------------


def test_assert_slsa_predicate_non_federal_warns_on_unknown_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-federal tier: unknown payload type emits a warning and returns (not rejected).

    A completely foreign payload_type is accepted at non-federal because custom
    signers may use non-SLSA attestation formats. However, a warning is emitted.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="arcskill.hub.verify"):
        _assert_slsa_predicate_type(
            "application/random",
            b"{}",
            _personal_config(),
        )  # Must not raise
    assert any("Unrecognised" in r.message for r in caplog.records)


def test_assert_slsa_predicate_federal_valid_intoto() -> None:
    """Federal tier: application/vnd.in-toto+json with correct predicateType passes."""
    attestation = {"predicateType": "https://slsa.dev/provenance/v1"}
    payload = json.dumps(attestation).encode()
    _assert_slsa_predicate_type(
        "application/vnd.in-toto+json",
        payload,
        _federal_config(),
    )  # Must not raise


def test_assert_slsa_predicate_federal_direct_slsa_type() -> None:
    """Federal tier: direct SLSA predicate type string accepted."""
    attestation = {"predicateType": "https://slsa.dev/provenance/v1.1"}
    payload = json.dumps(attestation).encode()
    _assert_slsa_predicate_type(
        "https://slsa.dev/provenance/v1",
        payload,
        _federal_config(),
    )  # Must not raise


def test_assert_slsa_predicate_federal_wrong_payload_type_raises() -> None:
    """Federal tier: non-SLSA payload type → SignatureInvalid."""
    with pytest.raises(SignatureInvalid, match="Federal tier requires SLSA in-toto attestation"):
        _assert_slsa_predicate_type(
            "application/octet-stream",
            b"{}",
            _federal_config(),
        )


def test_assert_slsa_predicate_federal_wrong_predicate_type_raises() -> None:
    """Federal tier: predicateType not starting with https://slsa.dev/ → SignatureInvalid."""
    attestation = {"predicateType": "https://example.com/not-slsa"}
    payload = json.dumps(attestation).encode()
    with pytest.raises(SignatureInvalid, match="Federal tier requires predicateType"):
        _assert_slsa_predicate_type(
            "application/vnd.in-toto+json",
            payload,
            _federal_config(),
        )


def test_assert_slsa_predicate_federal_invalid_json_raises() -> None:
    """Federal tier: payload that is not valid JSON → SignatureInvalid."""
    with pytest.raises(SignatureInvalid, match="Cannot parse SLSA attestation payload"):
        _assert_slsa_predicate_type(
            "application/vnd.in-toto+json",
            b"not-json-{{{",
            _federal_config(),
        )


# ---------------------------------------------------------------------------
# _extract_rekor_uuid_from_bundle
# ---------------------------------------------------------------------------


def test_extract_rekor_uuid_from_bundle_happy_path() -> None:
    """Extracts log_index from live bundle object via internal API."""
    mock_bundle = SimpleNamespace(
        log_entry=SimpleNamespace(_inner=SimpleNamespace(log_index=42069))
    )
    result = _extract_rekor_uuid_from_bundle(mock_bundle)
    assert result == "42069"


def test_extract_rekor_uuid_from_bundle_none_log_index() -> None:
    """Returns empty string when log_index is None."""
    mock_bundle = SimpleNamespace(
        log_entry=SimpleNamespace(_inner=SimpleNamespace(log_index=None))
    )
    result = _extract_rekor_uuid_from_bundle(mock_bundle)
    assert result == ""


def test_extract_rekor_uuid_from_bundle_missing_attribute() -> None:
    """Returns empty string when the internal API structure is absent."""
    result = _extract_rekor_uuid_from_bundle(object())
    assert result == ""


# ---------------------------------------------------------------------------
# _sigstore_verify — mocked sigstore package
# ---------------------------------------------------------------------------


def _make_sidecar(bundle_path: Path, data: dict[str, object]) -> Path:
    """Write a .sigstore sidecar next to the bundle."""
    sidecar = bundle_path.parent / (bundle_path.name + ".sigstore")
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    return sidecar


def _mock_sigstore_modules(
    *,
    bundle_kind: str = "hashedrekord",
    verification_error: Exception | None = None,
) -> dict[str, Any]:
    """Return a dict of fake sigstore submodules for use with patch.dict."""
    from unittest.mock import MagicMock

    mock_bundle_instance = MagicMock()
    mock_bundle_class = MagicMock(return_value=mock_bundle_instance)
    mock_bundle_class.from_json = MagicMock(return_value=mock_bundle_instance)

    mock_verifier = MagicMock()
    if verification_error:
        mock_verifier.verify_artifact.side_effect = verification_error
        mock_verifier.verify_dsse.side_effect = verification_error
    else:
        mock_verifier.verify_artifact.return_value = None
        mock_verifier.verify_dsse.return_value = (
            "application/vnd.in-toto+json",
            json.dumps({"predicateType": "https://slsa.dev/provenance/v1"}).encode(),
        )

    mock_verifier_class = MagicMock()
    mock_verifier_class.production.return_value = mock_verifier

    mock_errors = MagicMock()
    mock_errors.VerificationError = type("VerificationError", (Exception,), {})

    mock_identity = MagicMock()
    mock_unsafe_noop = MagicMock()

    fake_models = MagicMock()
    fake_models.Bundle = mock_bundle_class

    fake_verify = MagicMock()
    fake_verify.Verifier = mock_verifier_class

    fake_policy = MagicMock()
    fake_policy.Identity = mock_identity
    fake_policy.UnsafeNoOp = mock_unsafe_noop

    return {
        "sigstore": MagicMock(errors=mock_errors, models=fake_models, verify=fake_verify),
        "sigstore.errors": mock_errors,
        "sigstore.models": fake_models,
        "sigstore.verify": fake_verify,
        "sigstore.verify.policy": fake_policy,
    }


def test_sigstore_verify_success_hashedrekord() -> None:
    """_sigstore_verify succeeds on a hashedrekord bundle (mocked sigstore)."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file(b"real bundle bytes")
    _make_sidecar(bundle_path, {"verificationMaterial": {}})
    source = _source_with_identity()
    config = _personal_config()
    content_hash = "abc123"

    mods = _mock_sigstore_modules(bundle_kind="hashedrekord")
    with patch.dict("sys.modules", mods):
        with patch("arcskill.hub.verify._detect_bundle_kind", return_value="hashedrekord"):
            with patch("arcskill.hub.verify._extract_rekor_uuid_from_bundle", return_value="99"):
                with patch("arcskill.hub.verify._extract_slsa_level", return_value=3):
                    result = _sigstore_verify(bundle_path, source, config, content_hash)

    assert result.signature_valid is True
    assert result.slsa_level == 3


def test_sigstore_verify_success_dsse() -> None:
    """_sigstore_verify succeeds on a dsse bundle (mocked sigstore)."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    _make_sidecar(bundle_path, {"verificationMaterial": {}})
    source = _source_with_identity()
    config = _personal_config()

    mods = _mock_sigstore_modules(bundle_kind="dsse")
    with patch.dict("sys.modules", mods):
        with patch("arcskill.hub.verify._detect_bundle_kind", return_value="dsse"):
            with patch("arcskill.hub.verify._assert_slsa_predicate_type"):
                with patch("arcskill.hub.verify._extract_rekor_uuid_from_bundle", return_value=""):
                    with patch("arcskill.hub.verify._extract_slsa_level", return_value=3):
                        result = _sigstore_verify(bundle_path, source, config, "hash")

    assert result.signature_valid is True


def test_sigstore_verify_audited_any_issuer_when_no_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-federal source without signer_identity uses _AuditedAnyIssuerPolicy, not UnsafeNoOp.

    The Rekor inclusion proof and Fulcio cert chain are still verified.
    An AUDIT WARNING is emitted so operators can track unconfigured sources.
    """
    import logging

    from arcskill.hub.verify import _AuditedAnyIssuerPolicy, _sigstore_verify

    bundle_path = _make_bundle_file()
    _make_sidecar(bundle_path, {"verificationMaterial": {}})
    source = _source_without_identity()
    config = _personal_config()

    mods = _mock_sigstore_modules()

    # Capture the policy object passed to the verifier.
    captured_policies: list[object] = []

    def _spy_verify_artifact(input_: bytes, bundle: object, policy: object) -> None:
        captured_policies.append(policy)

    mods[
        "sigstore.verify"
    ].Verifier.production.return_value.verify_artifact.side_effect = _spy_verify_artifact

    with caplog.at_level(logging.WARNING, logger="arcskill.hub.verify"):
        with patch.dict("sys.modules", mods):
            with patch("arcskill.hub.verify._detect_bundle_kind", return_value="hashedrekord"):
                with patch("arcskill.hub.verify._extract_rekor_uuid_from_bundle", return_value=""):
                    with patch("arcskill.hub.verify._extract_slsa_level", return_value=0):
                        result = _sigstore_verify(bundle_path, source, config, "hash")

    assert result.signature_valid is True

    # Policy passed to verifier must be _AuditedAnyIssuerPolicy, not UnsafeNoOp.
    assert len(captured_policies) == 1
    assert isinstance(captured_policies[0], _AuditedAnyIssuerPolicy), (
        f"Expected _AuditedAnyIssuerPolicy, got {type(captured_policies[0]).__name__}"
    )

    # A warning must be emitted for unconfigured identity.
    assert any("AUDIT WARNING" in r.message for r in caplog.records)


def test_sigstore_verify_federal_missing_identity_raises() -> None:
    """Federal source without signer_identity → SignatureInvalid before verification."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    _make_sidecar(bundle_path, {"verificationMaterial": {}})
    source = _source_without_identity()
    config = _federal_config()

    mods = _mock_sigstore_modules()
    with patch.dict("sys.modules", mods):
        with pytest.raises(SignatureInvalid, match="Federal tier requires signer_identity"):
            _sigstore_verify(bundle_path, source, config, "hash")


def test_sigstore_verify_bad_bundle_json_raises() -> None:
    """Unparseable .sigstore sidecar JSON → SignatureInvalid."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    sidecar = bundle_path.parent / (bundle_path.name + ".sigstore")
    sidecar.write_bytes(b"not-valid-json{{{")
    source = _source_with_identity()
    config = _personal_config()

    mods = _mock_sigstore_modules()
    with patch.dict("sys.modules", mods):
        with pytest.raises(SignatureInvalid, match="Cannot parse Sigstore bundle"):
            _sigstore_verify(bundle_path, source, config, "hash")


def test_sigstore_verify_no_sidecar_non_federal_skips() -> None:
    """Non-federal + no sidecar + require_signature=False → skipped result."""
    from arcskill.hub.config import HubPolicy
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    # No sidecar file written
    source = _source_without_identity()
    config = HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        policy=HubPolicy(require_signature=False),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )

    mods = _mock_sigstore_modules()
    with patch.dict("sys.modules", mods):
        result = _sigstore_verify(bundle_path, source, config, "hash")

    assert result.skipped is True
    assert result.signature_valid is False


def test_sigstore_verify_no_sidecar_federal_raises() -> None:
    """Federal + no sidecar → SignatureInvalid immediately."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    # No sidecar file written
    source = _source_with_identity()
    config = _federal_config()

    mods = _mock_sigstore_modules()
    with patch.dict("sys.modules", mods):
        with pytest.raises(SignatureInvalid, match="No Sigstore bundle sidecar found"):
            _sigstore_verify(bundle_path, source, config, "hash")


def test_sigstore_verify_unexpected_exception_raises_signature_invalid() -> None:
    """Unexpected exception during Bundle.from_json → SignatureInvalid (catch-all)."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    _make_sidecar(bundle_path, {"verificationMaterial": {}})
    source = _source_with_identity()
    config = _personal_config()

    mods = _mock_sigstore_modules()
    # Override Bundle.from_json to raise an unexpected error
    mods["sigstore.models"].Bundle.from_json.side_effect = RuntimeError("TUF error")
    with patch.dict("sys.modules", mods):
        with patch("arcskill.hub.verify._detect_bundle_kind", return_value="hashedrekord"):
            with pytest.raises(SignatureInvalid, match="unexpected error"):
                _sigstore_verify(bundle_path, source, config, "hash")


# ---------------------------------------------------------------------------
# _fetch_crl — both JSON schemas
# ---------------------------------------------------------------------------


def test_fetch_crl_list_format() -> None:
    """Legacy flat-list CRL format: ["hash1", "hash2"]."""
    crl_data = json.dumps(["hash1", "hash2"]).encode()
    mock_response = MagicMock()
    mock_response.read.return_value = crl_data
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("arcskill.hub.verify.urllib.request.urlopen", return_value=mock_response):
        result = _fetch_crl("https://example.com/crl.json")

    assert result == frozenset(["hash1", "hash2"])


def test_fetch_crl_dict_format() -> None:
    """Preferred dict CRL format: {"revoked": ["hash1"]}."""
    crl_data = json.dumps({"revoked": ["hash1", "hash3"]}).encode()
    mock_response = MagicMock()
    mock_response.read.return_value = crl_data
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("arcskill.hub.verify.urllib.request.urlopen", return_value=mock_response):
        result = _fetch_crl("https://example.com/crl.json")

    assert "hash1" in result
    assert "hash3" in result


def test_fetch_crl_empty_dict_returns_empty_set() -> None:
    """Dict CRL with no 'revoked' key → empty frozenset."""
    crl_data = json.dumps({}).encode()
    mock_response = MagicMock()
    mock_response.read.return_value = crl_data
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("arcskill.hub.verify.urllib.request.urlopen", return_value=mock_response):
        result = _fetch_crl("https://example.com/crl.json")

    assert result == frozenset()


# ---------------------------------------------------------------------------
# sha256_path — large-file chunking
# ---------------------------------------------------------------------------


def test_sha256_path_large_file() -> None:
    """sha256_path produces correct digest for a file larger than 65536 bytes."""
    import hashlib

    # 128 KB of data (2x chunk size)
    data = b"x" * 131072
    tmpdir = Path(tempfile.mkdtemp())
    p = tmpdir / "large.bin"
    p.write_bytes(data)

    expected = hashlib.sha256(data).hexdigest()
    assert sha256_path(p) == expected


# ---------------------------------------------------------------------------
# _extract_slsa_level — edge case paths
# ---------------------------------------------------------------------------


def test_extract_slsa_level_empty_payload_b64_returns_zero() -> None:
    """dsseEnvelope present but payload is empty string → 0."""
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": ""}}}
    assert _extract_slsa_level(bundle_data) == 0


def test_extract_slsa_level_non_standard_slsa_in_build_type_returns_one() -> None:
    """build_type contains 'slsa' but not slsa.dev or slsa-github-generator → level 1."""
    attestation = {
        "predicate": {
            "buildType": "https://custom.example.com/slsa-custom-builder/v1",
            "runDetails": {"builder": {"id": "https://custom.example.com/builder"}},
        }
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": payload_b64}}}
    assert _extract_slsa_level(bundle_data) == 1


def test_extract_slsa_level_non_slsa_build_type_returns_zero() -> None:
    """build_type with no 'slsa' in it at all → level 0."""
    attestation = {
        "predicate": {
            "buildType": "https://example.com/completely-custom/v1",
        }
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": payload_b64}}}
    assert _extract_slsa_level(bundle_data) == 0


def test_extract_slsa_level_explicit_level2_builder_id() -> None:
    """Explicit buildLevel@v1=2 in builder id → level 2."""
    attestation = {
        "predicate": {
            "buildType": "https://slsa.dev/provenance/v1",
            "runDetails": {"builder": {"id": "https://example.com/builders/buildLevel@v1=2"}},
        }
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": payload_b64}}}
    assert _extract_slsa_level(bundle_data) == 2


def test_extract_slsa_level_slsa_dev_without_builder_annotation_returns_one() -> None:
    """slsa.dev build type with no level annotation and unknown builder → level 1."""
    attestation = {
        "predicate": {
            "buildType": "https://slsa.dev/provenance/v1",
            "runDetails": {"builder": {"id": "https://example.com/generic-builder"}},
        }
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    bundle_data = {"verificationMaterial": {"dsseEnvelope": {"payload": payload_b64}}}
    assert _extract_slsa_level(bundle_data) == 1


# ---------------------------------------------------------------------------
# _sigstore_verify — VerificationError catch path (line 357)
# ---------------------------------------------------------------------------


def test_sigstore_verify_verification_error_raises_signature_invalid() -> None:
    """VerificationError from verify_artifact → SignatureInvalid (not catch-all)."""
    from arcskill.hub.verify import _sigstore_verify

    bundle_path = _make_bundle_file()
    _make_sidecar(bundle_path, {"verificationMaterial": {}})
    source = _source_with_identity()
    config = _personal_config()

    mods = _mock_sigstore_modules()
    # Make the VerificationError the actual class used in the except clause
    verification_error_cls = mods["sigstore.errors"].VerificationError
    mods["sigstore.models"].Bundle.from_json = MagicMock(return_value=MagicMock())
    # Make verifier raise VerificationError
    verifier_mock = mods["sigstore.verify"].Verifier.production.return_value
    verifier_mock.verify_artifact.side_effect = verification_error_cls("bad signature")

    with patch.dict("sys.modules", mods):
        with patch("arcskill.hub.verify._detect_bundle_kind", return_value="hashedrekord"):
            with pytest.raises(SignatureInvalid, match="Sigstore verification failed"):
                _sigstore_verify(bundle_path, source, config, "hash")


# ---------------------------------------------------------------------------
# _extract_rekor_uuid — from bundle data (dict path)
# ---------------------------------------------------------------------------


def test_extract_rekor_uuid_with_tlog_entries() -> None:
    """_extract_rekor_uuid returns logIndex when tlogEntries is present."""
    bundle_data: dict[str, object] = {
        "verificationMaterial": {"tlogEntries": [{"logIndex": "77777"}]}
    }
    result = _extract_rekor_uuid(bundle_data)
    assert result == "77777"


def test_extract_rekor_uuid_empty_tlog_entries() -> None:
    """_extract_rekor_uuid returns empty string when tlogEntries is empty list."""
    bundle_data: dict[str, object] = {"verificationMaterial": {"tlogEntries": []}}
    result = _extract_rekor_uuid(bundle_data)
    assert result == ""


def test_extract_rekor_uuid_type_error_returns_empty() -> None:
    """_extract_rekor_uuid returns empty when tlogEntries[0] raises TypeError.

    Uses a mock that raises TypeError on __getitem__ to exercise the
    (KeyError, IndexError, TypeError) exception handler.
    """
    mock_entries = MagicMock()
    mock_entries.__bool__ = MagicMock(return_value=True)
    mock_entries.__getitem__ = MagicMock(side_effect=TypeError("not subscriptable"))

    bundle_data: dict[str, object] = {"verificationMaterial": {"tlogEntries": mock_entries}}
    result = _extract_rekor_uuid(bundle_data)
    assert result == ""
