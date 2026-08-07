"""Connector module runtime state — SPEC-062 COMP-015 (T-901/T-902).

Two things are under test:

* ``configure()``/``state()`` round-trip the narrow set of kwargs COMP-015 declares
  as its inputs (module config, telemetry, workspace, agent identity, tier, policy
  pipeline, human gate) — and ``state()`` fails closed before ``configure()`` runs.
* State lives on a ``contextvars.ContextVar``, never a module global (task 27/32,
  ``modules/scheduler/_runtime.py:1-19``). A plain global is silently overwritten by
  whichever agent's ``asyncio.Task`` most recently called ``configure()`` — a fleet
  of agents sharing one process would otherwise leak one agent's connector state into
  another's. The governing test below configures two "agents" in sibling tasks and
  forces them to interleave with a real ``asyncio.Barrier`` rather than relying on
  completion order, so an unsafe global would fail it even when a naive sequential
  test would not (per project precedent: an instant mock makes concurrent code run
  sequentially and the bug hides).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arctrust import AgentIdentity

from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.config import ConnectorsConfig


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _identity(suffix: str) -> AgentIdentity:
    return AgentIdentity.generate(org="test", agent_type=f"connectors-{suffix}")


# --- configure()/state() round-trip -----------------------------------------


def test_state_raises_before_configure_is_called() -> None:
    with pytest.raises(RuntimeError, match="not been configured"):
        _runtime.state()


def test_configure_binds_state_visible_to_state(tmp_path: Path) -> None:
    identity = _identity("a")
    telemetry = object()
    policy_pipeline = object()
    human_gate = object()

    _runtime.configure(
        config={},
        telemetry=telemetry,
        workspace=tmp_path,
        identity=identity,
        tier="federal",
        policy_pipeline=policy_pipeline,
        human_gate=human_gate,
    )
    st = _runtime.state()

    assert isinstance(st.config, ConnectorsConfig)
    assert st.telemetry is telemetry
    assert st.workspace == tmp_path.resolve()
    assert st.identity is identity
    assert st.tier == "federal"
    assert st.policy_pipeline is policy_pipeline
    assert st.human_gate is human_gate


def test_configure_accepts_an_already_built_config_instance(tmp_path: Path) -> None:
    cfg = ConnectorsConfig()

    _runtime.configure(config=cfg, telemetry=None, workspace=tmp_path, identity=_identity("a"))
    st = _runtime.state()

    assert st.config is cfg


def test_configure_defaults_tier_to_personal(tmp_path: Path) -> None:
    _runtime.configure(config={}, telemetry=None, workspace=tmp_path, identity=_identity("a"))

    assert _runtime.state().tier == "personal"


def test_configure_defaults_policy_pipeline_and_human_gate_to_none(tmp_path: Path) -> None:
    _runtime.configure(config={}, telemetry=None, workspace=tmp_path, identity=_identity("a"))

    st = _runtime.state()
    assert st.policy_pipeline is None
    assert st.human_gate is None


def test_reset_clears_state() -> None:
    _runtime.configure(config={}, telemetry=None, workspace=Path("."), identity=_identity("a"))
    _runtime.state()  # configured, does not raise

    _runtime.reset()

    with pytest.raises(RuntimeError):
        _runtime.state()


# --- bind(): re-applying an already-built state in a fresh sibling task -----


async def test_bind_reapplies_state_in_a_fresh_sibling_task(tmp_path: Path) -> None:
    """configure() only binds the CURRENT task's ContextVar — a brand-new sibling task
    (what turn dispatch spawns per turn) starts unconfigured until bind() reapplies
    the already-built state object, without rebuilding it."""
    built: _runtime._State | None = None

    async def turn_one() -> None:
        nonlocal built
        _runtime.configure(config={}, telemetry=None, workspace=tmp_path, identity=_identity("a"))
        built = _runtime.state()

    await asyncio.create_task(turn_one())
    assert built is not None

    async def turn_two() -> _runtime._State:
        with pytest.raises(RuntimeError, match="not been configured"):
            _runtime.state()
        _runtime.bind(built)
        return _runtime.state()

    result = await asyncio.create_task(turn_two())
    assert result is built


# --- the governing test: no cross-agent leakage under forced interleaving ---


async def test_a_second_agent_configured_in_a_sibling_task_does_not_observe_the_first(
    tmp_path: Path,
) -> None:
    """Two agents ``configure()`` concurrently in sibling tasks. A barrier forces
    both to have configured before either reads its state back, so this proves
    isolation under real interleaving rather than under whatever order asyncio
    happened to schedule the tasks in."""
    barrier = asyncio.Barrier(2)
    results: dict[str, _runtime._State] = {}

    async def run_agent(name: str, tier: str) -> None:
        _runtime.configure(
            config={},
            telemetry=None,
            workspace=tmp_path / name,
            identity=_identity(name),
            tier=tier,
        )
        await barrier.wait()  # force interleaving: both configured before either reads
        results[name] = _runtime.state()

    await asyncio.gather(
        run_agent("agent_a", "personal"),
        run_agent("agent_b", "federal"),
    )

    assert results["agent_a"].tier == "personal"
    assert results["agent_b"].tier == "federal"
    assert results["agent_a"].workspace == (tmp_path / "agent_a").resolve()
    assert results["agent_b"].workspace == (tmp_path / "agent_b").resolve()
    assert results["agent_a"].identity is not results["agent_b"].identity
