"""REQ-050 — arccli forwards the agent's tier + relax into make_execute_tool.

arccli is only the seam that carries isolation config from the agent's
arcagent.toml into arcrun's router; it holds no routing logic itself.
"""

from __future__ import annotations

from pathlib import Path

from arccli.commands.agent.tools import _agent_isolation

_BASE_TOML = """\
[agent]
name = "t"
[llm]
model = "anthropic/claude-haiku-4-5-20251001"
"""


def _write_agent(tmp_path: Path, extra: str) -> Path:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "arcagent.toml").write_text(_BASE_TOML + extra)
    return agent_dir


def test_defaults_to_personal_no_relax(tmp_path: Path) -> None:
    agent_dir = _write_agent(tmp_path, "")
    assert _agent_isolation(agent_dir) == ("personal", None)


def test_forwards_federal_tier(tmp_path: Path) -> None:
    agent_dir = _write_agent(tmp_path, '[security]\ntier = "federal"\n')
    assert _agent_isolation(agent_dir) == ("federal", None)


def test_forwards_personal_relax_off(tmp_path: Path) -> None:
    agent_dir = _write_agent(
        tmp_path,
        '[security]\ntier = "personal"\n[execution]\nrelax_isolation = "off"\n',
    )
    assert _agent_isolation(agent_dir) == ("personal", "off")


def test_personal_agent_tool_is_code_exec(tmp_path: Path) -> None:
    # A personal agent's execute_python builds and carries the stable tool name.
    from arcrun import make_execute_tool

    agent_dir = _write_agent(tmp_path, "")
    tier, relax = _agent_isolation(agent_dir)
    tool = make_execute_tool(tier=tier, relax=relax)
    assert tool.name == "execute_python"
