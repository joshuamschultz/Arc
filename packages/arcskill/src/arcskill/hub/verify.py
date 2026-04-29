"""arcskill.hub.verify -- Sigstore / cosign bundle verification.

Pipeline
--------
1. Fulcio certificate chain check (OIDC identity via ``sigstore`` Verifier).
2. Rekor transparency-log inclusion proof (verified inside ``verify_artifact``
   or ``verify_dsse`` — both paths verify the Rekor inclusion proof as step 8).
3. SLSA in-toto attestation parsing and level check.
4. CRL check (fail-closed at federal if unreachable).

Sigstore Python API usage (v3+)
--------------------------------
- ``Verifier.production(offline=False)`` -- uses Sigstore production trust root
  (TUF-managed).  Pass ``offline=True`` to rely on cached trust root; useful
  in air-gapped environments after an initial online seeding.
- ``Verifier.verify_artifact(input_, bundle, policy)`` -- verifies a
  raw-bytes artifact against its Sigstore bundle.  Raises
  ``sigstore.errors.VerificationError`` on any failure (invalid cert chain,
  Rekor inclusion proof mismatch, policy violation).
- ``Verifier.verify_dsse(bundle, policy)`` -- verifies a DSSE envelope bundle
  (used for SLSA in-toto attestations) and returns ``(payload_type, payload)``.
- ``Identity(identity=..., issuer=...)`` -- policy that checks the Fulcio
  certificate's Subject Alternative Name (SAN) and OIDC issuer.
- ``UnsafeNoOp()`` -- NEVER used.  See _AuditedAnyIssuerPolicy below.
- ``Bundle.from_json(raw)`` -- deserialises a Sigstore bundle JSON file.

Identity policy tiers
----------------------
Tier controls *which* issuers are trusted, not *whether* to verify.

- Federal: ``Identity`` with both ``signer_identity`` and ``signer_issuer``
  required in source config.
- Enterprise/personal with configured identity: ``Identity`` with the
  configured values.
- Enterprise/personal without configured identity: ``_AuditedAnyIssuerPolicy``
  — Fulcio cert chain and Rekor inclusion proof are still fully verified; only
  the SAN/issuer check is relaxed.  An audit WARNING is emitted so the operator
  can track unconfigured sources.  UnsafeNoOp is NEVER used.

SLSA predicate validation
--------------------------
``_assert_slsa_predicate_type`` runs at ALL tiers.  At non-federal tiers an
unknown/foreign payload_type emits a WARNING rather than raising, because
non-federal signers may use custom attestation types.  However, payloads that
*claim* the in-toto content type but contain invalid JSON are rejected at all
tiers — a well-formed claim must have a well-formed payload.

Bundle sidecar convention
--------------------------
A signed skill bundle ``skill.tar.gz`` ships with a sidecar file at either
``skill.tar.gz.sigstore`` (preferred, Sigstore v2 convention) or
``skill.sigstore`` (legacy).  The installer downloads both files; this module
locates the sidecar automatically.

Availability handling
---------------------
- If the ``sigstore`` Python package is importable, full chain verification
  runs.
- If ``sigstore`` is NOT importable:
  - Federal tier: ``SigstoreUnavailable`` is raised immediately with an
    install hint.
  - Personal / enterprise tiers: verification is skipped with a logged
    WARNING and ``VerifyResult.skipped=True``.

This matches the SDD §3.8 contract without hard-requiring the optional
``arcskill[hub]`` extra at import time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from arcskill.hub.config import HubConfig, SkillSource
from arcskill.hub.errors import CRLUnreachable, SignatureInvalid, SigstoreUnavailable

if TYPE_CHECKING:
    from arctrust import AuditSink

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Expected predicate type for SLSA v1.1 provenance attestations.
SLSA_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"

#: CRL in-process cache: {crl_url: (expires_at_monotonic, revoked_hashes)}.
_crl_cache: dict[str, tuple[float, frozenset[str]]] = {}


# ---------------------------------------------------------------------------
# VerifyResult
# ---------------------------------------------------------------------------


class VerifyResult(BaseModel):
    """Output of the verification stage.

    Attributes
    ----------
    content_hash:
        SHA-256 hex of the bundle (for lock-file writing).
    rekor_uuid:
        Rekor log entry log_index (as string), or empty string if
        verification was skipped.
    slsa_level:
        SLSA Build Level detected (0-3).  0 if no attestation present.
    signature_valid:
        True if Sigstore verification passed (or was skip-allowed).
    skipped:
        True when sigstore is unavailable and tier policy allowed skip.
    crl_checked:
        True if CRL check was performed.
    revoked:
        True if the bundle hash appeared in the CRL.
    """

    content_hash: str
    rekor_uuid: str = ""
    slsa_level: int = 0
    signature_valid: bool = False
    skipped: bool = False
    crl_checked: bool = False
    revoked: bool = False


# ---------------------------------------------------------------------------
# _AuditedAnyIssuerPolicy — permissive identity policy with audit trail
# ---------------------------------------------------------------------------


class _AuditedAnyIssuerPolicy:
    """Sigstore verification policy that accepts any valid certificate chain.

    This is the policy used when a source has no ``signer_identity``
    configured at non-federal tiers.  It satisfies the sigstore
    ``VerificationPolicy`` protocol (duck-typed: has a ``verify`` method)
    while ensuring Rekor inclusion proof and Fulcio cert chain validation
    still execute — only the SAN/issuer identity check is skipped.

    UnsafeNoOp is NEVER used in Arc.  The distinction matters: UnsafeNoOp
    skips all cert-chain checks; this class only skips the *identity pin*
    check while the verifier itself still enforces chain validity and Rekor.

    An audit WARNING is emitted on every call so operators can identify
    sources running without identity pinning.
    """

    def __init__(self, source_name: str) -> None:
        self._source_name = source_name

    def verify(self, cert: Any) -> None:
        """Accept any valid Fulcio certificate.

        Called by the sigstore Verifier after cert chain + Rekor proof pass.
        We accept the cert but log so operators see the unpinned call.
        """
        logger.warning(
            "AUDIT: Identity unpinned for source=%r — certificate accepted "
            "without SAN/issuer check.  Configure signer_identity to pin.",
            self._source_name,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_bundle(
    bundle_path: Path,
    source: SkillSource,
    config: HubConfig,
    content_hash: str,
    *,
    audit_sink: AuditSink | None = None,
) -> VerifyResult:
    """Verify the Sigstore bundle at *bundle_path*.

    Parameters
    ----------
    bundle_path:
        Path to the downloaded skill bundle (tarball).
    source:
        Source configuration providing ``signer_identity`` and
        ``signer_issuer``.
    config:
        Hub configuration for tier and policy settings.
    content_hash:
        Pre-computed SHA-256 of the bundle (from fetch stage).
    audit_sink:
        Optional arctrust AuditSink for emitting structured audit events.
        When None, audit events are dropped.

    Returns
    -------
    VerifyResult
        All verification fields populated.

    Raises
    ------
    SigstoreUnavailable
        If the ``sigstore`` package is not installed and tier is federal.
    SignatureInvalid
        If signature or OIDC identity verification fails.
    CRLUnreachable
        If CRL is unreachable and ``fail_closed_if_unreachable=True``.
    """
    try:
        result = _run_sigstore(bundle_path, source, config, content_hash)
    except SignatureInvalid:
        _emit_audit(
            audit_sink=audit_sink,
            action="skill.signature.verify",
            target=bundle_path.name,
            outcome="deny",
            tier=config.tier.level,
            source_name=source.name,
        )
        raise
    except Exception:
        _emit_audit(
            audit_sink=audit_sink,
            action="skill.signature.verify",
            target=bundle_path.name,
            outcome="error",
            tier=config.tier.level,
            source_name=source.name,
        )
        raise

    # Enforce SLSA level requirement BEFORE CRL check so failures are
    # deterministic regardless of CRL availability.
    required_level = config.policy.require_slsa_level
    if not result.skipped and result.slsa_level < required_level:
        _emit_audit(
            audit_sink=audit_sink,
            action="skill.slsa.check",
            target=bundle_path.name,
            outcome="deny",
            tier=config.tier.level,
            source_name=source.name,
        )
        raise SignatureInvalid(
            f"SLSA level {result.slsa_level} is below required level "
            f"{required_level} (tier={config.tier.level!r})"
        )

    _emit_audit(
        audit_sink=audit_sink,
        action="skill.signature.verify",
        target=bundle_path.name,
        outcome="allow",
        tier=config.tier.level,
        source_name=source.name,
    )
    result = _check_crl(result, config)
    return result


def _emit_audit(
    *,
    audit_sink: AuditSink | None,
    action: str,
    target: str,
    outcome: str,
    tier: str,
    source_name: str,
) -> None:
    """Emit a structured audit event if a sink is provided.

    Swallows all errors — auditing must never interrupt the calling path.
    """
    if audit_sink is None:
        return
    try:
        from arctrust import AuditEvent, emit

        emit(
            AuditEvent(
                actor_did=f"arcskill.hub.verify:{source_name}",
                action=action,
                target=target,
                outcome=outcome,
                tier=tier,
            ),
            audit_sink,
        )
    except Exception:
        logger.warning("Failed to emit audit event for %s %s", action, target)


# ---------------------------------------------------------------------------
# Sigstore availability guard
# ---------------------------------------------------------------------------


def _sigstore_importable() -> bool:
    """Return True iff the ``sigstore`` package can be imported."""
    try:
        import sigstore  # noqa: F401

        return True
    except ImportError:
        return False


def _run_sigstore(
    bundle_path: Path,
    source: SkillSource,
    config: HubConfig,
    content_hash: str,
) -> VerifyResult:
    """Attempt Sigstore bundle verification.

    Falls back gracefully if ``sigstore`` is not installed, subject to
    tier policy.

    Raises
    ------
    SigstoreUnavailable
        Federal tier only: sigstore package is absent.
    """
    if not _sigstore_importable():
        if config.is_federal:
            raise SigstoreUnavailable(
                "sigstore Python package is not installed; "
                "federal tier requires full Fulcio + Rekor chain verification. "
                "Install the hub extra: pip install 'arcskill[hub]'"
            )
        logger.warning(
            "sigstore package not available; skipping signature verification "
            "(non-federal tier).  Install arcskill[hub] for full verification."
        )
        return VerifyResult(
            content_hash=content_hash,
            skipped=True,
            signature_valid=False,
        )

    return _sigstore_verify(bundle_path, source, config, content_hash)


# ---------------------------------------------------------------------------
# Production Sigstore verification
# ---------------------------------------------------------------------------


def _locate_bundle_sidecar(bundle_path: Path) -> Path | None:
    """Return the ``.sigstore`` sidecar path, or None if absent.

    Checks two conventional locations:
    1. ``<bundle>.sigstore``  (e.g. ``skill.tar.gz.sigstore``)
    2. ``<stem>.sigstore``    (e.g. ``skill.sigstore``)
    """
    candidate1 = bundle_path.parent / (bundle_path.name + ".sigstore")
    if candidate1.exists():
        return candidate1
    candidate2 = bundle_path.parent / (bundle_path.stem + ".sigstore")
    if candidate2.exists():
        return candidate2
    return None


def _sigstore_verify(
    bundle_path: Path,
    source: SkillSource,
    config: HubConfig,
    content_hash: str,
) -> VerifyResult:
    """Run full Sigstore verification using the ``sigstore`` Python package.

    Verification steps performed
    ----------------------------
    1. Load and validate the Sigstore bundle JSON sidecar.
    2. Detect bundle type: ``hashedrekord`` (raw artifact) or ``dsse``
       (DSSE envelope / SLSA attestation).
    3. Build the ``VerificationPolicy`` from ``source.signer_identity`` and
       ``source.signer_issuer``.  Federal installs MUST have both configured;
       non-federal falls back to ``UnsafeNoOp`` when unconfigured.
    4. Call ``Verifier.production().verify_artifact()`` or
       ``Verifier.production().verify_dsse()`` depending on bundle type.
       Both paths verify: Fulcio cert chain, OIDC identity (via policy),
       and Rekor inclusion proof.
    5. Extract Rekor log_index and SLSA level from bundle metadata.

    Raises
    ------
    SignatureInvalid
        On any verification failure.
    """
    from sigstore.errors import VerificationError
    from sigstore.models import Bundle
    from sigstore.verify import Verifier
    from sigstore.verify.policy import Identity

    # -- Step 1: locate sidecar -----------------------------------------------
    bundle_file = _locate_bundle_sidecar(bundle_path)
    if bundle_file is None:
        if config.is_federal or config.policy.require_signature:
            raise SignatureInvalid(
                f"No Sigstore bundle sidecar found for {bundle_path.name}. "
                f"Expected: {bundle_path.name}.sigstore or "
                f"{bundle_path.stem}.sigstore"
            )
        logger.warning(
            "No Sigstore bundle found for %s; skipping verification",
            bundle_path.name,
        )
        return VerifyResult(
            content_hash=content_hash,
            skipped=True,
            signature_valid=False,
        )

    # -- Step 2: parse bundle JSON --------------------------------------------
    try:
        bundle_raw = bundle_file.read_text(encoding="utf-8")
        bundle_data: dict[str, Any] = json.loads(bundle_raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise SignatureInvalid(f"Cannot parse Sigstore bundle: {exc}") from exc

    # -- Step 3: build policy -------------------------------------------------
    # Federal tier MUST have signer_identity + signer_issuer in source config.
    if config.is_federal and (not source.signer_identity or not source.signer_issuer):
        raise SignatureInvalid(
            "Federal tier requires signer_identity and signer_issuer in "
            "source config but one or both are missing for "
            f"source={source.name!r}."
        )

    if source.signer_identity:
        # Build OIDC identity policy: checks Fulcio cert SAN + issuer.
        policy: Any = Identity(
            identity=source.signer_identity,
            issuer=source.signer_issuer if source.signer_issuer else None,
        )
    else:
        # Non-federal unconfigured source: Rekor inclusion proof and Fulcio
        # cert chain are still fully verified.  Only the SAN/issuer check is
        # relaxed via _AuditedAnyIssuerPolicy, which logs an audit WARNING so
        # operators know this source is running without identity pinning.
        #
        # UnsafeNoOp is NEVER used — it would skip the cert chain check.
        logger.warning(
            "AUDIT WARNING: No signer_identity configured for source=%r; "
            "Rekor + Fulcio cert chain verified but OIDC identity unpinned. "
            "Configure signer_identity to pin the expected signer.",
            source.name,
        )
        policy = _AuditedAnyIssuerPolicy(source_name=source.name)

    # -- Step 4: run Sigstore verifier ----------------------------------------
    try:
        bundle = Bundle.from_json(bundle_raw)
        verifier = Verifier.production()

        # Detect bundle type from the tlog entry kind.
        bundle_kind = _detect_bundle_kind(bundle_data)

        if bundle_kind == "dsse":
            # DSSE path: in-toto attestation / SLSA provenance bundle.
            # Returns (payload_type, payload_bytes).
            payload_type, payload_bytes = verifier.verify_dsse(
                bundle=bundle,
                policy=policy,
            )
            # Validate predicate type.
            _assert_slsa_predicate_type(payload_type, payload_bytes, config)
        else:
            # hashedrekord path: raw artifact bundle.
            artifact_bytes = bundle_path.read_bytes()
            verifier.verify_artifact(
                input_=artifact_bytes,
                bundle=bundle,
                policy=policy,
            )

    except VerificationError as exc:
        raise SignatureInvalid(f"Sigstore verification failed: {exc}") from exc
    except Exception as exc:
        # Catch-all: InvalidBundle, TUF errors, network errors, etc.
        raise SignatureInvalid(f"Sigstore verification failed (unexpected error): {exc}") from exc

    # -- Step 5: extract metadata from verified bundle ------------------------
    rekor_uuid = _extract_rekor_uuid_from_bundle(bundle) or _extract_rekor_uuid(bundle_data)
    slsa_level = _extract_slsa_level(bundle_data)

    return VerifyResult(
        content_hash=content_hash,
        rekor_uuid=rekor_uuid,
        slsa_level=slsa_level,
        signature_valid=True,
        skipped=False,
    )


def _detect_bundle_kind(bundle_data: dict[str, Any]) -> str:
    """Return ``"dsse"`` if the bundle wraps a DSSE envelope, else ``"hashedrekord"``."""
    try:
        tlog_entries = bundle_data.get("verificationMaterial", {}).get("tlogEntries", [])
        if tlog_entries:
            # The kind is encoded in the tlog entry body.
            body_b64 = tlog_entries[0].get("canonicalizedBody", "")
            if body_b64:
                body = json.loads(base64.b64decode(body_b64 + "=="))
                kind = body.get("kind", "")
                if kind == "dsse":
                    return "dsse"
    except Exception:  # noqa: S110 -- best-effort parse; failures default to hashedrekord
        pass
    # Also check for dsseEnvelope in verificationMaterial directly (newer bundles).
    if bundle_data.get("verificationMaterial", {}).get("dsseEnvelope"):
        return "dsse"
    return "hashedrekord"


def _assert_slsa_predicate_type(
    payload_type: str,
    payload_bytes: bytes,
    config: HubConfig,
) -> None:
    """Validate DSSE payload type and SLSA predicate at ALL tiers.

    Tier controls *stringency*, not *whether* to validate:

    - Federal tier: both ``payload_type`` and ``predicateType`` MUST be
      valid SLSA values; any deviation raises SignatureInvalid.

    - Non-federal tier: payloads claiming the in-toto content type
      (``application/vnd.in-toto+json``) must contain valid JSON; a
      malformed JSON payload raises SignatureInvalid regardless of tier
      because a well-formed claim must have a well-formed payload.
      An unrecognised/foreign payload_type is warned and accepted.

    The previous early-return at non-federal (``if not config.is_federal:
    return``) has been removed — it was a tier-bypass that allowed
    tampered DSSE payloads to pass without validation.

    Raises
    ------
    SignatureInvalid
        Federal: payload type not SLSA, predicateType not SLSA, or invalid JSON.
        All tiers: payload_type is in-toto but payload_bytes is not valid JSON.
    """
    _is_intoto = payload_type in (
        "application/vnd.in-toto+json",
        SLSA_PREDICATE_TYPE,
    )

    if config.is_federal:
        if not _is_intoto:
            raise SignatureInvalid(
                f"Federal tier requires SLSA in-toto attestation; "
                f"got payload_type={payload_type!r}"
            )
    else:
        # Non-federal: warn on unrecognised types but don't reject them.
        # Custom signers may use non-SLSA attestation formats.
        if not _is_intoto:
            logger.warning(
                "AUDIT WARNING: Unrecognised DSSE payload_type=%r for non-federal "
                "tier — accepted but not validated.  "
                "Configure a signer_identity to enforce payload type.",
                payload_type,
            )
            return

    # Reached here when payload_type is in-toto (all tiers) or federal-only path.
    # Parse the JSON payload — malformed payload is rejected at all tiers.
    try:
        attestation: dict[str, Any] = json.loads(payload_bytes)
        predicate_type: str = attestation.get("predicateType", "")
        if config.is_federal and not predicate_type.startswith("https://slsa.dev/provenance/"):
            raise SignatureInvalid(
                f"Federal tier requires predicateType starting with "
                f"'https://slsa.dev/provenance/'; got {predicate_type!r}"
            )
        if (
            not config.is_federal
            and predicate_type
            and not predicate_type.startswith("https://slsa.dev/provenance/")
        ):
            logger.warning(
                "AUDIT: non-SLSA predicateType=%r in in-toto payload "
                "for non-federal tier — accepted with warning.",
                predicate_type,
            )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SignatureInvalid(f"Cannot parse SLSA attestation payload: {exc}") from exc


# ---------------------------------------------------------------------------
# Metadata extraction helpers
# ---------------------------------------------------------------------------


def _extract_rekor_uuid_from_bundle(bundle: Any) -> str:
    """Extract Rekor log_index from a live Bundle object.

    Uses ``bundle.log_entry._inner.log_index`` (sigstore internal API).
    Returns empty string on any access failure.
    """
    try:
        log_index = bundle.log_entry._inner.log_index
        return str(log_index) if log_index is not None else ""
    except Exception:
        return ""


def _extract_rekor_uuid(bundle_data: dict[str, Any]) -> str:
    """Extract the Rekor log entry index from the bundle JSON, if present.

    Parses the ``verificationMaterial.tlogEntries[0].logIndex`` field
    from the Sigstore bundle v0.3+ format.  Returns empty string when
    absent.
    """
    try:
        tlog_entries = bundle_data.get("verificationMaterial", {}).get("tlogEntries", [])
        if tlog_entries:
            return str(tlog_entries[0].get("logIndex", ""))
    except (KeyError, IndexError, TypeError):
        pass
    return ""


def _extract_slsa_level(bundle_data: dict[str, Any]) -> int:
    """Parse the SLSA Build Level from an in-toto attestation in the bundle.

    Decodes the base64-encoded DSSE payload from
    ``verificationMaterial.dsseEnvelope.payload`` and inspects the
    ``predicate.buildType`` and ``predicate.runDetails.builder.id`` fields.

    Returns
    -------
    int
        SLSA Build Level (1, 2, or 3) if detectable; 0 if absent.
    """
    try:
        dsse_envelope = bundle_data.get("verificationMaterial", {}).get("dsseEnvelope", {})
        if not dsse_envelope:
            return 0
        payload_b64 = dsse_envelope.get("payload", "")
        if not payload_b64:
            return 0
        # Add padding to avoid base64 decode errors on odd-length strings.
        payload_bytes = base64.b64decode(payload_b64 + "==")
        attestation: dict[str, Any] = json.loads(payload_bytes)
        predicate = attestation.get("predicate", {})
        build_type: str = predicate.get("buildType", "")

        if not ("slsa.dev" in build_type or "slsa-github-generator" in build_type):
            if "slsa" in build_type.lower():
                return 1
            return 0

        # Inspect builder ID for explicit level annotation.
        run_details = predicate.get("runDetails", {})
        builder = run_details.get("builder", {})
        builder_id: str = builder.get("id", "")

        if "buildLevel@v1=3" in builder_id or "level3" in builder_id.lower():
            return 3
        if "buildLevel@v1=2" in builder_id or "level2" in builder_id.lower():
            return 2

        # slsa-github-generator builder without explicit level is SLSA L3
        # (the generator itself is SLSA L3 certified).
        if "slsa-github-generator" in builder_id:
            return 3

        # SLSA domain in build_type without recognised builder → default L1.
        return 1

    except Exception:  # noqa: S110
        pass
    return 0


# ---------------------------------------------------------------------------
# CRL check
# ---------------------------------------------------------------------------


def _check_crl(result: VerifyResult, config: HubConfig) -> VerifyResult:
    """Check the CRL and annotate *result* with ``crl_checked`` / ``revoked``.

    Caches the CRL in-process for ``crl_refresh_interval_seconds``.
    Fail-closed at federal when unreachable.

    Raises
    ------
    CRLUnreachable
        Federal tier: CRL endpoint unreachable.
    """
    crl_cfg = config.revocation
    now = time.monotonic()

    cached = _crl_cache.get(crl_cfg.crl_url)
    if cached and now < cached[0]:
        revoked_hashes = cached[1]
    else:
        try:
            revoked_hashes = _fetch_crl(crl_cfg.crl_url)
            _crl_cache[crl_cfg.crl_url] = (
                now + crl_cfg.crl_refresh_interval_seconds,
                revoked_hashes,
            )
        except (urllib.error.URLError, OSError) as exc:
            if crl_cfg.fail_closed_if_unreachable:
                raise CRLUnreachable(
                    f"CRL endpoint {crl_cfg.crl_url!r} unreachable: {exc}"
                ) from exc
            logger.warning(
                "CRL unreachable (%s); skipping check (non-fail-closed tier)",
                exc,
            )
            return result.model_copy(update={"crl_checked": False})

    is_revoked = result.content_hash in revoked_hashes
    if is_revoked:
        logger.error(
            "Skill bundle hash %s is in the CRL; install blocked",
            result.content_hash[:12],
        )

    return result.model_copy(update={"crl_checked": True, "revoked": is_revoked})


def _fetch_crl(url: str) -> frozenset[str]:
    """Fetch and parse a JSON CRL.  Returns the set of revoked content hashes.

    Accepts two JSON schemas:

    New format (preferred)::

        {"revoked": ["sha256hex1", "sha256hex2", ...]}

    Legacy format (flat list)::

        ["sha256hex1", "sha256hex2", ...]
    """
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        data = json.loads(resp.read())

    if isinstance(data, list):
        return frozenset(str(h) for h in data)

    revoked = data.get("revoked", [])
    return frozenset(str(h) for h in revoked)


# ---------------------------------------------------------------------------
# Utility: standalone hash check (used in tests and installer)
# ---------------------------------------------------------------------------


def sha256_path(path: Path) -> str:
    """Return lowercase hex SHA-256 of the file at *path*."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
