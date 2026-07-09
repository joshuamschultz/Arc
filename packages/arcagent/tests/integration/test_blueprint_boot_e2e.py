"""SPEC-047 Phase 6 — producers-unwired E2E: a materialized blueprint boots a real brain.

DC-8b: the agent runtime entrypoint (``arcagent/__main__.py``) FLAT-reads its per-agent
``arcagent.toml`` — it does NOT call ``load_config``. So a blueprint that only lived in a
runtime merge layer would be DEAD at agent runtime. These tests prove the real path:

  resolve+apply a packaged blueprint -> materialize the concrete arcagent.toml
    -> __main__._load_config (the real FLAT read) -> real ArcAgent.startup
    -> the memory module's real select_brain picks a concrete ArcMemoryBrain (AC-3)
    -> inspect_extensions over the LIVE registry reflects that selection (AC-6)

Nothing here is a rigged fixture: the blueprint is the packaged TOML, the load is the
flat runtime read, the brain is the real select_extension result, the registry is the
agent's own populated CapabilityRegistry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.__main__ import _load_config
from arcagent.blueprints import apply_blueprint, dumps_toml, resolve_blueprint
from arcagent.core.agent import ArcAgent
from arcagent.extension.inspect import inspect_extensions
from arcagent.modules.memory import _runtime as memory_runtime


def _materialize(tmp_path: Path, blueprint_name: str, *, deployment_tier: str) -> Path:
    """Apply a packaged blueprint over a minimal per-agent base and write the flat file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    base = {
        "agent": {"name": "aria", "workspace": str(workspace)},
        "llm": {"model": "test/model"},
        "identity": {"key_dir": str(tmp_path / "keys")},
        "security": {"tier": deployment_tier},
    }
    bp = resolve_blueprint(blueprint_name, tier=deployment_tier)
    merged = apply_blueprint(bp, base, deployment_tier=deployment_tier)
    target = tmp_path / "arcagent.toml"
    target.write_text(dumps_toml(merged), encoding="utf-8")
    return target


@pytest.fixture(autouse=True)
def _reset_memory() -> object:
    memory_runtime.reset()
    yield
    memory_runtime.reset()


async def test_blueprint_flat_boot_selects_concrete_brain(tmp_path: Path) -> None:
    """AC-3 — arc-init/apply materializes a config the FLAT runtime read boots to ArcMemoryBrain."""
    _materialize(tmp_path, "personal-assistant", deployment_tier="personal")

    # The REAL runtime flat read (not load_config) — proving the written file is what boots.
    config, config_path = _load_config(tmp_path)
    assert config.modules["memory"].config["brain"] == "arcmemory"

    agent = ArcAgent(config=config, config_path=config_path)
    await agent.startup()
    try:
        # The memory module's real select_brain ran during startup and picked the concrete brain.
        assert type(memory_runtime.state().brain).__name__ == "ArcMemoryBrain"
        assert memory_runtime.state().active is True
    finally:
        await agent.shutdown()


async def test_inspect_reflects_live_registry_and_selection(tmp_path: Path) -> None:
    """AC-6 — inspect_extensions over the booted agent's LIVE config + registry matches reality."""
    _materialize(tmp_path, "personal-assistant", deployment_tier="personal")
    config, config_path = _load_config(tmp_path)
    agent = ArcAgent(config=config, config_path=config_path)
    await agent.startup()
    try:
        rows = inspect_extensions(config, agent._capability_registry)

        brain_row = next(r for r in rows if r.family == "brain")
        assert brain_row.selected == "arcmemory"
        assert brain_row.available is True
        # The inspected selection equals the agent's REAL selected brain.
        assert (brain_row.selected == "arcmemory") == (
            type(memory_runtime.state().brain).__name__ == "ArcMemoryBrain"
        )

        # The live registry's builtin tools surface as scan_many rows.
        tool_rows = [r for r in rows if r.family == "tools" and r.kind == "scan_many"]
        assert tool_rows, "expected builtin tools from the live CapabilityRegistry"
    finally:
        await agent.shutdown()
