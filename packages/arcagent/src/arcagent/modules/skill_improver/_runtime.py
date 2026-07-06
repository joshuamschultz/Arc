"""Per-agent skill_improver module runtime context.

The skill_improver module's hooks share state (config, trace collector,
optimization engine, candidate store, evaluator, eval model cache,
background tasks, semaphore). Decorator-stamped functions in
capabilities.py can't carry that state in a closure, so it lives in a
module-level :class:`_State` instance configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.modules.memory._runtime`. Single-agent-per-process model.

The ``skill_registry`` field is duck-typed (``Any``) to decouple from
the legacy :class:`arcagent.core.skill_registry.SkillRegistry`. The
agent rewire layer may pass either the legacy registry or a wrapper
around :class:`arcagent.capabilities.capability_registry.CapabilityRegistry`
that exposes the same ``.skills`` list and ``.discover(...)`` no-op
surface. This module never imports from ``arcagent.core.skill_registry``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.guardrails import Guardrails

_logger = logging.getLogger("arcagent.modules.skill_improver._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across skill_improver hooks."""

    config: SkillImproverConfig
    eval_config: EvalConfig
    telemetry: Any
    workspace: Path
    llm_config: Any
    # Duck-typed: legacy SkillRegistry or CapabilityRegistry wrapper.
    # Accessed via .skills (list of SkillMeta) and .discover(ws, ws).
    skill_registry: Any
    guardrails: Guardrails
    candidate_store: CandidateStore
    eval_label: str
    # SPEC-033 D3 — agent DID + key to sign mutated skills on write.
    signer_did: str | None = None
    signing_key: bytes | None = None
    # Lazily initialised in the agent:ready hook once skill_registry arrives.
    trace_collector: Any = None
    eval_model: Any = None
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    eval_config: EvalConfig | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    skill_registry: Any = None,
    agent_name: str = "",
    identity: Any = None,
) -> None:
    """Bind module state. Called once at agent startup.

    ``skill_registry`` is duck-typed — pass the legacy SkillRegistry or
    any wrapper that exposes ``.skills`` and ``.discover(ws, ws)``.
    ``None`` is acceptable; the agent:ready hook re-checks and initialises
    the TraceCollector once a registry is available.

    ``identity`` (arctrust ``AgentIdentity``) supplies the DID key used to
    sign mutated skills (SPEC-033 D3) and to sign the tamper-evident WORM
    audit chain (D4). When it can sign, the candidate store is wired to a
    ``WormSink`` in a store separate from skill code (AU-9(2)).
    """
    global _state
    cfg = SkillImproverConfig(**(config or {}))
    ec = eval_config or EvalConfig()
    ws = workspace.resolve()
    signer_did, signing_key = _resolve_signer(identity)
    worm_sink = _build_worm_sink(ws, signing_key, telemetry)
    _state = _State(
        config=cfg,
        eval_config=ec,
        telemetry=telemetry,
        workspace=ws,
        llm_config=llm_config,
        skill_registry=skill_registry,
        guardrails=Guardrails(cfg),
        candidate_store=CandidateStore(ws, audit_sink=worm_sink),
        eval_label=f"{agent_name}/skill_improver" if agent_name else "skill_improver",
        signer_did=signer_did,
        signing_key=signing_key,
        semaphore=asyncio.Semaphore(ec.max_concurrent),
    )


def _resolve_signer(identity: Any) -> tuple[str | None, bytes | None]:
    """Extract (did, signing seed) from an AgentIdentity, or (None, None)."""
    if identity is None or not getattr(identity, "can_sign", False):
        return None, None
    try:
        return identity.did, identity.signing_seed
    except Exception:  # reason: verify-only identity — no seed; skip signing
        return None, None


def _build_worm_sink(workspace: Path, signing_key: bytes | None, telemetry: Any) -> Any:
    """Build a WORM audit sink in an operator-owned store (AU-9(2), SI-7(7)).

    The chain lives in ``<agent_root>/.audit`` — beside the workspace, not
    inside it — so the agent's workspace-confined file tools cannot truncate or
    forge their own audit record. On load a pre-existing chain is integrity-
    checked; a failure emits a ``skill_improver.audit.chain_verify_failed`` alert
    (SI-7(7)) so tampering is surfaced rather than silently trusted.

    Fail-open (AU-5): if the sink cannot be opened (e.g. a lock is already
    held), audit degrades to disabled rather than breaking module startup.
    """
    if signing_key is None:
        return None
    try:
        from arctrust import WormSink

        # agent root = the workspace's parent; operator-owned, agent cannot write here.
        chain = workspace.parent / ".audit" / "skill_improver.worm"
        preexisting = chain.exists()
        sink = WormSink(chain, signing_key)
    except Exception:  # reason: fail-open — never break startup on audit setup
        _logger.warning("skill_improver WORM audit sink unavailable; audit disabled")
        return None
    if preexisting and not sink.verify_chain():
        _logger.error("skill_improver WORM audit chain failed load-time verification")
        if telemetry is not None:
            telemetry.audit_event(
                "skill_improver.audit.chain_verify_failed", {"chain": str(chain)}
            )
    return sink


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "skill_improver module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = ["configure", "reset", "state"]
