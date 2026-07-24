"""Security: an agent's own file tools cannot reach the prompt overlay tree (COMP-008).

editable-system-prompts REQ-128 / T-754: prompt overlays live at
``<agent_root>/context/<package>/<name>.md`` — a sibling of the workspace, OUTSIDE
the ``<agent_root>/workspace`` subtree every built-in file tool is confined to by
``resolve_workspace_path``. This is defense-in-depth for COMP-007's placement: even
a traversal-crafted path from inside the workspace must be denied and audited, so
an agent can never author or tamper with the prompts that instruct it (ASI06).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.errors import ToolError
from arcagent.tools._validation import resolve_workspace_path


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (workspace, overlay_tree) in the standard agent-root layout."""
    agent_root = tmp_path / "team" / "an_agent"
    workspace = agent_root / "workspace"
    overlay_tree = agent_root / "context"
    workspace.mkdir(parents=True)
    (overlay_tree / "arcagent").mkdir(parents=True)
    return workspace, overlay_tree


class _CapturingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, details: dict[str, Any]) -> None:
        self.calls.append((event_type, details))


@pytest.mark.parametrize(
    "attack",
    [
        "../context/arcagent/spawn_guidance.md",  # relative traversal to the sibling
        "../../context/arcagent/spawn_guidance.md",  # deeper traversal
        "subdir/../../context/arcagent/spawn_guidance.md",  # traversal after a descent
    ],
)
def test_traversal_into_overlay_tree_is_denied_and_audited(tmp_path: Path, attack: str) -> None:
    workspace, _overlay = _layout(tmp_path)
    sink = _CapturingSink()
    with pytest.raises(ToolError) as exc:
        resolve_workspace_path(
            attack, workspace, tool_name="write", caller_did="did:arc:agent", audit_sink=sink
        )
    assert exc.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert sink.calls, "a confinement denial must be audited"


def test_absolute_overlay_path_is_denied(tmp_path: Path) -> None:
    workspace, overlay_tree = _layout(tmp_path)
    target = overlay_tree / "arcagent" / "spawn_guidance.md"
    sink = _CapturingSink()
    with pytest.raises(ToolError) as exc:
        resolve_workspace_path(
            str(target), workspace, tool_name="write", caller_did="did:arc:agent", audit_sink=sink
        )
    assert exc.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
    assert sink.calls


def test_overlay_tree_is_outside_the_workspace_boundary(tmp_path: Path) -> None:
    """Structural invariant behind COMP-007: context/ is a sibling of workspace/."""
    workspace, overlay_tree = _layout(tmp_path)
    assert overlay_tree.resolve().parent == workspace.resolve().parent
    assert workspace.resolve() not in overlay_tree.resolve().parents
    # A legitimate in-workspace write still resolves fine (no false-positive lockout).
    ok = resolve_workspace_path("notes.md", workspace, tool_name="write")
    assert ok.parent == workspace.resolve()
