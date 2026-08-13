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
    RecordCipher,
    Signer,
    SignerConfig,
    SignerError,
    WormSink,
    arc_home,
    build_signer,
    derive_record_key,
)
from arctrust.signer import VAULT_TRANSIT

_KEY_NAME = "operator.key"
_OPERATOR_KEY_REF = "operator"


def _machine_config_path() -> Path:
    """The deployment's machine-wide ``arcagent.toml``.

    ``${ARC_CONFIG_DIR:-~/.arc}/arcagent.toml``, resolved through
    :func:`arctrust.arc_home` on every call — the same root arcui's trust route
    reads, so the CLI and the dashboard cannot resolve different custody on one
    box. A constant frozen at import would ignore an env var set afterwards and
    silently fall back to the invoking user's real home.
    """
    return arc_home() / "arcagent.toml"


def operator_key_path(arc_dir: Path) -> Path:
    """Resolve the operator-key file under an Arc config dir."""
    return Path(arc_dir).expanduser() / "operator" / _KEY_NAME


def load_operator_key(arc_dir: Path | None = None) -> OperatorKey:
    """Load the operator key, auto-bootstrapping one if absent (zero-config).

    ``arc_dir`` defaults to :func:`arctrust.arc_home`, so an isolated deployment
    bootstraps and signs with ITS OWN operator key rather than the invoking
    user's — the same resolution ``arctrust.default_operator_key_path`` uses.
    """
    base = Path(arc_dir).expanduser() if arc_dir is not None else arc_home()
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
    base = Path(arc_dir).expanduser() if arc_dir is not None else arc_home()
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
    import arcagent

    config = _machine_config_path()
    block: dict[str, Any] = {}
    if config.exists():
        try:
            with open(config, "rb") as f:
                block = tomllib.load(f).get("security", {})
        except (OSError, tomllib.TOMLDecodeError):
            block = {}
    return arcagent.SecurityConfig(**block)


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


def resolve_record_cipher(arc_dir: Path | None = None) -> RecordCipher | None:
    """Resolve the at-rest seal for a CLI-written WORM chain (D-577).

    Mirrors :meth:`arcagent.core.agent.ArcAgent._resolve_record_cipher` so a chain
    written by ``arc`` and one written by the agent seal identically: the key is
    derived from the operator seed the deployment already custodies. Under
    ``vault_transit`` custody that seed never enters this process, so no at-rest
    key is resolvable and the caller writes in the clear — the key-custody question
    SPEC-063 owns.
    """
    if _machine_security().custody == VAULT_TRANSIT:
        return None
    return RecordCipher(derive_record_key(load_operator_key(arc_dir).seed))


def operator_worm_sink(arc_dir: Path | None, data_dir: Path) -> WormSink:
    """Open the deployment's operator-signed WORM chain under ``data_dir``.

    The chain lives with the operational data (the same file ``arc task`` and ``arc
    workflow`` append to); the key that signs it and the at-rest seal come from the
    config dir. One opener rather than one per command: a surface composing the
    path itself would write a second chain nothing ingests.

    The caller MUST close it — the sink holds an exclusive ``flock`` for its
    lifetime, so an unclosed one locks every later writer out.
    """
    from arcstore.ingest import WORM_ACTIVE_FILENAME

    worm_dir = Path(data_dir) / "worm"
    worm_dir.mkdir(parents=True, exist_ok=True)
    return WormSink(
        worm_dir / WORM_ACTIVE_FILENAME,
        resolve_operator_signer(arc_dir),
        cipher=resolve_record_cipher(arc_dir),
    )


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
    "ensure_operator_key",
    "load_operator_key",
    "operator_key_path",
    "operator_public_key",
    "operator_worm_sink",
    "resolve_operator_signer",
]
