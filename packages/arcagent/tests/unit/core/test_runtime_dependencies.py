"""Typed module-runtime dependency and binding lifecycle contracts."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arctrust import AgentIdentity

from arcagent.core import agent_lifecycle
from arcagent.core.agent import ArcAgent
from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig, ModuleEntry
from arcagent.core.runtime_dependencies import RuntimeBinding, RuntimeModuleSpec


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


def _patch_one_runtime(monkeypatch: pytest.MonkeyPatch, runtime: object) -> None:
    monkeypatch.setattr(agent_lifecycle, "active_modules", lambda _config: ["memory"])
    monkeypatch.setattr(agent_lifecycle, "_warn_config_without_folder", lambda _agent: None)
    monkeypatch.setattr(agent_lifecycle.importlib, "import_module", lambda _name: runtime)


def test_renamed_dependency_aborts_instead_of_silently_skipping(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _configured_agent(tmp_path)

    def configure(*, renamed_workspace: Any) -> None:
        del renamed_workspace

    _patch_one_runtime(monkeypatch, SimpleNamespace(configure=configure))

    with pytest.raises(RuntimeError, match="Required module 'memory' configuration failed"):
        agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())


def test_required_runtime_failure_aborts_startup_configuration(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _configured_agent(tmp_path)

    def configure(**_kwargs: Any) -> None:
        raise ValueError("invalid module config")

    _patch_one_runtime(monkeypatch, SimpleNamespace(configure=configure))

    with pytest.raises(RuntimeError, match="Required module 'memory' configuration failed"):
        agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())


def test_optional_runtime_failure_is_explicitly_logged(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = _configured_agent(tmp_path)

    def configure(**_kwargs: Any) -> None:
        raise ValueError("optional unavailable")

    _patch_one_runtime(monkeypatch, SimpleNamespace(configure=configure))
    required = agent_lifecycle._RUNTIME_SPECS["memory"]
    monkeypatch.setitem(
        agent_lifecycle._RUNTIME_SPECS,
        "memory",
        RuntimeModuleSpec(required.dependencies, optional=True),
    )

    with caplog.at_level(logging.ERROR):
        agent_lifecycle.configure_module_runtimes(agent, tmp_path, egress_proxy=MagicMock())

    assert "Optional module memory runtime configuration failed" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_clears_owned_runtime_bindings(tmp_path: Any) -> None:
    agent = _configured_agent(tmp_path)
    agent._startup_impl = AsyncMock()  # type: ignore[method-assign]
    await agent.startup()
    agent._runtime_bindings.append(RuntimeBinding("test", lambda _state: None, object()))

    await agent.shutdown()

    assert agent._runtime_bindings == []
