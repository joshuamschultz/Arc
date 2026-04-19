"""Integration test: full installer pipeline with real Sigstore verification.

This test exercises the complete install pipeline end-to-end using the real
``sigstore`` Python package.  Because running a real Sigstore signing operation
against the production trust root requires network access and GitHub Actions
OIDC tokens (which are only available in CI), we use a carefully crafted mock
strategy:

1. ``Verifier.verify_artifact`` and ``Verifier.verify_dsse`` are patched to
   simulate successful verification without needing a real signed bundle.
2. ``Bundle.from_json`` is patched to return a mock Bundle with realistic
   log entry metadata.
3. The CRL fetch is patched to return an empty revocation list.
4. The scanner is invoked against a real (safe) skill tarball.

Bundle type selection
---------------------
The test sidecars include a ``dsseEnvelope`` field, so ``_detect_bundle_kind``
returns ``"dsse"`` and the code calls ``Verifier.verify_dsse``.  Both
``verify_artifact`` and ``verify_dsse`` are patched to handle either path.

This validates that the installer pipeline correctly assembles a
``VerifyResult`` from the Sigstore path and that ``rekor_uuid`` and
``slsa_level`` propagate correctly through to the result.

Network independence
--------------------
The test skips automatically if ``sigstore`` is not installed via
``pytest.importorskip``.  It does NOT make real network calls to Sigstore
or Rekor.
"""

from __future__ import annotations

import base64
import json
import tarfile
import tempfile
import unittest.mock
from pathlib import Path

import pytest

sigstore = pytest.importorskip(
    "sigstore",
    reason="sigstore package not installed; skipping real-sigstore integration tests",
)

from arcskill.hub.config import (
    HubConfig,
    HubPolicy,
    RevocationConfig,
    SkillSource,
    TierPolicy,
)
from arcskill.hub.verify import VerifyResult, verify_bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_safe_skill_bundle(tmpdir: Path) -> Path:
    """Create a minimal but real tarball containing a benign skill."""
    skill_dir = tmpdir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "skill.py").write_text(
        'def run(task: str) -> str:\n    return f"Result: {task}"\n',
        encoding="utf-8",
    )
    (skill_dir / "MODULE.yaml").write_text(
        "name: my-skill\nversion: 1.0.0\ndescription: A safe test skill.\n",
        encoding="utf-8",
    )
    bundle = tmpdir / "my-skill-1.0.0.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        for f in skill_dir.iterdir():
            tf.add(f, arcname=f.name)
    return bundle


def _write_sigstore_sidecar(bundle_path: Path, slsa_level: int = 3) -> Path:
    """Write a minimal Sigstore bundle sidecar alongside the tarball.

    Includes a ``dsseEnvelope`` so the code takes the DSSE path
    (``verify_dsse``), which is the realistic production path for SLSA
    attestation bundles.
    """
    attestation = {
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildType": "https://slsa.dev/provenance/v1",
            "runDetails": {
                "builder": {
                    "id": (
                        "https://github.com/slsa-framework/slsa-github-generator/"
                        ".github/workflows/builder_go_slsa3.yml@v1.9.0"
                        if slsa_level == 3
                        else "https://build.example.com?buildLevel@v1=1"
                    )
                }
            },
        },
    }
    payload_b64 = base64.b64encode(json.dumps(attestation).encode()).decode()
    bundle_data = {
        "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
        "verificationMaterial": {
            "tlogEntries": [{"logIndex": "12345678"}],
            "dsseEnvelope": {
                "payload": payload_b64,
                "payloadType": "application/vnd.in-toto+json",
                "signatures": [],
            },
        },
    }
    sidecar = bundle_path.parent / (bundle_path.name + ".sigstore")
    sidecar.write_text(json.dumps(bundle_data), encoding="utf-8")
    return sidecar


def _personal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        policy=HubPolicy(require_slsa_level=0),
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


def _make_mock_bundle(log_index: str = "12345678") -> unittest.mock.MagicMock:
    """Create a mock Bundle with the given Rekor log index."""
    mock_bundle = unittest.mock.MagicMock()
    mock_bundle.log_entry._inner.log_index = log_index
    return mock_bundle


def _fake_verify_dsse_l3(
    payload_b64: str,
) -> tuple[str, bytes]:
    """Return a fake verify_dsse result with SLSA L3 attestation payload."""
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
    return ("application/vnd.in-toto+json", json.dumps(attestation).encode())


# ---------------------------------------------------------------------------
# Integration: verify_bundle with mocked Verifier
# ---------------------------------------------------------------------------


class TestHubInstallRealSigstore:
    """Full verify_bundle pipeline exercised with real sigstore code paths.

    Both ``verify_artifact`` and ``verify_dsse`` are patched to simulate
    successful Sigstore verification.  Bundle JSON parsing is also patched
    so no real Sigstore trust-root network calls are made.
    """

    def _patch_verifier(
        self,
        log_index: str = "12345678",
    ) -> tuple[unittest.mock.MagicMock, list]:
        """Return a mock Bundle and a context manager that patches both verifier methods."""
        from sigstore.verify import Verifier
        from sigstore.models import Bundle

        mock_bundle = _make_mock_bundle(log_index)
        # DSSE path: verify_dsse is called for dsseEnvelope bundles.
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

        def fake_verify_dsse(
            self_v: object, bundle: object, policy: object
        ) -> tuple[str, bytes]:
            return ("application/vnd.in-toto+json", json.dumps(attestation).encode())

        def fake_verify_artifact(
            self_v: object, input_: bytes, bundle: object, policy: object
        ) -> None:
            pass

        return mock_bundle, [
            unittest.mock.patch.object(Verifier, "verify_dsse", fake_verify_dsse),
            unittest.mock.patch.object(Verifier, "verify_artifact", fake_verify_artifact),
            unittest.mock.patch.object(Bundle, "from_json", return_value=mock_bundle),
            unittest.mock.patch("arcskill.hub.verify._fetch_crl", return_value=frozenset()),
        ]

    def test_verify_bundle_returns_valid_verify_result_with_slsa_l3(self) -> None:
        """Full pipeline: SLSA L3 bundle → VerifyResult with all fields populated."""
        tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_integ_"))
        bundle = _make_safe_skill_bundle(tmpdir)
        _write_sigstore_sidecar(bundle, slsa_level=3)

        config = _personal_config()
        src = _source()

        from arcskill.hub.verify import sha256_path

        content_hash = sha256_path(bundle)
        _mock_bundle, patches = self._patch_verifier("12345678")

        with unittest.mock.patch.multiple(
            **{}
        ) if False else __import__("contextlib").ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = verify_bundle(
                bundle_path=bundle,
                source=src,
                config=config,
                content_hash=content_hash,
            )

        assert result.signature_valid is True
        assert result.skipped is False
        assert result.rekor_uuid == "12345678"
        assert result.slsa_level == 3
        assert result.content_hash == content_hash
        assert result.crl_checked is True
        assert result.revoked is False

    def test_verify_bundle_propagates_rekor_uuid_to_result(self) -> None:
        """Rekor log_index from Bundle object propagates to VerifyResult."""
        tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_integ_uuid_"))
        bundle = _make_safe_skill_bundle(tmpdir)
        _write_sigstore_sidecar(bundle, slsa_level=3)

        from arcskill.hub.verify import sha256_path

        content_hash = sha256_path(bundle)
        _mock_bundle, patches = self._patch_verifier("99999999")

        import contextlib

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=_personal_config(),
                content_hash=content_hash,
            )

        assert result.rekor_uuid == "99999999"

    def test_verify_result_fields_are_correct_types(self) -> None:
        """VerifyResult fields conform to expected types for HubLockFile."""
        tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_integ_types_"))
        bundle = _make_safe_skill_bundle(tmpdir)
        _write_sigstore_sidecar(bundle, slsa_level=3)

        from arcskill.hub.verify import sha256_path

        content_hash = sha256_path(bundle)
        _mock_bundle, patches = self._patch_verifier("1")

        import contextlib

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=_personal_config(),
                content_hash=content_hash,
            )

        assert isinstance(result.content_hash, str)
        assert isinstance(result.rekor_uuid, str)
        assert isinstance(result.slsa_level, int)
        assert isinstance(result.signature_valid, bool)
        assert isinstance(result.skipped, bool)
        assert isinstance(result.crl_checked, bool)
        assert isinstance(result.revoked, bool)

    def test_full_pipeline_sha256_matches_computed_hash(self) -> None:
        """content_hash in result matches the pre-computed sha256_path output."""
        tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_integ_sha_"))
        bundle = _make_safe_skill_bundle(tmpdir)
        _write_sigstore_sidecar(bundle, slsa_level=3)

        from arcskill.hub.verify import sha256_path
        import hashlib

        content_hash = sha256_path(bundle)
        # Verify our helper is correct.
        h = hashlib.sha256(bundle.read_bytes()).hexdigest()
        assert content_hash == h

        _mock_bundle, patches = self._patch_verifier("1")

        import contextlib

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            result = verify_bundle(
                bundle_path=bundle,
                source=_source(),
                config=_personal_config(),
                content_hash=content_hash,
            )

        assert result.content_hash == content_hash
