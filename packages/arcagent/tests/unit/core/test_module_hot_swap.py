"""Live module enable/disable — a module's effects bind and unbind with no restart.

A module contributes tools, hooks, background tasks, and per-agent state. The
capability half rides the already-tested transactional reload; these tests pin
the two things ``set_module_enabled`` adds on top: the ``module:*`` scan-root
delta the reload acts on, and the runtime configure/teardown + task-local binding.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.core import agent_lifecycle
from arcagent.core.config import EvalConfig, LLMConfig, ModuleEntry
from arcagent.core.runtime_dependencies import RuntimeBinding, RuntimeDependencies


def _write_tool(path: Path, name: str) -> None:
    path.write_text(
        "from arcagent.tools._decorator import tool\n"
        '@tool(description="t")\n'
        f"async def {name}() -> str:\n"
        '    return "ok"\n'
    )


def _deps(workspace: Path) -> RuntimeDependencies:
    return RuntimeDependencies(
        workspace=workspace,
        config_path=workspace / "arcagent.toml",
        eval_config=EvalConfig(),
        llm_config=LLMConfig(model="test/model"),
        telemetry=None,
        bus=None,
        tool_registry=None,
        agent_name="olivia",
        agent_did="did:arc:test",
        team_root="/team",
        tier="personal",
        identity=None,
        operator_signer=object(),  # type: ignore[arg-type]
        policy_pipeline=None,
        egress_proxy=None,
        human_gate=None,
        agent_run_fn=AsyncMock(),
    )


def _fake_agent(tmp_path: Path, *, modules: dict[str, ModuleEntry]) -> Any:
    return SimpleNamespace(
        _capability_loader=MagicMock(),
        _runtime_deps=_deps(tmp_path),
        _config=SimpleNamespace(modules=modules),
        _config_path=tmp_path / "arcagent.toml",
        _runtime_bindings=[],
        reload=AsyncMock(return_value="reloaded"),
    )


def _enabled_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every enabled config entry as discovered (no on-disk module root)."""
    monkeypatch.setattr(
        agent_lifecycle,
        "active_modules",
        lambda cfg: [n for n, e in cfg.modules.items() if e.enabled],
    )


# --- The scan-root delta (CapabilityLoader.set_module_roots) -------------------


def test_set_module_roots_swaps_module_roots_and_keeps_the_rest(tmp_path: Path) -> None:
    agent_dir = tmp_path
    new_root = agent_dir / "capabilities" / "modules" / "web"
    new_root.mkdir(parents=True)

    loader = CapabilityLoader(
        scan_roots=[
            ("builtins", tmp_path / "b"),
            ("agent", tmp_path / "a"),
            ("module:voice", tmp_path / "old"),
        ],
        registry=MagicMock(),
    )
    loader.set_module_roots(agent_dir, ["web"])

    names = [name for name, _ in loader._scan_roots]
    assert "module:voice" not in names, "the dropped module's root must be gone"
    assert "module:web" in names, "the added module's root must be present"
    assert names[:2] == ["builtins", "agent"], "non-module roots are untouched, in order"
    assert names[-1] == "module:web", "module roots stay last (last-wins precedence)"


@pytest.mark.asyncio
async def test_dropping_a_scan_root_removes_its_tool_on_rescan(tmp_path: Path) -> None:
    """The removal mechanic disable relies on: a rescan drops the tools of a root
    that is no longer present. Proven against the real loader + registry."""
    from arcagent.capabilities.capability_registry import CapabilityRegistry

    keep_root, gone_root = tmp_path / "a", tmp_path / "b"
    keep_root.mkdir()
    gone_root.mkdir()
    _write_tool(keep_root / "keep.py", "keep")
    _write_tool(gone_root / "gone.py", "gone")

    registry = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("agent", keep_root), ("workspace", gone_root)], registry=registry
    )
    await loader.scan_and_register()
    assert await registry.get_tool("gone") is not None

    loader._scan_roots = [("agent", keep_root)]  # what set_module_roots does on disable
    await loader.scan_and_register()

    assert await registry.get_tool("gone") is None, "a dropped root's tool is removed"
    assert await registry.get_tool("keep") is not None, "surviving roots are untouched"


# --- The runtime half (set_module_enabled) -------------------------------------


@pytest.mark.asyncio
async def test_enable_configures_the_runtime_and_binds_and_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={})

    received: dict[str, Any] = {}

    class _Runtime:
        def configure(self, *, config: Any, workspace: Any) -> None:
            received.update(config=config, workspace=workspace)

        def state(self) -> object:
            return "STATE"

        def bind(self, state: object) -> None:
            del state

    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: _Runtime())

    result = await agent_lifecycle.set_module_enabled(agent, "web", enabled=True)

    assert received == {"config": {}, "workspace": tmp_path}
    assert agent._config.modules["web"].enabled is True
    assert [b.module_name for b in agent._runtime_bindings] == ["web"]
    agent._capability_loader.set_module_roots.assert_called_once()
    assert agent._capability_loader.set_module_roots.call_args.args[1] == ["web"]
    agent.reload.assert_awaited_once()
    assert result == "reloaded"


@pytest.mark.asyncio
async def test_disable_tears_down_the_runtime_drops_binding_and_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={"web": ModuleEntry(enabled=True)})
    agent._runtime_bindings.append(RuntimeBinding("web", lambda _s: None, object()))

    torn_down = False

    class _Runtime:
        async def teardown(self) -> None:
            nonlocal torn_down
            torn_down = True

    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: _Runtime())

    result = await agent_lifecycle.set_module_enabled(agent, "web", enabled=False)

    assert torn_down, "a RuntimeTeardownable module must be torn down on disable"
    assert agent._runtime_bindings == [], "the module's task-local binding is dropped"
    assert agent._config.modules["web"].enabled is False
    assert agent._capability_loader.set_module_roots.call_args.args[1] == []
    agent.reload.assert_awaited_once()
    assert result == "reloaded"


@pytest.mark.asyncio
async def test_disable_without_teardown_still_unwinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module with no teardown() is still fully unwound — capabilities via the
    reload, binding dropped here — without error."""
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={"web": ModuleEntry(enabled=True)})
    agent._runtime_bindings.append(RuntimeBinding("web", lambda _s: None, object()))

    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: SimpleNamespace())

    await agent_lifecycle.set_module_enabled(agent, "web", enabled=False)

    assert agent._runtime_bindings == []
    agent.reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_op_when_already_in_the_requested_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={"web": ModuleEntry(enabled=True)})

    result = await agent_lifecycle.set_module_enabled(agent, "web", enabled=True)

    assert "already" in result
    agent.reload.assert_not_awaited()
    agent._capability_loader.set_module_roots.assert_not_called()
