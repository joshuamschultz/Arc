"""``arc module install --from-source`` — the dev inner loop, through to a booted agent.

SPEC-066 REQ-337. The development loop signs with an EPHEMERAL key that is minted
per invocation and written nowhere, and a module capability now requires a valid
signature at every tier — personal included. Those two facts only coexist because
the install pins the key it just verified the manifest under into the agent's own
``[security.validators]``. If that pin were ever dropped, every dev-installed
module would materialize, enable, and then register nothing, and the inner loop
would die silently.

So the assertion here is not "the command exited 0". It is: run the real command
against a real deployment, then boot a real :class:`ArcAgent` over the result and
ask its live tool registry what it holds. Only the LLM is stubbed.

The module is ``scheduler`` from the real source catalog rather than a fixture
stub, because a stub that registers one ``@tool`` would not exercise the
``@capability`` / ``@hook`` / ``@background_task`` surface a real module reaches
an agent through.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcagent
import pytest
from arcbundle import capability_dir
from arcrun import StreamEvent, TurnEndEvent
from arctrust import load_validators

from arccli.commands import module as module_cmd

_MODULE = "scheduler"
_AGENT_ID = "dev_agent"


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A self-contained deployment with one bootable agent and nothing staged.

    Nothing staged is the point: ``--from-source`` is the path an operator takes
    when there is no signed bundle on the box, which is every developer checkout.
    """
    root = tmp_path / "deployment"
    (root / "arc").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(root / "store"))
    monkeypatch.chdir(root)

    agent_dir = root / "team" / _AGENT_ID
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        "[agent]\n"
        f'name = "{_AGENT_ID}"\n'
        'org = "arc"\n'
        'type = "executor"\n'
        f'workspace = "{agent_dir / "workspace"}"\n'
        '[llm]\nmodel = "test/model"\n'
        '[security]\ntier = "personal"\n'
        f'[identity]\nkey_dir = "{agent_dir / "keys"}"\nvault_path = ""\n',
        encoding="utf-8",
    )
    yield root


def _agent_dir(deployment: Path) -> Path:
    return deployment / "team" / _AGENT_ID


async def _one_token_turn(*args: Any, **kwargs: Any) -> Any:
    """Stand in for ``arcrun.run_stream`` — the only stub in this file."""

    async def _events() -> AsyncIterator[StreamEvent]:
        yield TurnEndEvent(final_text="ok", tool_calls_made=0)

    return _events()


async def _boot_and_read_tools(agent_dir: Path) -> set[str]:
    """Start a real agent over ``agent_dir`` and return its live tool names."""
    config_path = agent_dir / "arcagent.toml"
    agent = arcagent.ArcAgent(config=arcagent.load_config(config_path), config_path=config_path)
    model = MagicMock()
    model.close = AsyncMock()
    with (
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_one_token_turn),
    ):
        await agent.startup()
        try:
            assert agent._tool_registry is not None
            return set(agent._tool_registry.tools)
        finally:
            await agent.shutdown()


def test_from_source_install_boots_an_agent_that_registers_the_modules_tools(
    deployment: Path,
) -> None:
    """The whole dev loop: one command, then the tools are really there.

    A failure here means the inner loop is broken for every developer, whatever
    the command printed.
    """
    agent_dir = _agent_dir(deployment)

    module_cmd.module_handler(["install", "--from-source", _MODULE, "--agent", _AGENT_ID])

    copy_dir = capability_dir(agent_dir, _MODULE)
    assert (copy_dir / "capabilities.py").is_file(), (
        "the per-agent capability copy was not written"
    )
    assert arcagent.sidecar_path(copy_dir / "capabilities.py").is_file(), (
        "the copy carries no signature sidecar, so it can never clear the trust gate"
    )
    assert load_validators(agent_dir / "arcagent.toml").trusted_keys, (
        "the ephemeral development key was not pinned — nothing on the box can verify the copy"
    )

    tools = asyncio.run(_boot_and_read_tools(agent_dir))

    assert {"schedule_create", "schedule_list", "schedule_update", "schedule_cancel"} <= tools, (
        f"a dev-installed module registered no tools; the agent holds {sorted(tools)}"
    )
