"""Journey: enable a module, install it, and the capability is actually there.

This is what an operator does on a new box: put ``[modules.x] enabled = true`` in
a config and run ``arc install``. Two separate things have to happen — the module
gets materialized and signed at the deployment module root, and the running agent
picks it up and registers its tools. A deployment where the first happens and the
second does not looks completely healthy: the files are on disk, the config is
right, and the agent simply runs without the capability and says nothing.

That failure has shipped here more than once, which is why these assert on the
*started agent's* registered tools rather than on the installer's own report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .conftest import Deployment, ScriptedLLM

#: The in-repo module catalog `arc install` copies from.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "packages/arcagent/src/arcagent/modules"


@pytest.fixture(autouse=True)
def _module_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the installer at the in-repo catalog, as a wheel install would."""
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))


def install_modules(deployment: Deployment) -> list[list[str]]:
    """Run the real ``arc install`` bootstrap over the fleet."""
    from arccli.commands.up import agent_states, bootstrap_modules

    return bootstrap_modules(agent_states(deployment.team_root))


async def start_agent(deployment: Deployment) -> Any:
    """Build and start the agent from its config, the way the gateway does."""
    import arcagent

    config_path = deployment.agent_dir / "arcagent.toml"
    arc_agent = arcagent.ArcAgent(arcagent.load_config(config_path), config_path=config_path)
    await arc_agent.startup()
    return arc_agent


async def test_an_enabled_module_is_installed_and_its_tools_are_registered(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The whole install journey, judged by the agent — not by the installer.

    ``memory`` is the module a user notices first when it is missing, and it is
    the one that was silently absent fleet-wide after modules left the wheel.
    """
    enable_modules("memory")

    rows = install_modules(deployment)
    assert not [row for row in rows if "REFUSED" in row], f"install refused a module: {rows}"

    agent = await start_agent(deployment)
    try:
        tools = set(agent._tool_registry.tools)
        assert any("memory" in name for name in tools), (
            f"memory installed but registered no tool; agent has {sorted(tools)}"
        )
    finally:
        await agent.shutdown()


async def test_an_agent_starts_without_the_module_and_lacks_the_capability(
    deployment: Deployment, scripted_llm: ScriptedLLM
) -> None:
    """The paired negative: no module, no tools, and still a working agent.

    Modules are optional by design, so their absence must degrade the agent
    rather than break it. This also proves the assertion above is measuring the
    module and not something every agent has anyway.
    """
    agent = await start_agent(deployment)
    try:
        assert not [name for name in agent._tool_registry.tools if "memory" in name]
        session = await agent.session("no-module")
        replies = [event async for event in agent.run("still working?", session=session)]
        assert replies, "an agent with no modules could not complete a turn"
    finally:
        await agent.shutdown()


async def test_the_preflight_refuses_a_fleet_whose_modules_are_missing(
    deployment: Deployment, enable_modules: Any
) -> None:
    """An enabled-but-absent module must stop the bring-up, not be discovered later.

    This is the guard that keeps the silent-degradation failure impossible: a
    fleet answering chat with no scheduler, no tasks and no memory is far worse
    than one that refuses to start and names what is missing.
    """
    from arccli.commands.up import agent_states

    enable_modules("memory")

    states = agent_states(deployment.team_root)
    assert [state for state in states if state.degraded], (
        "an agent enabling an uninstalled module was not reported as degraded"
    )

    install_modules(deployment)
    assert not [state for state in agent_states(deployment.team_root) if state.degraded]
