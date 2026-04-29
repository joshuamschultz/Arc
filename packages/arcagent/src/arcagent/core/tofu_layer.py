"""SPEC-021 Component C-006 — TOFU policy layer.

Trust-On-First-Use gate for self-executing agent code (validator
scripts in skill folders, agent-authored ``.py``). Three tiers, three
profiles:

  * **personal**    — single boolean toggle (``auto_run_agent_code``)
  * **enterprise**  — match by name; new name → ``NEW_SIGHTING`` (caller
    prompts the human); known name + matching hash → ``ALLOW``;
    known name + different hash → ``DENY`` (tamper)
  * **federal**     — agent-authored code never runs; only Sigstore-signed
    bundles pass

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

from arcagent.core.config import ValidatorsConfig
from arcagent.core.tier import Tier


class Decision(StrEnum):
    """Outcome of a TOFU evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    NEW_SIGHTING = "new_sighting"


@dataclass(frozen=True)
class CapabilitySource:
    """Source bundle to evaluate.

    ``signed`` is True only when the bundle has a Sigstore sidecar that
    verified upstream (federal-tier path). For agent-authored ``.py``
    in personal/enterprise tiers, ``signed`` is False.
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
            return Decision.ALLOW if target.signed else Decision.DENY
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


__all__ = ["CapabilitySource", "Decision", "TofuLayer"]
