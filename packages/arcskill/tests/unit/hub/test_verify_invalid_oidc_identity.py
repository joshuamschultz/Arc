"""Tests for OIDC identity policy enforcement.

A bundle signed by an unexpected OIDC subject (wrong workflow URL, wrong
issuer, or a completely different principal) must be refused with
``SignatureInvalid``.  These tests mock ``Verifier.verify_artifact`` to
raise ``sigstore.errors.VerificationError`` with an identity-mismatch
message, which is the behaviour of the real sigstore library when the
certificate SAN does not match the configured ``Identity`` policy.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

sigstore = pytest.importorskip(
    "sigstore",
    reason="sigstore package not installed; skipping OIDC identity tests",
)

from arcskill.hub.config import HubConfig, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.errors import SignatureInvalid
from arcskill.hub.verify import _sigstore_verify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artifact(content: bytes = b"test skill bundle") -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_oidc_"))
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


def _source(identity: str, issuer: str = "https://token.actions.githubusercontent.com") -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity=identity,
        signer_issuer=issuer,
    )


def _minimal_bundle_data() -> dict:
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "verificationMaterial": {
            "tlogEntries": [{"logIndex": "100"}],
        },
    }


# ---------------------------------------------------------------------------
# Identity mismatch tests
# ---------------------------------------------------------------------------


class TestOIDCIdentityMismatch:
    """Bundles signed by wrong OIDC subjects must be refused."""

    def _run_verify_expecting_failure(
        self,
        artifact: Path,
        source: SkillSource,
        config: HubConfig,
        error_msg: str,
    ) -> None:
        """Run _sigstore_verify and assert SignatureInvalid is raised."""
        from sigstore.errors import VerificationError
        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        def fake_verify_artifact(self: object, input_: bytes, bundle: object, policy: object) -> None:
            raise VerificationError(error_msg)

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(Bundle, "from_json", return_value=unittest.mock.MagicMock()),
        ):
            with pytest.raises(SignatureInvalid, match=error_msg):
                _sigstore_verify(artifact, source, config, "deadbeef")

    def test_wrong_oidc_subject_refused(self) -> None:
        """Bundle signed by wrong@evil.com OIDC subject → refused."""
        artifact = _make_artifact()
        _write_sidecar(artifact, _minimal_bundle_data())

        src = _source(
            identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main"
        )
        config = _personal_config()

        # Simulate sigstore refusing because SAN = "wrong@evil.com"
        self._run_verify_expecting_failure(
            artifact,
            src,
            config,
            error_msg="Certificate's SANs do not match",
        )

    def test_wrong_issuer_refused(self) -> None:
        """Bundle from unexpected issuer is refused by Identity policy."""
        artifact = _make_artifact()
        _write_sidecar(artifact, _minimal_bundle_data())

        src = _source(
            identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
            issuer="https://token.actions.githubusercontent.com",
        )
        config = _personal_config()

        self._run_verify_expecting_failure(
            artifact,
            src,
            config,
            error_msg="Issuer mismatch",
        )

    def test_federal_identity_mismatch_raises_signature_invalid(self) -> None:
        """Federal tier: OIDC identity mismatch → SignatureInvalid (fail-closed)."""
        artifact = _make_artifact()
        _write_sidecar(artifact, _minimal_bundle_data())

        src = _source(
            identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main"
        )
        config = _federal_config()

        self._run_verify_expecting_failure(
            artifact,
            src,
            config,
            error_msg="Certificate's SANs do not match",
        )

    def test_federal_no_signer_identity_configured_raises(self) -> None:
        """Federal tier without signer_identity in source → SignatureInvalid (fail-closed).

        Federal requires both signer_identity and signer_issuer to be set.
        """
        artifact = _make_artifact()
        _write_sidecar(artifact, _minimal_bundle_data())

        src = SkillSource(
            name="unconfigured-source",
            type="github",
            repo="some/repo",
            # No signer_identity or signer_issuer — this must fail at federal.
        )
        config = _federal_config()

        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        # The guard should fire BEFORE verify_artifact is called.
        verify_artifact_called: list[bool] = []

        def fake_verify_artifact(self: object, input_: bytes, bundle: object, policy: object) -> None:
            verify_artifact_called.append(True)

        with (
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(Bundle, "from_json", return_value=unittest.mock.MagicMock()),
        ):
            with pytest.raises(SignatureInvalid, match="signer_identity"):
                _sigstore_verify(artifact, src, config, "hash")

        # The guard fires before calling the verifier.
        assert not verify_artifact_called

    def test_completely_different_principal_refused(self) -> None:
        """A bundle signed by a totally unrelated principal is refused."""
        artifact = _make_artifact()
        _write_sidecar(artifact, _minimal_bundle_data())

        # Expected: official Arc workflow.  Actual (simulated): attacker's fork.
        src = _source(
            identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main"
        )
        config = _personal_config()

        self._run_verify_expecting_failure(
            artifact,
            src,
            config,
            error_msg="Certificate's SANs do not match",
        )
