"""Per-agent skills-module runtime — wires the SkillAdapter seam (SPEC-044).

Mirrors :mod:`arcagent.modules.memory._runtime`. ``configure`` builds the injected
seams (agent-DID :class:`Signer`, operator-key WORM :class:`~arctrust.AuditSink`, the eval
LLM, and the operator-approval provider bound to the shared :class:`HumanGate`) and selects
the :class:`~arcagent.skilladapt.SkillAdapter`. With a :class:`NullSkillAdapter`, ``active``
is ``False`` and every hook short-circuits — a silent no-op that writes nothing (AC-1).

The ``skill_path`` seam and the retire/revive suppression reconcile read the module-global
state lazily so the real :class:`~arcagent.capabilities.capability_registry.CapabilityRegistry`
delivered at ``agent:ready`` is visible without rebinding the adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.capabilities import artifact_signing
from arcagent.core.config import EvalConfig
from arcagent.skilladapt import NullSkillAdapter, SkillAdapter, select_skill_adapter
from arcagent.utils.model_helpers import get_eval_model

_logger = logging.getLogger("arcagent.modules.skills._runtime")


class _SidecarSigner:
    """Agent-DID sidecar :class:`Signer` — writes ``<path>.arcsig`` (SPEC-033)."""

    def __init__(self, signer_did: str, private_key: bytes) -> None:
        self._did = signer_did
        self._key = private_key

    def sign(self, path: Path, content: bytes) -> None:
        artifact_signing.write_signature(
            path, content, signer_did=self._did, private_key=self._key
        )


@dataclass
class _State:
    adapter: SkillAdapter
    active: bool
    workspace: Path
    telemetry: Any = None
    # The real CapabilityRegistry (delivered at agent:ready). Skills live in its ``_skills``
    # dict as SkillEntry(name=, location=, ...) — NOT the old SkillRegistry ``.skills`` shape.
    skill_registry: Any = None
    # Signal-extraction state (the split-off half of the old trace_collector):
    # resolved SKILL.md path -> skill name, and the currently active skill span.
    skill_paths: dict[Path, str] = field(default_factory=dict)
    active_skill: str | None = None
    # Curator lifecycle-sweep cadence (CRITICAL-1): how often the @background_task loop
    # wakes. The 30-day inactivity *window* lives in the improver's LifecycleConfig.
    sweep_poll_seconds: float = 3_600.0
    sweep_turn: int = 0
    last_turn: int = 0  # stashed from agent:post_plan so the off-loop sweep has a turn label

    def index_skills(self, registry: Any) -> None:
        """Rebuild the SKILL.md-path -> name lookup from the CapabilityRegistry."""
        self.skill_registry = registry
        self.skill_paths = {
            entry.location.resolve(): entry.name for entry in registry._skills.values()
        }


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
    agent_did: str = "",
    identity: Any = None,
    operator_signer: Any = None,
    human_gate: Any = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    from arcagent.modules.skills.approval import build_skill_approval_provider
    from arcagent.modules.skills.config import SkillsConfig

    cfg = SkillsConfig(**(config or {}))
    ws = workspace.resolve()
    signer = _build_signer(identity)
    audit_sink = _build_worm_sink(ws, operator_signer, telemetry)
    # Operator-approval seam (D-10): a thin provider bound to the SHARED HumanGate
    # (SPEC-035/043 — operator-signed, self-approval-guarded, fail-closed at federal). No
    # gate wired → None → the improver denies (fail-closed). The improver decides *when*
    # approval is required per the tier ladder.
    approval_provider = (
        build_skill_approval_provider(human_gate, agent_did) if human_gate is not None else None
    )
    llm = get_eval_model(
        cached_model=None,
        eval_config=eval_config or EvalConfig(),
        llm_config=llm_config,
        logger=_logger,
        agent_label=f"{agent_name}/skills" if agent_name else "skills",
    )
    adapter = select_skill_adapter(
        cfg.adapter,
        workspace=ws,
        config=cfg.improver,
        tier=cfg.tier,
        llm=llm,
        signer=signer,
        approval_provider=approval_provider,
        audit_sink=audit_sink,
        agent_did=agent_did,
        skill_path=_skill_path,
        adapter_allowlist=tuple(cfg.adapter_allowlist),
    )
    _state = _State(
        adapter=adapter,
        active=not isinstance(adapter, NullSkillAdapter),
        workspace=ws,
        telemetry=telemetry,
        sweep_poll_seconds=cfg.sweep_poll_seconds,
    )
    _logger.info("skills module configured (adapter=%s, active=%s)", cfg.adapter, _state.active)


def _build_signer(identity: Any) -> _SidecarSigner | None:
    """Agent-DID sidecar signer from an AgentIdentity, or ``None`` (verify-only)."""
    if identity is None or not getattr(identity, "can_sign", False):
        return None
    try:
        return _SidecarSigner(identity.did, identity.signing_seed)
    except Exception:  # reason: verify-only identity — no seed; skip signing
        return None


def _build_worm_sink(workspace: Path, operator_signer: Any | None, telemetry: Any) -> Any:
    """Operator-signed WORM audit sink in ``<agent_root>/.audit/skills.worm`` (SPEC-053).

    Signed by the OPERATOR signer, never the agent DID — the audited subject must not
    be its own audit authority. Fail-open (AU-5): a sink that cannot be opened degrades
    to disabled rather than breaking startup.
    """
    if operator_signer is None:
        return None
    try:
        from arctrust import WormSink

        chain = workspace.parent / ".audit" / "skills.worm"
        preexisting = chain.exists()
        sink = WormSink(chain, operator_signer)
    except Exception:  # reason: fail-open — never break startup on audit setup
        _logger.warning("skills WORM audit sink unavailable; audit disabled")
        return None
    if preexisting and not sink.verify_chain() and telemetry is not None:
        telemetry.audit_event("skills.audit.chain_verify_failed", {"chain": str(chain)})
    return sink


def _skill_path(skill_name: str) -> Path | None:
    """Resolve a skill name to its SKILL.md path via the CapabilityRegistry (lazy).

    Reads the real registry shape (``_skills`` dict of ``SkillEntry`` with ``.location``),
    so the improver can locate a skill's bundle to mutate it (SPEC-044 finding 3b).
    """
    st = _state
    if st is None or st.skill_registry is None:
        return None
    entry = st.skill_registry._skills.get(skill_name)
    if entry is None:
        return None
    location: Path = entry.location
    return location


async def run_lifecycle_sweep() -> None:
    """One Curator pass: retire/revive sweep through the adapter, then reconcile the
    registry so retired skills stop being offered (CRITICAL-1 producer body + HIGH-3).

    Driven by the ``@background_task`` loop — the sole producer of ``review_lifecycle``,
    never a direct facade call.
    """
    st = _state
    if st is None or not st.active:
        return
    st.sweep_turn += 1
    await st.adapter.review_lifecycle(turn=st.last_turn or st.sweep_turn)
    await reconcile_suppression()


async def reconcile_suppression() -> None:
    """Align the registry's suppressed set with the adapter's retired skills (HIGH-3).

    Suppress newly-retired skills (hide from the offering) and unsuppress revived ones.
    Called after each sweep and once at ``agent:ready`` so retirement survives restart
    (the retired set is read from the on-disk candidate-store manifest).
    """
    st = _state
    if st is None or not st.active or st.skill_registry is None:
        return
    retired = st.adapter.retired_skills()
    registry = st.skill_registry
    for name in retired:
        await registry.suppress_skill(name)
    for name in await registry.suppressed_skills():
        if name not in retired:
            await registry.unsuppress_skill(name)


def record_turn(turn: int) -> None:
    """Stash the latest turn number (from agent:post_plan) for the off-loop sweep label."""
    if _state is not None:
        _state.last_turn = turn


def state() -> _State:
    if _state is None:
        raise RuntimeError(
            "skills module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = [
    "configure",
    "reconcile_suppression",
    "record_turn",
    "reset",
    "run_lifecycle_sweep",
    "state",
]
