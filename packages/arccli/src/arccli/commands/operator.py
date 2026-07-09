"""Operator-key custody for the CLI (SPEC-053).

The operator key is the deployment audit authority — it signs every WORM audit
chain and is deliberately distinct from any agent DID (the audited subject must
not hold the audit authority). ``arc init`` generates it once; direct ``arc
run`` audit uses it to sign its chain. All crypto is delegated to
``arctrust.OperatorKey`` — arccli holds no key logic of its own.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from arctrust import (
    FileNotaryTransit,
    OperatorKey,
    Signer,
    SignerConfig,
    SignerError,
    build_signer,
)
from arctrust.signer import VAULT_TRANSIT

# Well-known operator-key location (outside any agent workspace tool-sandbox).
DEFAULT_OPERATOR_DIR = Path("~/.arc/operator").expanduser()
_KEY_NAME = "operator.key"
_MACHINE_CONFIG = Path("~/.arc/arcagent.toml").expanduser()
_OPERATOR_KEY_REF = "operator"


def operator_key_path(arc_dir: Path) -> Path:
    """Resolve the operator-key file under an Arc config dir."""
    return Path(arc_dir).expanduser() / "operator" / _KEY_NAME


def load_operator_key(arc_dir: Path | None = None) -> OperatorKey:
    """Load the operator key, auto-bootstrapping one if absent (zero-config)."""
    base = Path(arc_dir).expanduser() if arc_dir is not None else DEFAULT_OPERATOR_DIR.parent
    return OperatorKey.load(operator_key_path(base), generate_if_absent=True)


def operator_public_key(arc_dir: Path | None = None) -> bytes | None:
    """Resolve the on-disk operator public key for signature PINNING (read-only).

    This is the key ``arc blueprint sign`` signs a preset with, so verification pins
    a user blueprint's ``.arcsig`` against it: an attacker who self-signs with a random
    keypair is refused because the manifest's key is not this one (SPEC-047 HIGH-1).

    Read-only and side-effect-free — it NEVER bootstraps a key (unlike
    :func:`load_operator_key`). Returns ``None`` when no operator key exists so the
    caller can fail closed above the personal tier (an unpinned floor is no floor). A
    present-but-tampered key raises through ``OperatorKey.load`` (covert-erasure guard).
    """
    base = Path(arc_dir).expanduser() if arc_dir is not None else DEFAULT_OPERATOR_DIR.parent
    try:
        return OperatorKey.load(operator_key_path(base), generate_if_absent=False).public_key
    except FileNotFoundError:
        return None


def ensure_operator_key(arc_dir: Path) -> OperatorKey:
    """Generate + persist the operator key under ``arc_dir`` if it does not exist.

    Idempotent: an existing key is loaded and returned unchanged (never
    regenerated — regenerating would orphan every chain it has signed). Bootstrap
    is atomic and symlink/TOCTOU-safe, and a missing key with a recorded pubkey
    fails closed rather than minting a fresh one — all enforced by
    ``OperatorKey.load`` (SPEC-053 custody hardening).
    """
    return OperatorKey.load(operator_key_path(arc_dir), generate_if_absent=True)


def _machine_security() -> Any:
    """Load the machine-wide ``[security]`` block into a validated SecurityConfig.

    Absent/unreadable config → defaults (personal / in_process). The
    ``SecurityConfig`` validator applies the tier crypto floor (federal forces
    FIPS + vault_transit + ecdsa-p256), so the CLI signs with the same posture
    as the agent (SPEC-037 F2/F3).
    """
    from arcagent.core.config import SecurityConfig

    block: dict[str, Any] = {}
    if _MACHINE_CONFIG.exists():
        try:
            with open(_MACHINE_CONFIG, "rb") as f:
                block = tomllib.load(f).get("security", {})
        except (OSError, tomllib.TOMLDecodeError):
            block = {}
    return SecurityConfig(**block)


def resolve_operator_signer(arc_dir: Path | None = None) -> Signer:
    """Resolve the operator WORM-chain signer from the machine security config.

    Threads custody + algorithm (SPEC-037 F3) so every CLI-signed audit chain
    matches the agent's policy chain instead of a bare Ed25519 default:
    ``in_process`` signs with the on-disk operator key at the configured
    algorithm; ``vault_transit`` signs by reference through the notary/HSM and
    never loads the seed. Fail-closed on an unresolvable transit (NFR-3).
    """
    sec = _machine_security()
    if sec.custody == VAULT_TRANSIT:
        transit = _resolve_transit(sec)
        return build_signer(
            SignerConfig(
                custody=VAULT_TRANSIT,
                algorithm=sec.signing_algorithm,
                key_ref=_OPERATOR_KEY_REF,
            ),
            vault_transit=transit,
        )
    return load_operator_key(arc_dir).into_signer(sec.signing_algorithm)


def _resolve_transit(sec: Any) -> FileNotaryTransit:
    """Resolve the out-of-process transit for CLI vault_transit signing."""
    keystore = (
        Path(sec.notary_keystore).expanduser()
        if sec.notary_keystore
        else Path(sec.operator_key_dir).expanduser() / "notary"
    )
    transit = FileNotaryTransit(keystore, algorithm=sec.signing_algorithm)
    try:
        transit.public_key(_OPERATOR_KEY_REF)
    except OSError as exc:
        raise SignerError(
            f"custody=vault_transit (tier={sec.tier}) but the transit at {keystore} "
            f"cannot serve the operator key — refusing to fall back to in-process "
            "signing (fail-closed, NFR-3). Provision the notary keystore or a "
            "Vault/HSM adapter, or run at tier=personal for on-disk in-process signing."
        ) from exc
    return transit


__all__ = [
    "DEFAULT_OPERATOR_DIR",
    "ensure_operator_key",
    "load_operator_key",
    "operator_key_path",
    "operator_public_key",
    "resolve_operator_signer",
]
