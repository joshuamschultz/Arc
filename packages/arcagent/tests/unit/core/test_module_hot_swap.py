"""Live module enable/disable — a module's effects bind and unbind with no restart.

A module contributes tools, hooks, background tasks, and per-agent state. The
capability half rides the already-tested transactional reload; these tests pin
the two things ``set_module_enabled`` adds on top: the ``module:*`` scan-root
delta the reload acts on, and the runtime configure/teardown + task-local binding.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.core import agent_lifecycle
from arcagent.core.config import EvalConfig, LLMConfig, ModuleEntry, load_config
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
        reload_or_raise=AsyncMock(return_value="reloaded"),
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
async def test_persistent_enable_survives_restart_and_preserves_module_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={"web": ModuleEntry(enabled=False, priority=7)})
    agent._config_path.write_text(
        "[agent]\nname = 'Olivia'\n\n[modules.web]\npriority = 7\n"
        "\n[modules.web.config]\ninterval_seconds = 90\n",
        encoding="utf-8",
    )

    class _Runtime:
        def configure(self, **_kwargs: Any) -> None: ...

        def state(self) -> object:
            return "STATE"

        def bind(self, state: object) -> None:
            del state

    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: _Runtime())

    await agent_lifecycle.enable_module_persisted(agent, "web")

    persisted = tomllib.loads(agent._config_path.read_text(encoding="utf-8"))
    assert persisted["modules"]["web"] == {
        "enabled": True,
        "priority": 7,
        "config": {"interval_seconds": 90},
    }
    assert load_config(agent._config_path).modules["web"].enabled is True


@pytest.mark.asyncio
async def test_persistent_enable_rolls_back_config_and_memory_on_runtime_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={})
    original = "[agent]\nname = 'Olivia'\n"
    agent._config_path.write_text(original, encoding="utf-8")

    class _Runtime:
        def configure(self, **_kwargs: Any) -> None:
            raise RuntimeError("module setup failed")

    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: _Runtime())

    with pytest.raises(RuntimeError, match="Module 'web' configuration failed"):
        await agent_lifecycle.enable_module_persisted(agent, "web")

    assert agent._config_path.read_text(encoding="utf-8") == original
    assert "web" not in agent._config.modules
    assert agent._capability_loader.set_module_roots.call_args.args[1] == []


@pytest.mark.asyncio
async def test_persistent_enable_never_starts_runtime_when_config_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _fake_agent(tmp_path, modules={})
    agent._config_path.write_text("[agent]\nname = 'Olivia'\n", encoding="utf-8")
    runtime = MagicMock()
    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: runtime)

    def fail_persist(_path: Path, _name: str) -> str:
        raise OSError("disk unavailable")

    monkeypatch.setattr(agent_lifecycle, "persist_module_enabled", fail_persist)

    with pytest.raises(OSError, match="disk unavailable"):
        await agent_lifecycle.enable_module_persisted(agent, "web")

    assert "web" not in agent._config.modules
    runtime.configure.assert_not_called()
    agent.reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistent_enable_rolls_back_live_bindings_when_reload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    agent = _fake_agent(tmp_path, modules={})
    original = "[agent]\nname = 'Olivia'\n"
    agent._config_path.write_text(original, encoding="utf-8")
    agent.reload.side_effect = RuntimeError("capability reload failed")

    class _Runtime:
        def configure(self, **_kwargs: Any) -> None: ...

        def state(self) -> object:
            return "STATE"

        def bind(self, state: object) -> None:
            del state

    monkeypatch.setattr(agent_lifecycle, "load_module_runtime", lambda _n: _Runtime())

    with pytest.raises(RuntimeError, match="capability reload failed"):
        await agent_lifecycle.enable_module_persisted(agent, "web")

    assert agent._config_path.read_text(encoding="utf-8") == original
    assert "web" not in agent._config.modules
    assert agent._runtime_bindings == []
    assert agent._capability_loader.set_module_roots.call_args.args[1] == []


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


@pytest.mark.asyncio
async def test_persistent_enable_repairs_an_enabled_but_degraded_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enabled_by_config(monkeypatch)
    entry = ModuleEntry(enabled=True)
    agent = _fake_agent(tmp_path, modules={"connected_data": entry})
    agent._capability_registry = SimpleNamespace(
        get_capability=AsyncMock(return_value=SimpleNamespace(setup_done=False))
    )
    calls: list[bool] = []

    async def repair(_agent: Any, _name: str, *, enabled: bool) -> str:
        calls.append(enabled)
        return "repaired"

    monkeypatch.setattr(agent_lifecycle, "set_module_enabled", repair)

    assert await agent_lifecycle.enable_module_persisted(agent, "connected_data") == "repaired"
    assert calls == [False, True]
