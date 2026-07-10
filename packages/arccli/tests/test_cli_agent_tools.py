"""Task #29 — `arc agent tools` must mirror the real runtime tool registry.

Root cause: `_discover_tools` (agent/_common.py) only scans the agent's OWN
`capabilities/` directory — it never included the ~13 builtin tools
(read/write/edit/ls/find/grep/bash/create_skill/update_skill/create_tool/
update_tool/store_secret/reload), global capabilities, workspace
capabilities, or per-ENABLED-module capability tools. `arc agent tools`
called it directly, so an agent with one scaffolded capability ("calculate")
reported exactly that one tool while `arc ext inspect` (which builds a real
CapabilityLoader-backed registry) correctly showed ~15.

`_discover_tools` itself stays correct and UNCHANGED — `arc agent build
--check` and `arc agent status` intentionally use it to report on the
agent's OWN authored capabilities, a narrower and still-valid question.
`arc agent tools` gets a new function, `_discover_runtime_tools`, that
answers the broader question: "what would this agent's tool registry
actually contain at startup?"
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from arccli.commands.agent._dispatch import agent_handler

_CALC_SRC = (
    "from arcagent.tools._decorator import tool\n\n\n"
    "@tool(description='add two numbers', classification='read_only',\n"
    "      capability_tags=['math'], when_to_use='math', version='1.0.0')\n"
    "async def calculate(a: int, b: int) -> str:\n"
    "    return str(a + b)\n"
)


def _write_agent(tmp_path: Path, *, extra_toml: str = "") -> Path:
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n' + extra_toml,
        encoding="utf-8",
    )
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "calculate.py").write_text(_CALC_SRC, encoding="utf-8")
    return tmp_path


def test_tools_lists_builtins_plus_agent_capability(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["tools", str(agent)])
    text = out.getvalue()
    # The live incident: only "calculate" showed up before the fix.
    assert "calculate" in text
    for builtin in ("write", "read", "edit", "ls", "find", "grep", "bash", "create_skill"):
        assert builtin in text, f"missing builtin {builtin!r} in:\n{text}"


def test_tools_json_includes_source_annotation(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["tools", str(agent), "--json"])
    data = json.loads(out.getvalue())
    by_name = {d["name"]: d for d in data}
    assert by_name["calculate"]["source"] == "agent"
    assert by_name["write"]["source"] == "builtins"


def test_tools_includes_enabled_module_tools(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path, extra_toml="[modules.session]\nenabled = true\n")
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["tools", str(agent), "--json"])
    data = json.loads(out.getvalue())
    names = {d["name"] for d in data}
    assert "session_search" in names


def test_tools_disabled_module_not_included(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path, extra_toml="[modules.session]\nenabled = false\n")
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["tools", str(agent), "--json"])
    data = json.loads(out.getvalue())
    names = {d["name"] for d in data}
    assert "session_search" not in names


def test_tools_no_agent_capabilities_dir_still_shows_builtins(tmp_path: Path) -> None:
    """Even with zero self-authored tools, the agent still boots with builtins."""
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n', encoding="utf-8"
    )
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["tools", str(tmp_path)])
    text = out.getvalue()
    assert "write" in text
    assert "calculate" not in text


def test_build_check_report_unaffected_by_the_fix(tmp_path: Path) -> None:
    """Regression guard: `arc agent build --check` must still report ONLY the
    agent's own scaffolded capabilities (a narrower, still-valid question),
    not the full builtins-included registry.
    """
    agent = _write_agent(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            agent_handler(["build", str(agent), "--check"])
        except SystemExit:
            pass  # missing API key etc. is expected/irrelevant to this assertion
    text = out.getvalue()
    assert "calculate" in text
    assert "tools: 1 total" in text
