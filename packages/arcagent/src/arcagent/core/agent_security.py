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
    operator_key_file,
    operator_transit_for,
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


def operator_key_path(sec: Any) -> Path:
    """The operator key file a security config names — the ONE spelling.

    Unset config resolves the shared location, so the agent, the CLI, and the
    gateway sign with the same key; a chain signed with a second key verifies
    against neither.

    Takes the config rather than a live agent so a caller that has not built one
    — the bring-up preflight — asks this question instead of re-deriving it. The
    preflight used to check the deployment default while agents loaded a dir
    their config named, so it passed a fleet green in which every turn died on a
    missing key.
    """
    return operator_key_file(sec)


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
    """The out-of-process transit, via the one arctrust resolver (fail-closed)."""
    return operator_transit_for(sec)


def witness_medium_path(sec: Any) -> Path:
    """The witness anchor medium a security config names — the ONE spelling.

    Unset resolves the deployment location, so the medium follows a relocated
    Arc home. A spelled-out default named the pre-split path and ignored
    ``ARC_CONFIG_DIR``, which is the defect that left a live fleet's
    ``operator_key_dir`` pointing at an empty directory.
    """
    from arctrust.paths import default_witness_medium_path

    configured = sec.witness_medium_path
    return Path(configured).expanduser() if configured else default_witness_medium_path()


def build_witness(agent: Any) -> WitnessAnchor | None:
    if agent._config.security.tier != "federal":
        return None
    return AppendOnlyMediumWitness(witness_medium_path(agent._config.security))


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


def machine_operator_signer(sec: Any) -> Signer:
    """The operator signer a standalone ``python -m arcagent`` resolves before any agent exists.

    Same key location and custody the agent resolves at startup: ``in_process``
    loads (or bootstraps, as startup does) the operator key; ``vault_transit``
    signs by reference through the notary. An operator seed kept in a vault
    cannot be read before the agent's vault resolver exists, so that refuses and
    the caller fails closed.
    """
    if sec.custody == VAULT_TRANSIT:
        return build_signer(
            SignerConfig(
                custody=VAULT_TRANSIT,
                algorithm=sec.signing_algorithm,
                key_ref=_OPERATOR_KEY_REF,
            ),
            vault_transit=resolve_transit(None, sec),
        )
    if sec.operator_vault_path:
        raise SignerError("operator key is vault-resolved; no authority before agent startup")
    key = OperatorKey.load(operator_key_path(sec), generate_if_absent=True)
    return key.into_signer(sec.signing_algorithm)


__all__ = [
    "build_witness",
    "machine_operator_signer",
    "operator_key_path",
    "policy_audit_log_path",
    "prior_audit_chains_exist",
    "resolve_operator_signer",
    "resolve_record_cipher",
    "resolve_transit",
    "trace_checkpoint_chain_path",
    "verify_witness_consistency",
]
