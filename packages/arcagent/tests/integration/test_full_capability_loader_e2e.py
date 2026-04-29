"""SPEC-021 Phase 4 integration test — full end-to-end wiring.

Validates that :class:`CapabilityLoader` can load:

  1. All 12 built-in capabilities under ``arcagent/builtins/capabilities/``
     (7 file/exec tools + 5 self-mod tools)
  2. All 4 built-in skill folders under
     ``arcagent/builtins/capabilities/skills/``
  3. All 8 migrated modules' ``capabilities.py`` files under
     ``arcagent/modules/<name>/`` (memory, scheduler, browser, voice,
     telegram, slack, policy, ui_reporter)

This is the wiring that ``ArcAgent.startup`` will adopt in the
agent.py rewire. Each module's ``_runtime.configure(...)`` is called
with the minimum-viable args before the loader scans, so capability
classes can be instantiated without raising.

Together this exercises the full SPEC-021 R-001 / R-002 / R-004 paths
with everything we have, without touching ``agent.py`` itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.core.config import EvalConfig, TelemetryConfig
from arcagent.core.telemetry import AgentTelemetry


def _builtins_root() -> Path:
    import arcagent.builtins.capabilities as builtins_pkg

    return Path(builtins_pkg.__file__).parent


def _module_root(name: str) -> Path:
    import importlib

    pkg = importlib.import_module(f"arcagent.modules.{name}")
    return Path(pkg.__file__).parent  # type: ignore[arg-type]


def _make_telemetry() -> AgentTelemetry:
    return AgentTelemetry(
        config=TelemetryConfig(),
        agent_did="did:arc:test",
    )


def _configure_all_modules(workspace: Path, telemetry: AgentTelemetry) -> None:
    """Run ``_runtime.configure(...)`` for each migrated module.

    Each module's accepted kwargs differ; we pass the minimum viable
    set per module signature. The agent's startup will adopt this
    same shape (with real config injection) when wired up.
    """
    from arcagent.modules.browser import _runtime as browser_runtime
    from arcagent.modules.memory import _runtime as memory_runtime
    from arcagent.modules.policy import _runtime as policy_runtime
    from arcagent.modules.scheduler import _runtime as scheduler_runtime
    from arcagent.modules.slack import _runtime as slack_runtime
    from arcagent.modules.telegram import _runtime as telegram_runtime
    from arcagent.modules.ui_reporter import _runtime as ui_runtime
    from arcagent.modules.voice import _runtime as voice_runtime

    eval_config = EvalConfig()

    memory_runtime.configure(
        workspace=workspace,
        eval_config=eval_config,
        telemetry=telemetry,
        agent_name="test",
    )
    policy_runtime.configure(
        workspace=workspace,
        eval_config=eval_config,
        telemetry=telemetry,
        agent_name="test",
    )
    scheduler_runtime.configure(workspace=workspace, telemetry=telemetry)
    browser_runtime.configure(workspace=workspace, telemetry=telemetry)
    voice_runtime.configure(telemetry=telemetry)
    telegram_runtime.configure(workspace=workspace, telemetry=telemetry)
    slack_runtime.configure(workspace=workspace, telemetry=telemetry)
    ui_runtime.configure(workspace=workspace, agent_name="test")


def _reset_all_runtimes() -> None:
    """Test isolation: reset every module's runtime."""
    from arcagent.builtins.capabilities import _runtime as builtin_runtime
    from arcagent.modules.browser import _runtime as browser_runtime
    from arcagent.modules.memory import _runtime as memory_runtime
    from arcagent.modules.policy import _runtime as policy_runtime
    from arcagent.modules.scheduler import _runtime as scheduler_runtime
    from arcagent.modules.slack import _runtime as slack_runtime
    from arcagent.modules.telegram import _runtime as telegram_runtime
    from arcagent.modules.ui_reporter import _runtime as ui_runtime
    from arcagent.modules.voice import _runtime as voice_runtime

    builtin_runtime.reset()
    memory_runtime.reset()
    policy_runtime.reset()
    scheduler_runtime.reset()
    browser_runtime.reset()
    voice_runtime.reset()
    telegram_runtime.reset()
    slack_runtime.reset()
    ui_runtime.reset()


@pytest.fixture(autouse=True)
def _isolate_runtimes() -> None:
    _reset_all_runtimes()
    yield
    _reset_all_runtimes()


@pytest.mark.asyncio
async def test_full_loader_registers_builtins_and_modules(
    tmp_path: Path,
) -> None:
    """The agent's future startup wiring, exercised end-to-end."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Builtins package owns its own runtime; configure it.
    from arcagent.builtins.capabilities import _runtime as builtin_runtime

    builtin_runtime.configure(workspace=workspace)

    telemetry = _make_telemetry()
    _configure_all_modules(workspace, telemetry)

    # Build the scan-root list the agent will use post-rewire.
    scan_roots: list[tuple[str, Path]] = [
        ("builtins", _builtins_root()),
        ("builtins-skills", _builtins_root() / "skills"),
    ]
    # One scan root per migrated module so its capabilities.py is found.
    for name in [
        "memory",
        "scheduler",
        "browser",
        "voice",
        "telegram",
        "slack",
        "policy",
        "ui_reporter",
    ]:
        scan_roots.append((f"module:{name}", _module_root(name)))

    reg = CapabilityRegistry()
    loader = CapabilityLoader(scan_roots=scan_roots, registry=reg)
    diff = await loader.scan_and_register()

    # Core builtins (7 ports + 5 self-mod = 12).
    expected_tools = {
        "read",
        "write",
        "edit",
        "bash",
        "grep",
        "find",
        "ls",
        "reload",
        "create_tool",
        "create_skill",
        "update_tool",
        "update_skill",
    }
    for name in expected_tools:
        assert (await reg.get_tool(name)) is not None, name

    # Built-in skills.
    expected_skills = {
        "create-tool",
        "create-skill",
        "update-tool",
        "update-skill",
    }
    for name in expected_skills:
        assert (await reg.get_skill(name)) is not None, name

    # Migrated module hooks/tools register without raising.
    # Spot-check a representative subset.
    assert (await reg.get_tool("schedule_create")) is not None  # scheduler
    assert (await reg.get_tool("transcribe")) is not None  # voice
    assert (await reg.get_tool("slack_notify_user")) is not None
    assert (await reg.get_tool("notify_user")) is not None  # telegram

    policy_hooks = await reg.get_hooks("agent:assemble_prompt")
    assert any(h.meta.name == "inject_policy_md" for h in policy_hooks)

    memory_hooks = await reg.get_hooks("agent:pre_tool")
    assert len(memory_hooks) >= 1  # memory subscribes to pre_tool

    capability_added_hooks = await reg.get_hooks("capability:added")
    assert len(capability_added_hooks) >= 1  # ui_reporter subscribes

    # No registration failures expected on this clean wiring.
    assert not diff.errors, f"unexpected errors during full-loader scan: {diff.errors}"
