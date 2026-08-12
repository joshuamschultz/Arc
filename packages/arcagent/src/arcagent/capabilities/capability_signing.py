"""SPEC-066 COMP-010 — the operator half of the capability trust gate.

The loader has refused unsigned capabilities above personal tier since SPEC-033,
but nothing shipped that could actually sign one: ``arc trust approve`` pins a
source hash the signature floor never lets :class:`~arctrust.TofuLayer` reach.
This module closes that gap.

:func:`sign` is ONE operator action with THREE durable effects, because passing
the gate needs all three and any one alone leaves the capability gated:

1. the detached ``.arcsig`` sidecar over the artifact bytes — clears the
   signature floor;
2. the signer's public key pinned into the agent's ``arcagent.toml`` as a
   trusted capability-verification key — makes that signature *verifiable*;
3. the TOFU pin under the loader's pin name — records the operator's approval
   of these exact bytes.

:func:`revoke` removes all three, returning the capability to gated.

This is an operator-only trust mutation on an agent's own config. No private key
material is logged, echoed, or persisted anywhere by these functions — only the
32-byte verify key ever reaches disk.

Both operator surfaces — ``arc trust`` and ``POST /api/trust/*`` — reach a
capability's trust through these two functions, which is why the audit record
(REQ-323, COMP-014) is emitted HERE rather than in each surface: one operator
action stays one audit record, and the two surfaces cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from arctrust import (
    AuditEvent,
    AuditSink,
    KeyPair,
    approve,
    disapprove,
    emit,
    hash_source,
    pin_key,
    unpin_key,
)

from arcagent.capabilities.artifact_signing import (
    SIDECAR_SUFFIX,
    load_signature,
    sidecar_path,
    write_signature,
)
from arcagent.capabilities.inventory import pin_name_for_path

#: Actor recorded when a revocation arrives without a named operator — a library
#: caller rather than one of the two operator surfaces. The action still happened
#: and still gets an actor, matching ``arcui.audit._UI_ACTOR_DID``.
_UNNAMED_OPERATOR_DID = "did:arc:operator:unnamed"


def sign(
    artifact: Path,
    *,
    signer_did: str,
    private_key: bytes,
    config_path: Path,
    audit_sink: AuditSink | None = None,
) -> None:
    """Sign ``artifact`` and record the trust it needs to load.

    Args:
        artifact: The gated artifact — a capability ``.py``, or a skill's
            ``SKILL.md``. Its current bytes are what gets signed and pinned, so
            a later edit invalidates both (drift is a hard stop, by design).
        signer_did: DID recorded as the signer, as the TOFU approver, and as the
            operator the audit record is attributed to.
        private_key: 32-byte Ed25519 seed. Used in-process only; never written.
        config_path: The agent's ``arcagent.toml`` — the sole persistence
            surface for both the trusted key and the TOFU pin.
        audit_sink: Where the ``capability.signed`` record lands. ``None`` — a
            library caller with no chain — records nothing and is not an error.

    Raises:
        OSError: The artifact or the config could not be read/written.
        ValueError: ``private_key`` is not a valid 32-byte Ed25519 seed.

    Errors propagate rather than being swallowed: a half-applied signing is a
    capability the operator believes is trusted and is not. The audit record is
    emitted last, so it only ever attests to a signing that fully landed.
    """
    content = artifact.read_bytes()
    source = content.decode("utf-8")
    public_key = KeyPair.from_seed(private_key).public_key
    write_signature(artifact, content, signer_did=signer_did, private_key=private_key)
    pin_key(config_path, public_key=public_key)
    approve(
        config_path,
        name=pin_name_for_path(artifact),
        source=source,
        approver=signer_did,
        timestamp=datetime.now(UTC).isoformat(),
    )
    _audit(
        audit_sink,
        action="capability.signed",
        outcome="signed",
        artifact=artifact,
        operator_did=signer_did,
        source_hash=hash_source(source),
    )


def revoke(
    artifact: Path,
    *,
    config_path: Path,
    operator_did: str = _UNNAMED_OPERATOR_DID,
    audit_sink: AuditSink | None = None,
) -> None:
    """Withdraw ``artifact``'s signature, trusted key, and TOFU pin.

    The exact inverse of :func:`sign`. The signer's key is unpinned only once no
    other artifact under the agent root is still signed by it — a shared
    operator key that another capability depends on must survive one revocation,
    or revoking a single tool would silently gate everything that operator signed.

    A missing sidecar, key, or pin is not an error: revocation is idempotent, so
    an interrupted one can always be completed by running it again.

    ``operator_did`` is the operator WITHDRAWING trust, not the one who granted
    it — the sidecar's signer is being removed, so recording it as the actor
    would name the wrong person in the audit record.
    """
    manifest = load_signature(artifact)
    sidecar_path(artifact).unlink(missing_ok=True)
    if manifest is not None and not _key_still_in_use(config_path.parent, manifest.public_key):
        unpin_key(config_path, public_key=bytes.fromhex(manifest.public_key))
    disapprove(config_path, name=pin_name_for_path(artifact))
    _audit(
        audit_sink,
        action="capability.signature_revoked",
        outcome="revoked",
        artifact=artifact,
        operator_did=operator_did,
        source_hash=_source_hash(artifact),
    )


def _audit(
    sink: AuditSink | None,
    *,
    action: str,
    outcome: str,
    artifact: Path,
    operator_did: str,
    source_hash: str | None,
) -> None:
    """Record one capability trust mutation (REQ-323) through ``arctrust.emit``.

    The single emission point for both operator surfaces. A ``None`` sink is a
    load-bearing case rather than a fault — the same fail-open posture
    :meth:`CapabilityLoader._audit` takes — so a library caller signing without a
    chain still signs.

    ``source_hash`` is :func:`arctrust.hash_source` over the artifact's current
    bytes: the SAME canonical hash the TOFU pin binds to, so an audit line and a
    pin line for one artifact read identically. Only the path, the operator DID,
    and that hash are recorded — never key material of any kind.
    """
    if sink is None:
        return
    emit(
        AuditEvent(
            actor_did=operator_did,
            action=action,
            target=str(artifact),
            outcome=outcome,
            payload_hash=source_hash,
        ),
        sink,
    )


def _source_hash(artifact: Path) -> str | None:
    """The canonical pin hash of ``artifact``'s current bytes, or ``None``.

    Revocation tolerates an artifact that is already gone (its pins are not), so
    an unreadable artifact costs the audit record its hash — never the
    revocation itself.
    """
    try:
        return hash_source(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _key_still_in_use(agent_root: Path, public_key_hex: str) -> bool:
    """True when any artifact still under ``agent_root`` is signed by that key.

    Runs after the revoked artifact's own sidecar is gone, so it sees exactly
    the set of signatures that must keep working.
    """
    for sidecar in agent_root.rglob(f"*{SIDECAR_SUFFIX}"):
        artifact = sidecar.with_name(sidecar.name.removesuffix(SIDECAR_SUFFIX))
        manifest = load_signature(artifact)
        if manifest is not None and manifest.public_key == public_key_hex:
            return True
    return False


__all__ = ["revoke", "sign"]
