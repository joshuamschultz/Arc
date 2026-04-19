"""Tests for the production Sigstore verification path.

These tests exercise the real ``sigstore`` Python package.  Because Sigstore
requires a well-formed bundle + a real/staging Rekor inclusion proof to pass
``Verifier.production()``, we mock ``Verifier.verify_artifact`` to return
successfully (simulating a valid bundle) while keeping the surrounding
dispatch logic (bundle loading, policy construction, metadata extraction,
and SLSA parsing) fully exercised with real code.

Real Sigstore network verification is not feasible in a unit-test environment
without a pre-recorded bundle signed against the production Sigstore trust
root.  The integration test in ``tests/integration/`` exercises the real
Verifier against staging.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

sigstore = pytest.importorskip(
    "sigstore",
    reason="sigstore package not installed; skipping production-path tests",
)

from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import SignatureInvalid
from arcskill.hub.verify import VerifyResult, _sigstore_verify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact(content: bytes = b"test skill bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_prod_"))
    p = tmpdir / "skill.tar.gz"
    p.write_bytes(content)
    return p


def _write_sidecar(bundle_path: Path, bundle_data: dict) -> Path:
    sidecar = bundle_path.parent / (bundle_path.name + ".sigstore")
    sidecar.write_text(json.dumps(bundle_data), encoding="utf-8")
    return sidecar


def _slsa_l3_bundle_data() -> dict:
    """Return a minimal bundle JSON with SLSA L3 metadata."""
    attestation = {
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildType": "https://slsa.dev/provenance/v1",
            "runDetails": {
                "builder": {
                    "id": "https://github.com/slsa-framework/slsa-github-generator/"
                    ".github/workflows/builder_go_slsa3.yml@v1.9.0"
                }
            },
        },
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "verificationMaterial": {
            "tlogEntries": [{"logIndex": "99887766"}],
            "dsseEnvelope": {
                "payload": payload_b64,
                "payloadType": "application/vnd.in-toto+json",
                "signatures": [],
            },
        },
    }


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


def _source(
    identity: str = "https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
    issuer: str = "https://token.actions.githubusercontent.com",
) -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity=identity,
        signer_issuer=issuer,
    )


# ---------------------------------------------------------------------------
# Production path: verify_artifact called with correct sigstore v4 API
# ---------------------------------------------------------------------------


class TestProductionVerifyArtifactPath:
    """Exercise the hashedrekord (raw artifact) verification path."""

    def test_verify_artifact_called_with_input_underscore(self) -> None:
        """Verifier.verify_artifact MUST be called with ``input_=`` (not ``input=``).

        This is the critical sigstore v4 API fix.  If ``input=`` is used, Python
        will raise TypeError: unexpected keyword argument.
        """
        artifact = _make_artifact()
        bundle_data: dict = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {
                "tlogEntries": [{"logIndex": "12345"}],
            },
        }
        _write_sidecar(artifact, bundle_data)

        config = _personal_config()
        src = _source()

        captured_kwargs: dict = {}

        def fake_verify_artifact(
            self: object,
            input_: bytes,
            bundle: object,
            policy: object,
        ) -> None:
            captured_kwargs["input_"] = input_
            captured_kwargs["bundle"] = bundle
            captured_kwargs["policy"] = policy

        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(
                Bundle,
                "from_json",
                return_value=unittest.mock.MagicMock(
                    log_entry=unittest.mock.MagicMock(
                        _inner=unittest.mock.MagicMock(log_index="12345")
                    )
                ),
            ),
        ):
            result = _sigstore_verify(artifact, src, config, "fakehash")

        # Verify the correct keyword was captured.
        assert "input_" in captured_kwargs, (
            "verify_artifact was not called with the 'input_' keyword argument; "
            "sigstore v4 requires input_ not input"
        )
        assert captured_kwargs["input_"] == artifact.read_bytes()
        assert result.signature_valid is True
        assert result.rekor_uuid == "12345"

    def test_identity_policy_built_from_source_config(self) -> None:
        """Identity(identity=..., issuer=...) is built from SkillSource fields."""
        artifact = _make_artifact()
        bundle_data = {"verificationMaterial": {"tlogEntries": [{"logIndex": "5"}]}}
        _write_sidecar(artifact, bundle_data)

        config = _personal_config()
        src = _source(
            identity="https://github.com/my-org/my-repo/.github/workflows/publish.yml@refs/heads/main",
            issuer="https://token.actions.githubusercontent.com",
        )

        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity
        from sigstore.models import Bundle

        captured_policy: list = []

        def fake_verify_artifact(self: object, input_: bytes, bundle: object, policy: object) -> None:
            captured_policy.append(policy)

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(
                Bundle,
                "from_json",
                return_value=unittest.mock.MagicMock(
                    log_entry=unittest.mock.MagicMock(
                        _inner=unittest.mock.MagicMock(log_index="5")
                    )
                ),
            ),
        ):
            _sigstore_verify(artifact, src, config, "somehash")

        assert len(captured_policy) == 1
        assert isinstance(captured_policy[0], Identity)
        # Identity stores identity in _identity attribute.
        assert captured_policy[0]._identity == src.signer_identity

    def test_unsafe_noop_policy_when_no_signer_identity(self) -> None:
        """UnsafeNoOp is used as fallback when source has no signer_identity."""
        artifact = _make_artifact()
        bundle_data = {"verificationMaterial": {"tlogEntries": [{"logIndex": "1"}]}}
        _write_sidecar(artifact, bundle_data)

        config = _personal_config()
        src = SkillSource(name="community-src", type="github", repo="some/repo")
        # No signer_identity or signer_issuer.

        from sigstore.verify import Verifier
        from sigstore.verify.policy import UnsafeNoOp
        from sigstore.models import Bundle

        captured_policy: list = []

        def fake_verify_artifact(self: object, input_: bytes, bundle: object, policy: object) -> None:
            captured_policy.append(policy)

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(
                Bundle,
                "from_json",
                return_value=unittest.mock.MagicMock(
                    log_entry=unittest.mock.MagicMock(
                        _inner=unittest.mock.MagicMock(log_index="1")
                    )
                ),
            ),
        ):
            _sigstore_verify(artifact, src, config, "hash")

        assert len(captured_policy) == 1
        assert isinstance(captured_policy[0], UnsafeNoOp)

    def test_verification_error_raises_signature_invalid(self) -> None:
        """sigstore.errors.VerificationError is wrapped in SignatureInvalid."""
        artifact = _make_artifact()
        bundle_data = {"verificationMaterial": {"tlogEntries": [{"logIndex": "1"}]}}
        _write_sidecar(artifact, bundle_data)

        config = _personal_config()
        src = _source()

        from sigstore.errors import VerificationError
        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        def fake_verify_artifact(self: object, input_: bytes, bundle: object, policy: object) -> None:
            raise VerificationError("Rekor inclusion proof mismatch")

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(Bundle, "from_json", return_value=unittest.mock.MagicMock()),
        ):
            with pytest.raises(SignatureInvalid, match="Rekor inclusion proof mismatch"):
                _sigstore_verify(artifact, src, config, "hash")


# ---------------------------------------------------------------------------
# DSSE bundle path (SLSA attestation)
# ---------------------------------------------------------------------------


class TestProductionVerifyDSSEPath:
    """Exercise the dsse (SLSA attestation) verification path."""

    def test_dsse_bundle_dispatches_to_verify_dsse(self) -> None:
        """A bundle with dsseEnvelope triggers verify_dsse, not verify_artifact."""
        artifact = _make_artifact()
        attestation = {
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildType": "https://slsa.dev/provenance/v1",
                "runDetails": {"builder": {"id": "slsa-github-generator/v1"}},
            },
        }
        payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
        bundle_data = {
            "verificationMaterial": {
                "tlogEntries": [{"logIndex": "77"}],
                "dsseEnvelope": {
                    "payload": payload_b64,
                    "payloadType": "application/vnd.in-toto+json",
                    "signatures": [],
                },
            }
        }
        _write_sidecar(artifact, bundle_data)

        config = _personal_config()
        src = _source()

        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        dsse_called: list[bool] = []
        artifact_called: list[bool] = []

        def fake_verify_dsse(self: object, bundle: object, policy: object) -> tuple[str, bytes]:
            dsse_called.append(True)
            return ("application/vnd.in-toto+json", json.dumps(attestation).encode())

        def fake_verify_artifact(self: object, input_: bytes, bundle: object, policy: object) -> None:
            artifact_called.append(True)

        with (
            unittest.mock.patch.object(Verifier, "verify_dsse", fake_verify_dsse),
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(
                Bundle,
                "from_json",
                return_value=unittest.mock.MagicMock(
                    log_entry=unittest.mock.MagicMock(
                        _inner=unittest.mock.MagicMock(log_index="77")
                    )
                ),
            ),
        ):
            result = _sigstore_verify(artifact, src, config, "hash")

        assert dsse_called, "verify_dsse was not called for DSSE bundle"
        assert not artifact_called, "verify_artifact should not be called for DSSE bundle"
        assert result.signature_valid is True

    def test_slsa_metadata_extracted_from_dsse_bundle(self) -> None:
        """SLSA L3 level is extracted from DSSE attestation payload."""
        artifact = _make_artifact()
        bundle_data = _slsa_l3_bundle_data()
        _write_sidecar(artifact, bundle_data)

        config = _personal_config()
        src = _source()

        attestation = {
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildType": "https://slsa.dev/provenance/v1",
                "runDetails": {
                    "builder": {
                        "id": "https://github.com/slsa-framework/slsa-github-generator/"
                        ".github/workflows/builder_go_slsa3.yml@v1.9.0"
                    }
                },
            },
        }

        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        def fake_verify_dsse(self: object, bundle: object, policy: object) -> tuple[str, bytes]:
            return ("application/vnd.in-toto+json", json.dumps(attestation).encode())

        with (
            unittest.mock.patch.object(Verifier, "verify_dsse", fake_verify_dsse),
            unittest.mock.patch.object(
                Bundle,
                "from_json",
                return_value=unittest.mock.MagicMock(
                    log_entry=unittest.mock.MagicMock(
                        _inner=unittest.mock.MagicMock(log_index="99887766")
                    )
                ),
            ),
        ):
            result = _sigstore_verify(artifact, src, config, "hash")

        assert result.slsa_level == 3
        assert result.rekor_uuid == "99887766"
