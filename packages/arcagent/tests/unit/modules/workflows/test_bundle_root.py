"""An agent-authored workflow must land where it can be signed and run.

The failure this pins: the module resolved its bundle root under the AGENT'S
workspace. An agent asked in chat to build a workflow built one — real files,
no error, tool reported success — and it was invisible to the operator CLI, to
the dashboard, and to the fleet runner, because all three read the deployment
root. A definition nobody can sign and nothing can run is not a workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _state(tmp_path: Path, workflows_dir: str = "workflows") -> Any:
    from arctrust import AgentIdentity

    from arcagent.modules.workflows import _runtime

    _runtime.reset()
    _runtime.configure(
        config={"workflows_dir": workflows_dir},
        workspace=tmp_path / "agent" / "workspace",
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
    )
    return _runtime.state()


def test_the_root_is_the_deployment_dir_not_the_agent_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.modules.workflows import _runtime

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
    state = _state(tmp_path)
    try:
        assert _runtime._bundle_root(state) == tmp_path / "config" / "workflows"
    finally:
        _runtime.reset()


def test_the_root_matches_what_the_runner_dispatches_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same directory, resolved independently by both sides."""
    from arcteam.config import default_config_dir

    from arcagent.modules.workflows import _runtime

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
    state = _state(tmp_path)
    try:
        # ``build_workflow_runner`` uses ``<workspace_root>/workflows`` where the
        # gateway's workspace_root is the deployment config dir.
        assert _runtime._bundle_root(state) == default_config_dir() / "workflows"
    finally:
        _runtime.reset()


def test_an_absolute_root_is_used_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.modules.workflows import _runtime

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
    elsewhere = tmp_path / "elsewhere"
    state = _state(tmp_path, workflows_dir=str(elsewhere))
    try:
        assert _runtime._bundle_root(state) == elsewhere
    finally:
        _runtime.reset()
