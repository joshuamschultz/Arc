"""SPEC-082 /review backfill — ``arc mcp serve`` real wiring (no mocks).

``test_mcp_serve_command.py`` proves the handler routes to the right collaborators,
but it patches every one of them: the real agent load in ``_load_arcagent`` never
ran. The lessons file is explicit — exercise the real path, not a mock — so these
tests drive the genuine ``_load_arcagent`` against a real scaffolded agent directory
(the three sibling TOML files ``arc agent create`` writes) and assert it returns a
started-able ``ArcAgent`` plus its config and path. (The stdio wire itself is now the
``mcp`` SDK's stdio server; ``arc mcp serve --stdio`` feeds it the process's
stdin/stdout directly, so there is no longer any arccli-owned pipe wiring to test —
the handshake is covered by the door's stdio-transport test.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arccli.commands import mcp
from arccli.commands.agent._common import (
    _DEFAULT_ARCLLM_CONFIG,
    _DEFAULT_ARCRUN_CONFIG,
    render_agent_config,
)


def test_load_arcagent_returns_a_started_able_agent(tmp_path: Path) -> None:
    """The real loader turns a scaffolded agent directory into an ``ArcAgent`` triple.

    Scaffolds the three sibling TOML files ``arc agent create`` produces, then calls
    the genuine ``_load_arcagent`` (which delegates to the shared agent loader and
    constructs a real ``ArcAgent`` with a fleet). Asserts the returned agent, config,
    and path are the real objects the ``arc mcp serve`` handler feeds to
    ``build_mcp_door`` — no delegation stub.
    """
    (tmp_path / "arcagent.toml").write_text(
        render_agent_config(name="probe", did="did:arc:test:probe"), encoding="utf-8"
    )
    (tmp_path / "arcllm.toml").write_text(_DEFAULT_ARCLLM_CONFIG, encoding="utf-8")
    (tmp_path / "arcrun.toml").write_text(_DEFAULT_ARCRUN_CONFIG, encoding="utf-8")

    agent, config, config_path = mcp._load_arcagent(str(tmp_path))

    # A started-able ArcAgent: startup/shutdown are the coroutines the handler drives.
    assert asyncio.iscoroutinefunction(agent.startup)
    assert asyncio.iscoroutinefunction(agent.shutdown)
    assert config.agent.name == "probe"
    assert config_path == tmp_path / "arcagent.toml"


def test_load_arcagent_exits_when_no_config_present(tmp_path: Path) -> None:
    """An agent directory with no ``arcagent.toml`` fails closed with a non-zero exit."""
    with pytest.raises(SystemExit) as exc:
        mcp._load_arcagent(str(tmp_path))
    assert exc.value.code != 0
