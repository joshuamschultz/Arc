"""The ONE resolver for the deployment operator's key (public half and signer).

Every site that pins a verification against the operator, and every site that
signs as the operator, asks here. Under ``in_process`` custody the key is the
on-disk ``0600`` file; under ``vault_transit`` there is no private key file at
all, so the public key (and signer) come from the transit handle. A second
spelling of this lookup is how a federal vault-held key fails to verify
workflows, skills and schedules: one site reads a file that does not exist
while another asks the vault.

``security`` is duck-typed (``custody``, ``signing_algorithm``,
``operator_key_dir``, ``notary_keystore``) so arctrust stays a leaf: arcagent's
validated ``SecurityConfig`` satisfies it. ``None`` means "this deployment" and
reads the machine ``[security]`` block, applying the tier custody floor.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust.operator import OperatorKey
from arctrust.paths import (
    OPERATOR_KEY_FILENAME,
    Base,
    config_file,
    default_operator_key_path,
    operator_dir,
)
from arctrust.signer import (
    ED25519,
    IN_PROCESS,
    VAULT_TRANSIT,
    FileNotaryTransit,
    Signer,
    SignerConfig,
    SignerError,
    build_signer,
)

OPERATOR_KEY_REF = "operator"
_FEDERAL_ALGORITHM = "ecdsa-p256"


@dataclass(frozen=True)
class MachineSecurity:
    """The custody-relevant slice of the machine ``[security]`` block."""

    custody: str = IN_PROCESS
    signing_algorithm: str = ED25519
    operator_key_dir: str = ""
    notary_keystore: str = ""


def machine_security(base: Base = None) -> MachineSecurity:
    """Read custody from ``<arc_config>/arcagent.toml`` ``[security]``.

    Mirrors the tier floor ``arcagent.SecurityConfig`` applies: federal forces
    ``vault_transit`` + ``ecdsa-p256``; enterprise defaults to ``vault_transit``
    unless custody is set explicitly. Absent or unreadable config is personal.
    """
    path = config_file("arcagent.toml", base)
    block: dict[str, Any] = {}
    try:
        with open(path, "rb") as handle:
            block = tomllib.load(handle).get("security", {})
    except (OSError, tomllib.TOMLDecodeError):
        block = {}
    tier = str(block.get("tier", "personal")).lower()
    custody = str(block.get("custody", IN_PROCESS))
    algorithm = str(block.get("signing_algorithm", ED25519))
    if tier == "federal":
        custody, algorithm = VAULT_TRANSIT, _FEDERAL_ALGORITHM
    elif tier == "enterprise" and "custody" not in block:
        custody = VAULT_TRANSIT
    return MachineSecurity(
        custody=custody,
        signing_algorithm=algorithm,
        operator_key_dir=str(block.get("operator_key_dir", "")),
        notary_keystore=str(block.get("notary_keystore", "")),
    )


def operator_key_file(security: Any = None, base: Base = None) -> Path:
    """The on-disk operator key a security config names (``in_process`` custody)."""
    configured = getattr(security, "operator_key_dir", "") if security is not None else ""
    if not configured:
        return default_operator_key_path(base)
    return Path(configured).expanduser() / OPERATOR_KEY_FILENAME


def operator_transit_for(security: Any, base: Base = None) -> FileNotaryTransit:
    """The out-of-process transit, proven able to serve the operator key."""
    keystore_raw = getattr(security, "notary_keystore", "")
    if keystore_raw:
        keystore = Path(keystore_raw).expanduser()
    else:
        key_dir = getattr(security, "operator_key_dir", "")
        keystore = (Path(key_dir).expanduser() if key_dir else operator_dir(base)) / "notary"
    transit = FileNotaryTransit(keystore, algorithm=security.signing_algorithm)
    try:
        transit.public_key(OPERATOR_KEY_REF)
    except OSError as exc:
        raise SignerError(
            f"custody=vault_transit but the transit at {keystore} cannot serve the "
            f"operator key {OPERATOR_KEY_REF!r}; refusing to fall back to in-process "
            "signing (fail-closed). Provision the notary keystore or a Vault/HSM adapter."
        ) from exc
    return transit


def operator_signer_for(security: Any = None, *, base: Base = None) -> Signer:
    """The operator signing capability for this deployment's custody.

    ``vault_transit`` signs by reference (the seed never enters this process);
    ``in_process`` loads the on-disk key read-only, never bootstrapping one.
    Anything unresolvable raises; there is no silent downgrade.
    """
    sec = security if security is not None else machine_security(base)
    if sec.custody == VAULT_TRANSIT:
        return build_signer(
            SignerConfig(
                custody=VAULT_TRANSIT,
                algorithm=sec.signing_algorithm,
                key_ref=OPERATOR_KEY_REF,
            ),
            vault_transit=operator_transit_for(sec, base),
        )
    key = OperatorKey.load(operator_key_file(sec, base), generate_if_absent=False)
    return key.into_signer(sec.signing_algorithm)


def operator_public_key_for(security: Any = None, *, base: Base = None) -> bytes | None:
    """The operator public key every signature is PINNED against, read-only.

    ``None`` only when an ``in_process`` deployment has no key yet, so a caller
    above personal tier can fail closed (an unpinned floor is no floor). A
    present-but-tampered key, or an unservable transit, raises.
    """
    try:
        return operator_signer_for(security, base=base).public_key
    except FileNotFoundError:
        return None
