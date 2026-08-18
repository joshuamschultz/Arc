"""Typed module-runtime dependency and binding lifecycle contracts.

A module declares its dependency contract by naming keyword parameters on
``configure`` that match the closed ``DependencyKey`` vocabulary. Core names no
module: it offers the full menu and each module receives exactly the entries it
names, nothing more. These tests pin that contract and the least-privilege it
guarantees.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arctrust import AgentIdentity

from arcagent.core import agent_lifecycle
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    EvalConfig,
    LLMConfig,
    ModuleEntry,
)
from arcagent.core.runtime_dependencies import RuntimeBinding, RuntimeDependencies


def _configured_agent(tmp_path: Any) -> ArcAgent:
    config = ArcAgentConfig(
        agent=AgentConfig(name="runtime", workspace=str(tmp_path)),
        llm=LLMConfig(model="test/model"),
    )
    agent = ArcAgent(config=config, config_path=tmp_path / "arcagent.toml")
    agent._identity = AgentIdentity.generate(org="test", agent_type="runtime")
    agent._telemetry = MagicMock()
    agent._bus = MagicMock()
    agent._tool_registry = MagicMock()
    agent._config.modules = {"memory": ModuleEntry(enabled=True, config={})}
    return agent


def _patch_one_runtime(
    monkeypatch: pytest.MonkeyPatch, name: str, runtime: object
) -> None:
    monkeypatch.setattr(agent_lifecycle, "active_modules", lambda _config: [name])
    monkeypatch.setattr(agent_lifecycle, "_warn_config_without_folder", lambda _agent: None)
    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _name: runtime)


# --- The by-signature dependency contract (RuntimeDependencies.select_for) -----


_SENTINELS = {
    "operator_signer": object(),
    "identity": object(),
    "policy_pipeline": object(),
    "human_gate": object(),
    "bus": object(),
    "tool_registry": object(),
    "egress_proxy": object(),
    "agent_run_fn": object(),
}


def _full_deps(workspace: Path) -> RuntimeDependencies:
    """A dependency menu with distinguishable sentinels for every field."""
    s = _SENTINELS
    return RuntimeDependencies(
        workspace=workspace,
        config_path=workspace / "arcagent.toml",
        eval_config=EvalConfig(),
        llm_config=LLMConfig(model="test/model"),
        telemetry=MagicMock(),
        bus=s["bus"],  # type: ignore[arg-type]
        tool_registry=s["tool_registry"],  # type: ignore[arg-type]
        agent_name="olivia",
        agent_did="did:arc:test",
        team_root="/team",
        tier="personal",
        identity=s["identity"],  # type: ignore[arg-type]
        operator_signer=s["operator_signer"],  # type: ignore[arg-type]
        policy_pipeline=s["policy_pipeline"],
        egress_proxy=s["egress_proxy"],  # type: ignore[arg-type]
        human_gate=s["human_gate"],
        agent_run_fn=s["agent_run_fn"],  # type: ignore[arg-type]
    )


def test_select_for_delivers_exactly_the_named_vocabulary(tmp_path: Path) -> None:
    deps = _full_deps(tmp_path)

    def configure(
        *, config: Any, telemetry: Any, workspace: Any, operator_signer: Any
    ) -> None:
        del config, telemetry, workspace, operator_signer

    kwargs = deps.select_for(configure, {"prefix": ">>"})

    assert set(kwargs) == {"config", "telemetry", "workspace", "operator_signer"}
    assert kwargs["config"] == {"prefix": ">>"}
    assert kwargs["workspace"] == tmp_path
    assert kwargs["operator_signer"] is _SENTINELS["operator_signer"]


def test_select_for_withholds_privileges_a_module_does_not_name(tmp_path: Path) -> None:
    """Least privilege: a module that never names operator_signer never gets it."""
    deps = _full_deps(tmp_path)

    def configure(*, config: Any, telemetry: Any) -> None:
        del config, telemetry

    kwargs = deps.select_for(configure, {})

    assert "operator_signer" not in kwargs
    assert "identity" not in kwargs
    assert "human_gate" not in kwargs
    assert set(kwargs) == {"config", "telemetry"}


def test_select_for_ignores_params_outside_the_vocabulary(tmp_path: Path) -> None:
    """Non-vocabulary params (a module's own injected helpers) keep their defaults."""
    deps = _full_deps(tmp_path)

    def configure(*, config: Any, messenger: Any = None, registry: Any = None) -> None:
        del config, messenger, registry

    kwargs = deps.select_for(configure, {})

    assert set(kwargs) == {"config"}


def test_select_for_reproduces_every_shipped_module_grant(tmp_path: Path) -> None:
    """The signature is the whole contract: every shipped module's configure names
    exactly the vocabulary entries it needs — no core registry required."""
    import importlib
    import inspect
    import pkgutil

    import arcagent.modules as modules_pkg
    from arcagent.core.runtime_dependencies import DependencyKey

    deps = _full_deps(tmp_path)
    vocab = {k.value for k in DependencyKey}
    checked = 0
    for info in pkgutil.iter_modules(modules_pkg.__path__):
        if not info.ispkg:
            continue
        try:
            mod = importlib.import_module(f"arcagent.modules.{info.name}._runtime")
        except ModuleNotFoundError:
            continue
        configure = getattr(mod, "configure", None)
        if configure is None:
            continue
        params = set(inspect.signature(configure).parameters)
        kwargs = deps.select_for(configure, {})
        assert set(kwargs) == params & vocab, info.name
        checked += 1
    assert checked >= 15, f"expected the shipped module catalog, scanned only {checked}"


# --- configure_module_runtimes wiring -----------------------------------------


def test_unknown_module_configures_with_no_core_registration(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module core has never heard of loads and receives what it names — proof
    the seam is open: adding a module needs no edit to core."""
    agent = _configured_agent(tmp_path)
    agent._config.modules = {"brandnew": ModuleEntry(enabled=True, config={"k": "v"})}

    received: dict[str, Any] = {}

    def configure(*, config: Any, workspace: Any, agent_name: Any) -> None:
        received.update(config=config, workspace=workspace, agent_name=agent_name)

    _patch_one_runtime(monkeypatch, "brandnew", SimpleNamespace(configure=configure))
    agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())

    assert received == {"config": {"k": "v"}, "workspace": tmp_path, "agent_name": "runtime"}


def test_renamed_dependency_aborts_instead_of_silently_skipping(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _configured_agent(tmp_path)

    def configure(*, renamed_workspace: Any) -> None:
        del renamed_workspace

    _patch_one_runtime(monkeypatch, "memory", SimpleNamespace(configure=configure))

    with pytest.raises(RuntimeError, match="Required module 'memory' configuration failed"):
        agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())


def test_required_runtime_failure_aborts_startup_configuration(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _configured_agent(tmp_path)

    def configure(**_kwargs: Any) -> None:
        raise ValueError("invalid module config")

    _patch_one_runtime(monkeypatch, "memory", SimpleNamespace(configure=configure))

    with pytest.raises(RuntimeError, match="Required module 'memory' configuration failed"):
        agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())


def test_configuration_error_names_the_module_and_chains_the_cause(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _configured_agent(tmp_path)

    def configure(**_kwargs: Any) -> None:
        raise ValueError("boom")

    _patch_one_runtime(monkeypatch, "memory", SimpleNamespace(configure=configure))

    with pytest.raises(RuntimeError) as excinfo:
        with caplog.at_level(logging.ERROR):
            agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())

    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_shutdown_clears_owned_runtime_bindings(tmp_path: Any) -> None:
    agent = _configured_agent(tmp_path)
    agent._startup_impl = AsyncMock()  # type: ignore[method-assign]
    await agent.startup()
    agent._runtime_bindings.append(RuntimeBinding("test", lambda _state: None, object()))

    await agent.shutdown()

    assert agent._runtime_bindings == []
