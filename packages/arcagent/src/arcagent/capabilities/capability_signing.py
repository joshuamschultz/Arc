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
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from arctrust import KeyPair, approve, disapprove, pin_key, unpin_key

from arcagent.capabilities.artifact_signing import (
    SIDECAR_SUFFIX,
    load_signature,
    sidecar_path,
    write_signature,
)
from arcagent.capabilities.inventory import pin_name_for_path


def sign(
    artifact: Path,
    *,
    signer_did: str,
    private_key: bytes,
    config_path: Path,
) -> None:
    """Sign ``artifact`` and record the trust it needs to load.

    Args:
        artifact: The gated artifact — a capability ``.py``, or a skill's
            ``SKILL.md``. Its current bytes are what gets signed and pinned, so
            a later edit invalidates both (drift is a hard stop, by design).
        signer_did: DID recorded as the signer, and as the TOFU approver.
        private_key: 32-byte Ed25519 seed. Used in-process only; never written.
        config_path: The agent's ``arcagent.toml`` — the sole persistence
            surface for both the trusted key and the TOFU pin.

    Raises:
        OSError: The artifact or the config could not be read/written.
        ValueError: ``private_key`` is not a valid 32-byte Ed25519 seed.

    Errors propagate rather than being swallowed: a half-applied signing is a
    capability the operator believes is trusted and is not.
    """
    content = artifact.read_bytes()
    public_key = KeyPair.from_seed(private_key).public_key
    write_signature(artifact, content, signer_did=signer_did, private_key=private_key)
    pin_key(config_path, public_key=public_key)
    approve(
        config_path,
        name=pin_name_for_path(artifact),
        source=content.decode("utf-8"),
        approver=signer_did,
        timestamp=datetime.now(UTC).isoformat(),
    )


def revoke(artifact: Path, *, config_path: Path) -> None:
    """Withdraw ``artifact``'s signature, trusted key, and TOFU pin.

    The exact inverse of :func:`sign`. The signer's key is unpinned only once no
    other artifact under the agent root is still signed by it — a shared
    operator key that another capability depends on must survive one revocation,
    or revoking a single tool would silently gate everything that operator signed.

    A missing sidecar, key, or pin is not an error: revocation is idempotent, so
    an interrupted one can always be completed by running it again.
    """
    manifest = load_signature(artifact)
    sidecar_path(artifact).unlink(missing_ok=True)
    if manifest is not None and not _key_still_in_use(config_path.parent, manifest.public_key):
        unpin_key(config_path, public_key=bytes.fromhex(manifest.public_key))
    disapprove(config_path, name=pin_name_for_path(artifact))


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
