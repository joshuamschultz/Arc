"""Per-agent skills-module runtime — wires the SkillAdapter seam (SPEC-044).

Mirrors :mod:`arcagent.modules.memory._runtime`. ``configure`` builds the injected
seams (agent-DID :class:`Signer`, operator-key WORM :class:`~arctrust.AuditSink`, the
eval LLM) and selects the :class:`~arcagent.skilladapt.SkillAdapter`. When the selected
adapter is a :class:`~arcagent.skilladapt.NullSkillAdapter`, ``active`` is ``False`` and
every capability hook short-circuits — a silent no-op that writes nothing (AC-1).

The ``skill_path``/``reload`` seams read the module-global state lazily so the skill
registry delivered at ``agent:ready`` is visible without rebinding the adapter.
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
    skill_registry: Any = None
    # Signal-extraction state (the split-off half of the old trace_collector):
    # resolved skill-file path -> skill name, and the currently active skill span.
    skill_paths: dict[Path, str] = field(default_factory=dict)
    active_skill: str | None = None

    def index_skills(self, registry: Any) -> None:
        """Rebuild the path -> name lookup from the registry's ``.skills`` list."""
        self.skill_registry = registry
        self.skill_paths = {s.file_path.resolve(): s.name for s in registry.skills}


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    agent_name: str = "",
    identity: Any = None,
    operator_signer: Any = None,
) -> None:
    """Bind module state. Called once at agent startup."""
    global _state
    from arcagent.modules.skills.config import SkillsConfig

    cfg = SkillsConfig(**(config or {}))
    ws = workspace.resolve()
    signer = _build_signer(identity)
    audit_sink = _build_worm_sink(ws, operator_signer, telemetry)
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
        audit_sink=audit_sink,
        skill_path=_skill_path,
        reload=_reload,
        adapter_allowlist=tuple(cfg.adapter_allowlist),
    )
    _state = _State(
        adapter=adapter,
        active=not isinstance(adapter, NullSkillAdapter),
        workspace=ws,
        telemetry=telemetry,
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
    """Resolve a skill name to its file path via the registry (lazy — reads state)."""
    st = _state
    if st is None or st.skill_registry is None:
        return None
    for skill in st.skill_registry.skills:
        if skill.name == skill_name:
            path: Path | None = skill.file_path
            return path
    return None


def _reload() -> None:
    """Re-discover skills after a mutation (lazy — reads state)."""
    st = _state
    if st is None or st.skill_registry is None:
        return
    st.skill_registry.discover(st.workspace, st.workspace)


def retired_skill_names() -> frozenset[str]:
    """Retired skill names from the active adapter, or empty when skills is off/unset.

    Read by the capability-offering path to hide retired skills from the loop (HIGH-3,
    REQ-043). Safe to call before configuration — returns empty rather than raising.
    """
    st = _state
    if st is None or not st.active:
        return frozenset()
    return st.adapter.retired_skills()


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


__all__ = ["configure", "reset", "retired_skill_names", "state"]
