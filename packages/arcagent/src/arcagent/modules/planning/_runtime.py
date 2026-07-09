"""Per-agent planning module runtime context (SPEC-040).

The planner's tools and hooks share one piece of state: where plans live, the
arcllm handle used to decompose/replan, the audit sink, and the arcrun run seam
bound at ``agent:ready``. Decorator-stamped functions can't carry that in a
closure, so it lives in a module-level :class:`_State` configured once at agent
startup — mirroring :mod:`arcagent.modules.policy._runtime`.

The ToolRegistry/PolicyPipeline are NOT injected here (the decorator dispatcher
does not carry them); step execution instead drives the agent's own run seam
(``run_fn``), so a step's tools pass the real policy pipeline + budget breaker
without the planner ever touching them.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.modules.base_config import ModuleConfig
from arcagent.modules.planning.models import PlanBudget
from arcagent.modules.planning.store import PlanStore
from arcagent.utils.model_helpers import get_eval_model

_logger = logging.getLogger("arcagent.modules.planning._runtime")


class PlanningConfig(ModuleConfig):
    """Planning module configuration."""

    enabled: bool = False
    # Bound replan ceiling — never run away (REQ-031).
    max_replans: int = 3
    # Aggregate plan budget, sliced onto per-step run ceilings (REQ-022).
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    # SPEC-043 concurrent Plan-Execute. Default False = interim sequential
    # walk (one ready step at a time). When True the orchestrator dispatches
    # the whole ready DAG frontier concurrently under reserve-then-settle.
    concurrent: bool = False
    # Max branches dispatched at once when ``concurrent`` is set (REQ-056).
    max_parallel: int = 8


@dataclass
class _State:
    """Mutable runtime state shared across planning tools and hooks."""

    plans_dir: Path
    workspace: Path
    agent_name: str
    agent_did: str
    config: PlanningConfig
    store: PlanStore
    telemetry: Any = None
    llm_config: Any = None
    eval_config: Any = None
    eval_label: str = "eval"
    # Lazily built arcllm handle (decompose/replan inference).
    eval_model: Any = None
    # arcrun run seam, bound at agent:ready (drives one bounded run per step).
    run_fn: Any = None
    # Tool names known to the agent — grounds decomposition (REQ-005). Empty
    # until a live registry populates it; the protected-path gate still fires.
    known_tools: set[str] = field(default_factory=set)

    @property
    def budget(self) -> PlanBudget:
        return PlanBudget(
            max_tokens=self.config.max_tokens,
            max_cost_usd=self.config.max_cost_usd,
        )

    @property
    def max_replans(self) -> int:
        return self.config.max_replans


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    llm_config: Any = None,
    eval_config: Any = None,
    agent_name: str = "",
    agent_did: str = "",
    operator_signer: Any = None,
) -> None:
    """Bind module state. Called once at agent startup.

    ``operator_signer`` (arctrust ``Signer``) signs the planner's tamper-evident
    WORM audit chain — every ``plan.created`` / ``plan.step.*`` / ``plan.replanned``
    / ``plan.completed`` transition lands as a signed, hash-chained record
    (SPEC-053/037, AU-9(2)). The lifecycle hands it in only because ``planning``
    is a WORM-sink module; absent it (a tier with no operator authority), the
    chain is simply not written and transitions still emit to ``telemetry``.
    """
    global _state
    cfg = PlanningConfig(**(config or {}))
    ws = workspace.resolve()
    plans_dir = ws / "plans"
    store = PlanStore(
        plans_dir,
        audit_sink=_build_worm_sink(ws, operator_signer, telemetry),
        operator_signer=operator_signer,
        telemetry=telemetry,
        actor_did=agent_did or (f"did:arc:{agent_name}" if agent_name else "did:arc:planner"),
    )
    _state = _State(
        plans_dir=plans_dir,
        workspace=ws,
        agent_name=agent_name,
        agent_did=agent_did,
        config=cfg,
        store=store,
        telemetry=telemetry,
        llm_config=llm_config,
        eval_config=eval_config,
        eval_label=f"{agent_name}/eval" if agent_name else "eval",
    )


def _build_worm_sink(workspace: Path, operator_signer: Any | None, telemetry: Any) -> Any:
    """Build the planner's operator-signed WORM audit sink (AU-9(2), SI-7(7)).

    Signed through the OPERATOR ``Signer`` (never the agent DID — the audited
    subject must not be its own audit authority). The chain lives in
    ``<agent_root>/.audit`` — beside the workspace, not inside it — so the
    agent's workspace-confined file tools cannot truncate or forge their own
    audit record. A pre-existing chain is integrity-checked on load.

    Fail-open (AU-5): if the sink cannot be opened, audit degrades to disabled
    rather than breaking module startup.
    """
    if operator_signer is None:
        return None
    try:
        from arctrust import WormSink

        chain = workspace.parent / ".audit" / "planning.worm"
        preexisting = chain.exists()
        sink = WormSink(chain, operator_signer)
    except Exception:  # reason: fail-open — never break startup on audit setup
        _logger.warning("planning WORM audit sink unavailable; audit disabled")
        return None
    if preexisting and not sink.verify_chain():
        _logger.error("planning WORM audit chain failed load-time verification")
        if telemetry is not None:
            telemetry.audit_event("planning.audit.chain_verify_failed", {"chain": str(chain)})
    return sink


def get_model() -> Any:
    """Return the arcllm handle for decomposition/replan, building it lazily."""
    st = state()
    if st.eval_model is not None:
        return st.eval_model
    result = get_eval_model(
        cached_model=st.eval_model,
        eval_config=st.eval_config,
        llm_config=st.llm_config,
        logger=_logger,
        agent_label=st.eval_label,
    )
    if result is not None:
        st.eval_model = result
    return result


def identity_goal_hash() -> str:
    """Hash binding a plan to the agent's immutable identity goals (ASI01).

    Reads ``<workspace>/identity.md`` — the agent's read-only goal charter — and
    hashes its content together with the agent DID. A plan stores this at
    creation; if identity.md later changes, the recomputed hash no longer
    matches and the plan is refused as goal drift (a hijacked or superseded
    plan must not keep executing against goals it was never authorized for).
    """
    st = state()
    identity_path = st.workspace / "identity.md"
    content = identity_path.read_text(encoding="utf-8") if identity_path.exists() else ""
    return hashlib.sha256(f"{st.agent_did}::{content}".encode()).hexdigest()


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "planning module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


__all__ = [
    "PlanningConfig",
    "configure",
    "get_model",
    "identity_goal_hash",
    "reset",
    "state",
]
