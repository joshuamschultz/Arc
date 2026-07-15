"""SPEC-021 Component C-006 — TOFU policy layer.

Trust-On-First-Use gate for self-executing agent code (validator scripts in
skill folders, agent-authored ``.py``). Three tiers, three profiles:

  * **personal**    — a source signed with the agent's own pinned identity
    key (verified by the loader before this layer ever sees it) always
    loads; otherwise gated by the ``auto_run_agent_code`` toggle
  * **enterprise**  — match by name; new name → ``NEW_SIGHTING`` (caller
    prompts the human); known name + matching hash → ``ALLOW``;
    known name + different hash → ``DENY`` (tamper)
  * **federal**     — a valid signature is the floor (unsigned → ``DENY``),
    then the *same* human-approval gate as enterprise applies: signed +
    first-sight → ``NEW_SIGHTING``, signed + approved hash → ``ALLOW``,
    signed + drifted hash → ``DENY``. A self-signature attributes code, it
    does not authorize it — an operator still approves every new artifact.

The ``[security.validators]`` block in ``arcagent.toml`` (R-043) is the sole
persistence surface (see :mod:`arctrust.validators`) and lives at agent root,
never inside the workspace. The agent has no write access; only the human
operator updates it via ``arc trust approve``.

This layer evaluates **source approval**. Tool-call policy (the
:class:`~arctrust.policy.PolicyPipeline`) is a separate concern — that gates
*invocation*, not *load*. The tier is passed as a plain string
("personal"/"enterprise"/"federal") so this trust foundation stays independent
of arcagent's ``Tier`` enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from arctrust.validators import ValidatorsConfig, hash_source

_FEDERAL = "federal"
_ENTERPRISE = "enterprise"


class TofuDecision(StrEnum):
    """Outcome of a TOFU evaluation.

    Named ``TofuDecision`` (not ``Decision``) to stay distinct from
    :class:`arctrust.policy.Decision`, the tool-call policy result — a different
    concern (invocation vs. load).
    """

    ALLOW = "allow"
    DENY = "deny"
    NEW_SIGHTING = "new_sighting"


@dataclass(frozen=True)
class CapabilitySource:
    """Source bundle to evaluate.

    ``signed`` is True when the artifact's detached ``.arcsig`` signature
    re-verified at load (attributed to the agent's own DID key), or when a
    hub bundle carries a Sigstore sidecar that verified upstream. It proves
    integrity + attribution, never authorization — the tier gate still decides
    whether attributed code may load.
    """

    name: str
    source: str
    signed: bool = False


class TofuLayer:
    """Per-tier source-approval gate.

    Construct once per agent with the loaded validator config; call
    :meth:`evaluate` per source. Stateless — the only state is the immutable
    validator config supplied at construction. ``tier`` is the deployment-tier
    string ("personal"/"enterprise"/"federal"); an ``arcagent.core.tier.Tier``
    (a ``StrEnum``) passes through unchanged.
    """

    def __init__(self, tier: str, validators: ValidatorsConfig) -> None:
        self._tier = tier
        self._validators = validators
        # Pre-build name→hash map for O(1) lookups.
        self._approved_by_name: dict[str, str] = {
            entry.name: entry.hash for entry in validators.approved
        }

    def evaluate(self, target: CapabilitySource) -> TofuDecision:
        if self._tier == _FEDERAL:
            # Signature is the floor, but a self-signature only proves the code
            # is unmodified + attributed — never that it is *authorized*. A
            # compromised agent can sign its own new tool, so first-sight signed
            # code still routes through the enterprise human-approval gate
            # (NEW_SIGHTING → ``arc trust approve``). Federal = signed AND
            # operator-approved: strictly stronger than enterprise.
            if not target.signed:
                return TofuDecision.DENY
            return self._evaluate_enterprise(target)
        if self._tier == _ENTERPRISE:
            return self._evaluate_enterprise(target)
        # Personal — signed OR the auto_run_agent_code toggle. A signature
        # here has already been re-verified by the loader against the
        # AGENT'S OWN pinned identity key (agent_lifecycle.py always passes
        # trusted_public_key = agent._identity.public_key, regardless of
        # tier) — an attacker who can write into the workspace cannot forge
        # it without the agent's private key. That makes "signed" a real
        # attribution boundary, not a bypass: unsigned code still needs the
        # explicit opt-in. This is what makes the personal-tier default
        # experience work — the scaffolded calculator.py (signed at `arc
        # agent create` time) and an agent's own self-signed skills
        # (SPEC-033 self-modification tools) load without requiring the
        # operator to flip auto_run_agent_code globally.
        if target.signed:
            return TofuDecision.ALLOW
        if self._validators.auto_run_agent_code:
            return TofuDecision.ALLOW
        return TofuDecision.DENY

    def _evaluate_enterprise(self, target: CapabilitySource) -> TofuDecision:
        approved_hash = self._approved_by_name.get(target.name)
        if approved_hash is None:
            return TofuDecision.NEW_SIGHTING
        if hash_source(target.source) == approved_hash:
            return TofuDecision.ALLOW
        return TofuDecision.DENY


__all__ = ["CapabilitySource", "TofuDecision", "TofuLayer"]
