"""SPEC-021 Component C-006 — TOFU policy layer.

Trust-On-First-Use gate for self-executing agent code (validator
scripts in skill folders, agent-authored ``.py``). Three tiers, three
profiles:

  * **personal**    — single boolean toggle (``auto_run_agent_code``)
  * **enterprise**  — match by name; new name → ``NEW_SIGHTING`` (caller
    prompts the human); known name + matching hash → ``ALLOW``;
    known name + different hash → ``DENY`` (tamper)
  * **federal**     — a valid signature is the floor (unsigned → ``DENY``),
    then the *same* human-approval gate as enterprise applies: signed +
    first-sight → ``NEW_SIGHTING``, signed + approved hash → ``ALLOW``,
    signed + drifted hash → ``DENY``. A self-signature attributes code, it
    does not authorize it — an operator still approves every new artifact.

The ``[security.validators]`` block in ``arcagent.toml`` (R-043) is the
sole persistence surface and lives at agent root, never inside the
workspace. The agent has no write access; only the human user updates
it via ``arc trust approve``.

This layer evaluates **source approval**. Tool-call policy (the
existing :class:`PolicyPipeline`) is a separate concern — that gates
*invocation*, not *load*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from arcagent.core.config import ValidatorEntry, ValidatorsConfig
from arcagent.core.tier import Tier


class Decision(StrEnum):
    """Outcome of a TOFU evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    NEW_SIGHTING = "new_sighting"


@dataclass(frozen=True)
class CapabilitySource:
    """Source bundle to evaluate.

    ``signed`` is True when the artifact's detached ``.arcsig`` signature
    re-verified at load (attributed to the agent's own DID key), or when a
    hub bundle carries a Sigstore sidecar that verified upstream. It proves
    integrity + attribution, never authorization — the tier gate above still
    decides whether attributed code may load.
    """

    name: str
    source: str
    signed: bool = False


class TofuLayer:
    """Per-tier source-approval gate.

    Construct once per agent with the loaded validator config; call
    :meth:`evaluate` per source. Stateless — the only state is the
    immutable validator config supplied at construction.
    """

    def __init__(self, tier: Tier, validators: ValidatorsConfig) -> None:
        self._tier = tier
        self._validators = validators
        # Pre-build name→hash map for O(1) lookups.
        self._approved_by_name: dict[str, str] = {
            entry.name: entry.hash for entry in validators.approved
        }

    def evaluate(self, target: CapabilitySource) -> Decision:
        if self._tier == Tier.FEDERAL:
            # Signature is the floor, but a self-signature only proves the code
            # is unmodified + attributed — never that it is *authorized*. A
            # compromised agent can sign its own new tool, so first-sight signed
            # code still routes through the enterprise human-approval gate
            # (NEW_SIGHTING → ``arc trust approve``). Federal = signed AND
            # operator-approved: strictly stronger than enterprise.
            if not target.signed:
                return Decision.DENY
            return self._evaluate_enterprise(target)
        if self._tier == Tier.ENTERPRISE:
            return self._evaluate_enterprise(target)
        # Personal — single toggle.
        if self._validators.auto_run_agent_code:
            return Decision.ALLOW
        return Decision.DENY

    def _evaluate_enterprise(self, target: CapabilitySource) -> Decision:
        approved_hash = self._approved_by_name.get(target.name)
        if approved_hash is None:
            return Decision.NEW_SIGHTING
        if _hash_source(target.source) == approved_hash:
            return Decision.ALLOW
        return Decision.DENY


def _hash_source(source: str) -> str:
    """Return ``sha256:<hex>`` digest of source bytes."""
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def approve_source(
    validators: ValidatorsConfig,
    *,
    name: str,
    source: str,
    approver: str,
    timestamp: str,
) -> ValidatorsConfig:
    """Record a TOFU approval — pin ``name`` to the current source hash (D1).

    Returns a new :class:`ValidatorsConfig` with the ``name``'s approval set to
    ``source``'s hash (replacing any prior approval for that name, so a
    re-approval after drift supersedes the stale hash). This is the pure data
    operation behind ``arc trust approve``; the CLI persists the result to
    ``arcagent.toml``. A subsequent byte change no longer matches the pinned
    hash, so :meth:`TofuLayer.evaluate` returns ``DENY`` (drift = hard stop)
    until the operator approves again.
    """
    entry = ValidatorEntry(
        name=name,
        hash=_hash_source(source),
        approver=approver,
        timestamp=timestamp,
    )
    kept = tuple(e for e in validators.approved if e.name != name)
    return validators.model_copy(update={"approved": (*kept, entry)})


__all__ = ["CapabilitySource", "Decision", "TofuLayer", "approve_source"]
