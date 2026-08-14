"""Security custody, audit-path, and witness setup for ``ArcAgent``."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, cast

from arctrust import (
    AppendOnlyMediumWitness,
    FileNotaryTransit,
    OperatorKey,
    RecordCipher,
    Signer,
    SignerConfig,
    SignerError,
    WitnessAnchor,
    assert_fips_if_required,
    build_signer,
    derive_record_key,
    read_verified_anchor,
    verify_local_head_witnessed,
)
from arctrust.signer import VAULT_TRANSIT

_logger = logging.getLogger("arcagent.agent")
_OPERATOR_KEY_REF = "operator"


def policy_audit_log_path(agent: Any) -> Path:
    configured = agent._config.security.policy_audit_log
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else agent._workspace / path
    try:
        from arcstore.config import resolve_data_dir
    except ImportError:
        return cast(Path, agent._workspace / "audit" / "policy-chain.jsonl")
    raw = agent._config.agent.name or agent._workspace.name or "agent"
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", raw) or "agent"
    return resolve_data_dir() / "worm" / f"audit-chain-{slug}.jsonl"


def operator_key_path(agent: Any) -> Path:
    """The deployment operator key this agent signs its policy chain with.

    Unset config resolves the ONE shared location, so the agent, the CLI, and the
    gateway sign with the same key; a chain signed with a second key verifies
    against neither.
    """
    from arctrust.paths import OPERATOR_KEY_FILENAME, default_operator_key_path

    configured = agent._config.security.operator_key_dir
    if not configured:
        return default_operator_key_path()
    return Path(configured).expanduser() / OPERATOR_KEY_FILENAME


def resolve_operator_signer(agent: Any, sec: Any) -> Signer:
    assert_fips_if_required(require_fips=sec.require_fips, algorithm=sec.signing_algorithm)
    if sec.custody == VAULT_TRANSIT:
        agent._operator_key = None
        transit = agent._resolve_transit(sec)
        return build_signer(
            SignerConfig(
                custody=VAULT_TRANSIT,
                algorithm=sec.signing_algorithm,
                key_ref=_OPERATOR_KEY_REF,
            ),
            vault_transit=transit,
        )
    agent._operator_key = OperatorKey.load(
        agent._operator_key_path(),
        vault_resolver=agent._vault_resolver,
        vault_path=sec.operator_vault_path,
        generate_if_absent=True,
        prior_chain_exists=agent._prior_audit_chains_exist(),
    )
    return cast(Signer, agent._operator_key.into_signer(sec.signing_algorithm))


def resolve_record_cipher(agent: Any) -> RecordCipher | None:
    if agent._operator_key is not None:
        return RecordCipher(derive_record_key(agent._operator_key.seed))
    _logger.warning(
        "audit records are NOT sealed at rest: custody=vault_transit keeps the "
        "operator seed out of this process and no at-rest key is configured "
        "(SPEC-063 owns audit-store key custody)"
    )
    return None


def resolve_transit(_agent: Any, sec: Any) -> FileNotaryTransit:
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
            f"custody=vault_transit but the transit at {keystore} cannot serve "
            f"the operator key {_OPERATOR_KEY_REF!r} — refusing to fall back to "
            "in-process signing (fail-closed, NFR-3). Provision the notary "
            "keystore (or configure a production Vault Transit/HSM adapter)."
        ) from exc
    return transit


def build_witness(agent: Any) -> WitnessAnchor | None:
    if agent._config.security.tier != "federal":
        return None
    medium = Path(agent._config.security.witness_medium_path).expanduser()
    return AppendOnlyMediumWitness(medium)


def trace_checkpoint_chain_path(agent: Any) -> Path:
    return cast(Path, agent._workspace.parent / ".audit" / "trace-checkpoint.worm")


def prior_audit_chains_exist(agent: Any) -> bool:
    audit_dir = agent._workspace.parent / ".audit"
    candidates = (
        agent._policy_audit_log_path(),
        agent._trace_checkpoint_chain_path(),
        audit_dir / "skills.worm",
    )
    return any(path.exists() for path in candidates)


def verify_witness_consistency(agent: Any) -> None:
    if agent._witness is None or agent._operator_signer is None:
        return
    local = read_verified_anchor(
        agent._trace_checkpoint_chain_path(),
        agent._operator_signer.public_key,
        cipher=agent._record_cipher,
    )
    verify_local_head_witnessed(
        local,
        agent._witness,
        federal=agent._config.security.tier == "federal",
    )


__all__ = [
    "build_witness",
    "operator_key_path",
    "policy_audit_log_path",
    "prior_audit_chains_exist",
    "resolve_operator_signer",
    "resolve_record_cipher",
    "resolve_transit",
    "trace_checkpoint_chain_path",
    "verify_witness_consistency",
]
