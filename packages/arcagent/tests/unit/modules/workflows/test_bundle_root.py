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
from arctrust.paths import workflows_dir


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
        assert _runtime._bundle_root(state) == workflows_dir(tmp_path / "config")
    finally:
        _runtime.reset()


def test_the_root_matches_what_the_runner_dispatches_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same directory, resolved independently by both sides."""
    from arcagent.modules.workflows import _runtime

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
    state = _state(tmp_path)
    try:
        # The CLI signer, the fleet runner, and the tasks module all take the
        # root from the deployment resolver; this module composes its own from
        # the configured name, so the two have to land on the same directory.
        assert _runtime._bundle_root(state) == workflows_dir()
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


def test_a_node_naming_an_unregistered_agent_is_refused_while_repairable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roster check exists so "unknown agent" is an authoring error.

    Unchecked, a definition naming `@nobody` validates, signs, runs, and dies
    at its first node — long after the person who could fix it walked away.
    """
    from arcteam.workflow import parse_definition, validate_definition
    from arcteam.workflow.validator import KnownReferences

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "config"))
    definition = parse_definition(
        {
            "workflow": {"id": "wf", "owner": "@sales"},
            "node": [{"id": "a", "kind": "agent", "agent": "@nobody"}],
        }
    )

    issues = validate_definition(
        definition, known=KnownReferences(agents=frozenset({"@sales", "@ops"}))
    )

    assert [(i.node_id, i.field) for i in issues] == [("a", "agent")]
    assert "@ops" in issues[0].admissible


def test_an_empty_roster_kind_is_skipped_not_rejected() -> None:
    """Partial knowledge is normal: agents are enumerable, every tool is not."""
    from arcteam.workflow import parse_definition, validate_definition
    from arcteam.workflow.validator import KnownReferences

    definition = parse_definition(
        {
            "workflow": {"id": "wf", "owner": "@sales"},
            "node": [{"id": "a", "kind": "tool", "tool": "crm_lookup", "agent": "@sales"}],
        }
    )

    issues = validate_definition(definition, known=KnownReferences(agents=frozenset({"@sales"})))

    assert issues == ()
