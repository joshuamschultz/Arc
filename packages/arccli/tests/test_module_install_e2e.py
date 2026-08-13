"""`arc module install --from-source` really gives a running agent the capability.

SPEC-066 T-971/T-972 (REQ-331, REQ-337, REQ-339). Every other test in this area
stops at the filesystem: bytes verified, tree materialized, config entry written.
None of them answers the question an operator actually asks — *does the agent
now have the tool?* That gap is the repo's known failure mode: a correct
predicate wired to nothing.

So this runs the whole path with nothing stubbed but the LLM. The real CLI
command builds a bundle from the **real** module source catalog, signs it with a
development key, verifies it, materializes it under a temporary
``ARC_CONFIG_DIR``, copies the capability surface into the agent, and enables it
in the agent's own config. Then a real :class:`ArcAgent` starts against that same
config and runs a turn, and the assertions are made against the live tool
registry and the live runtime binding.

``memory`` and ``browser`` are the two named in the migration order — ``memory``
because its ``NullBrain`` default makes absent-safety true, ``browser`` because
it is the module that motivated the whole spec.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcagent
import pytest
from arcbundle import capability_dir
from arcrun import StreamEvent, TurnEndEvent

from arccli.commands import module as module_cmd

# The real repository catalog — the same tree release CI packages from.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "arcagent" / "src" / "arcagent" / "modules"

# One tool per module that must reach the registry. A module whose capability
# file loaded but registered nothing would otherwise look like a success.
_ANCHOR_TOOL = {"memory": "memory_search", "browser": "browser_navigate"}


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated deployment holding one agent, pointed at the real catalog."""
    root = tmp_path / "deployment"
    (root / "arc").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(root / "store"))
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))
    monkeypatch.chdir(root)

    agent = root / "team" / "josh_agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        "[agent]\n"
        "name = 'josh'\n"
        "org = 'testorg'\n"
        "type = 'executor'\n"
        f"workspace = '{agent / 'workspace'}'\n\n"
        "[llm]\n"
        "model = 'test/model'\n\n"
        "[identity]\n"
        f"key_dir = '{agent / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        "[security]\n"
        "tier = 'personal'\n",
        encoding="utf-8",
    )
    yield root


def _agent_dir(deployment: Path) -> Path:
    return deployment / "team" / "josh_agent"


async def _one_token_turn(*args: Any, **kwargs: Any) -> Any:
    """Stand in for ``arcrun.run_stream`` — the only stub in this file."""

    async def _events() -> AsyncIterator[StreamEvent]:
        yield TurnEndEvent(final_text="installed", tool_calls_made=0)

    return _events()


@dataclass(frozen=True)
class _Observed:
    """What the LIVE agent had, captured before shutdown tears it back down."""

    tools: set[str]
    bound_modules: set[str]
    events: list[StreamEvent]


async def _start_agent_and_run_a_turn(agent_dir: Path) -> _Observed:
    """Start a real agent from the config the CLI just edited, and run one turn.

    The snapshot is taken while the agent is still up. ``shutdown()`` empties the
    tool registry, so reading it afterwards reports an empty set for an agent
    that was serving twenty tools a moment earlier — a shape in which every
    "the tool is absent" assertion passes for the wrong reason.
    """
    config_path = agent_dir / "arcagent.toml"
    config = arcagent.load_config(config_path)

    model = MagicMock()
    model.close = AsyncMock()
    agent = arcagent.ArcAgent(config=config, config_path=config_path)
    with (
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
    ):
        await agent.startup()
        with patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_one_token_turn):
            session = await agent.session("install-e2e")
            events = [event async for event in agent.run("say hello", session=session)]
        assert agent._tool_registry is not None
        observed = _Observed(
            tools=set(agent._tool_registry.tools),
            bound_modules={binding.module_name for binding in agent._runtime_bindings},
            events=events,
        )
        await agent.shutdown()
    return observed


@pytest.mark.parametrize("module", ["memory", "browser"])
async def test_from_source_install_gives_a_running_agent_the_capability(
    module: str, deployment: Path
) -> None:
    """Install through the real CLI, then prove the live agent serves the tool."""
    assert (_SOURCE_CATALOG / module).is_dir(), "the real catalog moved; this proves nothing"
    agent_dir = _agent_dir(deployment)

    module_cmd.module_handler(["install", "--from-source", module])

    # The three filesystem outcomes, each with a different trust property.
    assert (deployment / "arc" / "modules" / module / "_runtime.py").is_file()
    assert (capability_dir(agent_dir, module) / "capabilities.py").is_file()
    assert not (capability_dir(agent_dir, module) / "_runtime.py").exists()

    observed = await _start_agent_and_run_a_turn(agent_dir)

    # The capability reached the LIVE registry — not merely the disk.
    assert _ANCHOR_TOOL[module] in observed.tools, (
        f"{module} installed but {_ANCHOR_TOOL[module]!r} never registered; "
        f"got {sorted(observed.tools)}"
    )

    # And the runtime was configured, which is the half a capability-only copy
    # would silently skip.
    assert module in observed.bound_modules

    assert isinstance(observed.events[-1], TurnEndEvent)
    assert observed.events[-1].final_text == "installed"


@pytest.mark.parametrize("module", ["memory", "browser"])
async def test_the_agent_does_not_serve_the_tool_before_the_install(
    module: str, deployment: Path
) -> None:
    """The control case. Without it, the test above could be passing because the
    tool ships in the wheel rather than because the install put it there."""
    observed = await _start_agent_and_run_a_turn(_agent_dir(deployment))

    assert observed.tools, "no tools at all — an empty registry proves nothing below"
    assert _ANCHOR_TOOL[module] not in observed.tools
    assert module not in observed.bound_modules
