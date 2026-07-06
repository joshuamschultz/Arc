"""Regression: the agent-tools scanner must read the path create_tool writes to.

create_tool writes agent-authored tools to ``<agent_root>/workspace/capabilities/``.
The UI's disk scanner previously looked at ``<agent_root>/.capabilities`` (a path
nothing writes to), so newly created tools never appeared on refresh. These tests
lock the scanner onto the real write path.
"""

from __future__ import annotations

from pathlib import Path

from arcui.routes.agent_detail.tools import _collect_disk_tools

_TOOL_SRC = """\
from arcagent.builtins.capabilities import tool


@tool(name="word_count", description="Count words")
async def word_count(text: str) -> str:
    return str(len(text.split()))
"""


def test_scans_workspace_capabilities(tmp_path: Path) -> None:
    caps = tmp_path / "workspace" / "capabilities"
    caps.mkdir(parents=True)
    (caps / "word_count.py").write_text(_TOOL_SRC, encoding="utf-8")

    names = {t["name"] for t in _collect_disk_tools(tmp_path)}
    assert "word_count" in names


def test_operator_capabilities_still_scanned(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir(parents=True)
    (caps / "curated.py").write_text(_TOOL_SRC.replace("word_count", "curated"), encoding="utf-8")

    names = {t["name"] for t in _collect_disk_tools(tmp_path)}
    assert "curated" in names


def test_missing_dirs_yield_nothing(tmp_path: Path) -> None:
    assert _collect_disk_tools(tmp_path) == []
