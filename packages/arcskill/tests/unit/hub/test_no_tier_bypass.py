"""Regression tests for the three tier-bypass removals.

Each bypass allowed non-federal tiers to skip verification steps that are
required at ALL tiers per the four-pillars rule. These tests assert the
correct post-fix behaviour and are expected to FAIL before the fix is applied.

Bypass 1 (verify.py): UnsafeNoOp policy used when signer_identity unconfigured
    → After fix: AnyIssuer (cert-chain + Rekor verified; any issuer accepted)
      must be used; UnsafeNoOp must NEVER appear for any tier.

Bypass 2 (verify.py): _assert_slsa_predicate_type returns early at non-federal
    → After fix: SLSA predicate type is validated at all tiers (non-federal
      logs a warning for unrecognised types; tampered type triggers SignatureInvalid).

Bypass 3 (dry_run.py): skip_sandbox=True succeeds at non-federal without sandbox
    → After fix: skip_sandbox raises SandboxRequired at ALL tiers; sandbox
      always runs; tier determines *which* backend, not *whether* to sandbox.

Audit migration:
    verify_bundle and run_dry_run accept an optional ``audit_sink`` parameter.
    When provided, AuditEvents are emitted for key outcomes (sig verify
    success/fail, sandbox-skip warning, CRL fetch fail).
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest.mock
from pathlib import Path

import pytest
from arcskill.hub.config import HubConfig, HubPolicy, RevocationConfig, SkillSource, TierPolicy
from arcskill.hub.dry_run import DryRunResult, run_dry_run
from arcskill.hub.errors import SandboxRequired, SignatureInvalid
from arcskill.hub.verify import VerifyResult, verify_bundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _personal_config(*, require_signature: bool = True) -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
        policy=HubPolicy(require_signature=require_signature, require_slsa_level=0),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )


def _enterprise_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="enterprise"),
        policy=HubPolicy(require_slsa_level=0),
        revocation=RevocationConfig(
            crl_url="https://test.example.com/crl.json",
            fail_closed_if_unreachable=False,
        ),
    )


def _source_no_identity() -> SkillSource:
    """Source with NO signer_identity — the bypass trigger for Bypass 1."""
    return SkillSource(
        name="community-skill",
        type="registry",
        url="https://skills.example.com",
        trust="community",
        signer_identity=None,
        signer_issuer=None,
    )


def _source_with_identity() -> SkillSource:
    return SkillSource(
        name="arc-official",
        type="github",
        repo="arc-foundation/skills",
        signer_identity="https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main",
        signer_issuer="https://token.actions.githubusercontent.com",
    )


def _make_bundle_with_sidecar(
    bundle_bytes: bytes = b"fake bundle",
    sidecar_content: str | None = None,
) -> tuple[Path, Path]:
    """Return (bundle_path, sidecar_path). Creates a minimal .sigstore sidecar."""
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_bypass_test_"))
    bundle_path = tmpdir / "skill.tar.gz"
    bundle_path.write_bytes(bundle_bytes)

    if sidecar_content is None:
        # Minimal structurally valid bundle JSON (will fail real Sigstore verify
        # but lets us test the policy selection path).
        sidecar_content = json.dumps({
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {"tlogEntries": []},
            "dsseEnvelope": {},
        })

    sidecar_path = tmpdir / "skill.tar.gz.sigstore"
    sidecar_path.write_text(sidecar_content, encoding="utf-8")
    return bundle_path, sidecar_path


def _make_tarball(files: dict[str, str]) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_dr_bypass_"))
    bundle = tmpdir / "skill.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return bundle


# ---------------------------------------------------------------------------
# BYPASS 1 — UnsafeNoOp must not be used at any tier
# ---------------------------------------------------------------------------


def test_bypass1_audited_policy_used_at_personal_no_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bypass 1: personal + no signer_identity must use _AuditedAnyIssuerPolicy.

    After fix: _sigstore_verify uses _AuditedAnyIssuerPolicy instead of
    UnsafeNoOp, and emits an AUDIT WARNING so operators can track unconfigured
    sources. Rekor + Fulcio cert chain still verified.
    """
    import logging
    import sys

    from arcskill.hub.verify import _AuditedAnyIssuerPolicy

    bundle_path, _ = _make_bundle_with_sidecar()
    config = _personal_config()
    source = _source_no_identity()

    captured_policy: list[object] = []

    def _spy_verify_artifact(
        input_: bytes,
        bundle: object,
        policy: object,
    ) -> None:
        captured_policy.append(policy)

    # Build minimal mock sigstore modules (same pattern as test_verify_internal.py).
    mock_errors = unittest.mock.MagicMock()
    mock_errors.VerificationError = type("VerificationError", (Exception,), {})
    mock_bundle_instance = unittest.mock.MagicMock()
    mock_bundle_class = unittest.mock.MagicMock()
    mock_bundle_class.from_json.return_value = mock_bundle_instance
    mock_verifier = unittest.mock.MagicMock()
    mock_verifier.verify_artifact.side_effect = _spy_verify_artifact
    mock_verifier_class = unittest.mock.MagicMock()
    mock_verifier_class.production.return_value = mock_verifier
    mock_identity = unittest.mock.MagicMock()

    fake_mods = {
        "sigstore": unittest.mock.MagicMock(),
        "sigstore.errors": mock_errors,
        "sigstore.models": unittest.mock.MagicMock(Bundle=mock_bundle_class),
        "sigstore.verify": unittest.mock.MagicMock(Verifier=mock_verifier_class),
        "sigstore.verify.policy": unittest.mock.MagicMock(Identity=mock_identity),
    }

    with caplog.at_level(logging.WARNING, logger="arcskill.hub.verify"):
        with unittest.mock.patch.dict(sys.modules, fake_mods):
            with unittest.mock.patch(
                "arcskill.hub.verify._detect_bundle_kind", return_value="hashedrekord"
            ):
                with unittest.mock.patch(
                    "arcskill.hub.verify._extract_rekor_uuid_from_bundle", return_value=""
                ):
                    with unittest.mock.patch(
                        "arcskill.hub.verify._extract_slsa_level", return_value=0
                    ):
                        with unittest.mock.patch(
                            "arcskill.hub.verify._fetch_crl", return_value=frozenset()
                        ):
                            verify_bundle(
                                bundle_path=bundle_path,
                                source=source,
                                config=config,
                                content_hash="abc",
                            )

    # Policy passed to verifier must be _AuditedAnyIssuerPolicy.
    assert len(captured_policy) == 1, (
        f"Expected verifier to be called once with a policy, got: {captured_policy}"
    )
    assert isinstance(captured_policy[0], _AuditedAnyIssuerPolicy), (
        f"Expected _AuditedAnyIssuerPolicy, got {type(captured_policy[0]).__name__}"
    )

    # Audit warning must be emitted.
    assert any("AUDIT WARNING" in r.message for r in caplog.records), (
        "No AUDIT WARNING logged for unconfigured signer_identity"
    )


def test_bypass1_tampered_signature_rejected_at_personal() -> None:
    """Bypass 1: personal tier with NO signer_identity still rejects tampered sigs.

    After fix: verification runs at all tiers. A Sigstore VerificationError
    (tampered signature, expired cert, Rekor proof mismatch) must raise
    SignatureInvalid — even at personal tier with no signer_identity.
    """
    import sys

    bundle_path, _ = _make_bundle_with_sidecar()
    config = _personal_config()
    source = _source_no_identity()

    # Use a real exception type (not sigstore.errors.VerificationError) since
    # sigstore is not installed — the code wraps any exception from the verifier
    # in SignatureInvalid via the broad ``except Exception`` catch.
    class _FakeVerificationError(Exception):
        pass

    mock_errors = unittest.mock.MagicMock()
    mock_errors.VerificationError = _FakeVerificationError
    mock_bundle_class = unittest.mock.MagicMock()
    mock_bundle_class.from_json.return_value = unittest.mock.MagicMock()
    mock_verifier = unittest.mock.MagicMock()
    mock_verifier.verify_artifact.side_effect = _FakeVerificationError(
        "certificate has expired"
    )
    mock_verifier_class = unittest.mock.MagicMock()
    mock_verifier_class.production.return_value = mock_verifier
    mock_identity = unittest.mock.MagicMock()

    fake_mods = {
        "sigstore": unittest.mock.MagicMock(),
        "sigstore.errors": mock_errors,
        "sigstore.models": unittest.mock.MagicMock(Bundle=mock_bundle_class),
        "sigstore.verify": unittest.mock.MagicMock(Verifier=mock_verifier_class),
        "sigstore.verify.policy": unittest.mock.MagicMock(Identity=mock_identity),
    }

    with unittest.mock.patch.dict(sys.modules, fake_mods):
        with unittest.mock.patch(
            "arcskill.hub.verify._detect_bundle_kind", return_value="hashedrekord"
        ):
            with pytest.raises(SignatureInvalid):
                verify_bundle(
                    bundle_path=bundle_path,
                    source=source,
                    config=config,
                    content_hash="abc",
                )


def test_bypass1_missing_sidecar_personal_with_require_signature_raises() -> None:
    """Missing .sigstore sidecar at personal with require_signature=True → SignatureInvalid.

    The `config.policy.require_signature` gate must work regardless of tier.
    This exercises the sidecar-missing path inside _sigstore_verify.
    """
    import sys

    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_bypass1_"))
    bundle_path = tmpdir / "skill.tar.gz"
    bundle_path.write_bytes(b"fake bundle")
    # No .sigstore sidecar file created.

    config = _personal_config(require_signature=True)
    source = _source_no_identity()

    # Mock sigstore to be importable so we reach the sidecar check.
    mock_errors = unittest.mock.MagicMock()
    mock_errors.VerificationError = type("VerificationError", (Exception,), {})
    mock_identity = unittest.mock.MagicMock()

    fake_mods = {
        "sigstore": unittest.mock.MagicMock(),
        "sigstore.errors": mock_errors,
        "sigstore.models": unittest.mock.MagicMock(),
        "sigstore.verify": unittest.mock.MagicMock(),
        "sigstore.verify.policy": unittest.mock.MagicMock(Identity=mock_identity),
    }

    with unittest.mock.patch.dict(sys.modules, fake_mods):
        with pytest.raises(SignatureInvalid):
            verify_bundle(
                bundle_path=bundle_path,
                source=source,
                config=config,
                content_hash="abc",
            )


# ---------------------------------------------------------------------------
# BYPASS 2 — SLSA predicate type must be validated at non-federal tiers
# ---------------------------------------------------------------------------


def test_bypass2_slsa_predicate_validation_runs_at_enterprise() -> None:
    """Bypass 2: enterprise tier must not early-return from _assert_slsa_predicate_type.

    After fix: the function validates what it can at all tiers. An unknown
    payload_type at enterprise should log a warning, not silently succeed.
    """
    from arcskill.hub.verify import _assert_slsa_predicate_type

    config = _enterprise_config()

    # This should NOT silently return — it should at minimum log a warning
    # and/or validate the payload_type value.
    # Post-fix: calling with a garbage payload_type at non-federal should
    # not simply pass; it should warn or validate.
    with unittest.mock.patch("arcskill.hub.verify.logger") as mock_logger:
        _assert_slsa_predicate_type(
            payload_type="text/plain",  # Invalid type
            payload_bytes=b"not-json",
            config=config,
        )
        # Post-fix: a warning must have been emitted for unrecognised payload_type.
        assert mock_logger.warning.called or mock_logger.info.called, (
            "_assert_slsa_predicate_type silently skipped validation at non-federal tier. "
            "Post-fix it must emit a warning for unrecognised/invalid payload types."
        )


def test_bypass2_tampered_predicate_type_raises_at_personal() -> None:
    """Bypass 2: tampered predicateType in DSSE payload raises at all tiers.

    Post-fix: even at personal tier, a bundle claiming to be SLSA but with
    a tampered/invalid predicateType must raise SignatureInvalid (not silently pass).
    The distinction: a completely foreign payload_type is warned; a payload_type
    that *claims* to be SLSA but has malformed content is rejected.
    """
    from arcskill.hub.verify import _assert_slsa_predicate_type

    config = _personal_config()

    # A payload_type that looks like in-toto but has malformed JSON payload.
    with pytest.raises(SignatureInvalid):
        _assert_slsa_predicate_type(
            payload_type="application/vnd.in-toto+json",
            payload_bytes=b"not valid json {{{",
            config=config,
        )


def test_bypass2_valid_slsa_predicate_accepted_at_personal() -> None:
    """Bypass 2 (positive case): valid SLSA predicate is accepted at personal tier."""
    from arcskill.hub.verify import _assert_slsa_predicate_type

    config = _personal_config()

    payload = json.dumps({
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {},
    }).encode()

    # Must not raise.
    _assert_slsa_predicate_type(
        payload_type="application/vnd.in-toto+json",
        payload_bytes=payload,
        config=config,
    )


# ---------------------------------------------------------------------------
# BYPASS 3 — skip_sandbox must not be honoured at any tier
# ---------------------------------------------------------------------------


def test_bypass3_skip_sandbox_raises_at_personal() -> None:
    """Bypass 3: skip_sandbox=True must raise SandboxRequired at personal tier.

    Post-fix: skip_sandbox is removed or raises at ALL tiers. Tier controls
    *which* backend runs, not *whether* to sandbox.
    """
    bundle = _make_tarball({"skill.py": "# skill\n"})
    config = _personal_config()

    with unittest.mock.patch(
        "arcskill.hub.dry_run.is_firecracker_available", return_value=False
    ):
        with unittest.mock.patch(
            "arcskill.hub.dry_run._docker_available", return_value=False
        ):
            # Post-fix: skip_sandbox=True should raise SandboxRequired, not return
            # a passed result with skipped=True.
            with pytest.raises(SandboxRequired):
                run_dry_run(bundle, config, skip_sandbox=True)


def test_bypass3_skip_sandbox_raises_at_enterprise() -> None:
    """Bypass 3: skip_sandbox=True must raise SandboxRequired at enterprise tier."""
    bundle = _make_tarball({"skill.py": "# skill\n"})
    config = _enterprise_config()

    with unittest.mock.patch(
        "arcskill.hub.dry_run.is_firecracker_available", return_value=False
    ):
        with unittest.mock.patch(
            "arcskill.hub.dry_run._docker_available", return_value=False
        ):
            with pytest.raises(SandboxRequired):
                run_dry_run(bundle, config, skip_sandbox=True)


def test_bypass3_sandbox_runs_when_docker_available_at_personal() -> None:
    """Bypass 3 (positive): when Docker is available, personal tier sandbox runs normally.

    Post-fix: removing skip_sandbox means the Docker path runs when available.
    """
    bundle = _make_tarball({"skill.py": "# skill\n"})
    config = _personal_config()

    mock_result = DryRunResult(
        passed=True,
        stdout="OK",
        exit_code=0,
        backend_used="docker",
        skipped=False,
    )

    with unittest.mock.patch(
        "arcskill.hub.dry_run.is_firecracker_available", return_value=False
    ):
        with unittest.mock.patch(
            "arcskill.hub.dry_run._docker_available", return_value=True
        ):
            with unittest.mock.patch("arcskill.hub.dry_run._run_docker", return_value=mock_result):
                # Patch _run_in_sandbox to avoid async complexity.
                with unittest.mock.patch(
                    "arcskill.hub.dry_run._run_in_sandbox",
                    return_value=mock_result,
                ):
                    result = run_dry_run(bundle, config)

    assert result.backend_used == "docker"
    assert result.skipped is False
    assert result.passed is True


# ---------------------------------------------------------------------------
# AUDIT MIGRATION — AuditEvents emitted via arctrust.audit.emit
# ---------------------------------------------------------------------------


def test_audit_event_emitted_on_signature_verify_success() -> None:
    """An AuditEvent is emitted when signature verification succeeds.

    Post-fix: verify_bundle accepts an optional ``audit_sink`` argument.
    When a sink is provided, an AuditEvent with outcome='allow' is emitted
    after a successful signature verification.
    """
    from arctrust import AuditEvent

    bundle_path, _ = _make_bundle_with_sidecar()
    config = _personal_config()
    source = _source_with_identity()

    captured_events: list[AuditEvent] = []

    class CaptureSink:
        def write(self, event: AuditEvent) -> None:
            captured_events.append(event)

    sink = CaptureSink()

    with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
        mock_run.return_value = VerifyResult(
            content_hash="deadbeef",
            rekor_uuid="12345",
            slsa_level=3,
            signature_valid=True,
        )
        with unittest.mock.patch(
            "arcskill.hub.verify._fetch_crl", return_value=frozenset()
        ):
            result = verify_bundle(
                bundle_path=bundle_path,
                source=source,
                config=config,
                content_hash="deadbeef",
                audit_sink=sink,
            )

    assert result.signature_valid is True
    assert len(captured_events) >= 1, (
        "No AuditEvent emitted on successful signature verify. "
        "Post-fix: verify_bundle must emit an AuditEvent with outcome='allow' "
        "via the injected audit_sink."
    )
    outcomes = [e.outcome for e in captured_events]
    assert "allow" in outcomes, (
        f"Expected at least one AuditEvent with outcome='allow'. Got: {outcomes}"
    )


def test_audit_event_emitted_on_signature_verify_failure() -> None:
    """An AuditEvent is emitted when signature verification fails.

    Post-fix: verify_bundle emits an AuditEvent with outcome='deny' (or
    'error') before raising SignatureInvalid.
    """
    from arctrust import AuditEvent

    bundle_path, _ = _make_bundle_with_sidecar()
    config = _personal_config()
    source = _source_with_identity()

    captured_events: list[AuditEvent] = []

    class CaptureSink:
        def write(self, event: AuditEvent) -> None:
            captured_events.append(event)

    sink = CaptureSink()

    with unittest.mock.patch("arcskill.hub.verify._run_sigstore") as mock_run:
        mock_run.side_effect = SignatureInvalid("tampered")
        with pytest.raises(SignatureInvalid):
            verify_bundle(
                bundle_path=bundle_path,
                source=source,
                config=config,
                content_hash="deadbeef",
                audit_sink=sink,
            )

    assert len(captured_events) >= 1, (
        "No AuditEvent emitted on failed signature verify. "
        "Post-fix: verify_bundle must emit an AuditEvent with outcome='deny' "
        "before re-raising SignatureInvalid."
    )
    outcomes = [e.outcome for e in captured_events]
    assert any(o in ("deny", "error") for o in outcomes), (
        f"Expected at least one AuditEvent with outcome='deny' or 'error'. Got: {outcomes}"
    )


def test_audit_event_emitted_on_sandbox_unavailable_warning() -> None:
    """An AuditEvent (outcome='warn') is emitted when no sandbox backend is available.

    Post-fix: when Firecracker and Docker are both unavailable at non-federal
    (scan-only fallback path), run_dry_run emits an AuditEvent with
    outcome='warn' AND the skip_sandbox path has been removed (so the code
    reaches the fallback, not the early-return).
    """
    from arctrust import AuditEvent

    bundle = _make_tarball({"skill.py": "# skill\n"})
    config = _personal_config()

    captured_events: list[AuditEvent] = []

    class CaptureSink:
        def write(self, event: AuditEvent) -> None:
            captured_events.append(event)

    sink = CaptureSink()

    with unittest.mock.patch(
        "arcskill.hub.dry_run.is_firecracker_available", return_value=False
    ):
        with unittest.mock.patch(
            "arcskill.hub.dry_run._docker_available", return_value=False
        ):
            # Post-fix: sandbox-unavailable is a SandboxRequired at personal too,
            # OR the fallback path emits an audit warning. Test whichever path lands.
            try:
                run_dry_run(bundle, config, audit_sink=sink)
                # If it succeeded (scan-only fallback), check the audit event.
                assert len(captured_events) >= 1, (
                    "No AuditEvent emitted on sandbox-unavailable fallback. "
                    "Post-fix must emit an audit event for the scan-only path."
                )
                outcomes = [e.outcome for e in captured_events]
                assert any(o in ("warn", "allow") for o in outcomes)
            except SandboxRequired:
                # Post-fix may raise SandboxRequired at all tiers.
                # Either outcome is valid; just confirm the test ran.
                pass
