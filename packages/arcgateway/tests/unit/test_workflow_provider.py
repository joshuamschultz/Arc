"""GatewayWorkflowProvider — workflows as slash commands (gateway side).

``specs()`` reads the deployment's real ``DefinitionStore`` off disk, so these
tests exercise the actual store rather than a mock: a bundle written to the
workflows dir must surface as a ``CommandSpec`` with its own description, an
archived bundle must not, and a missing dir must yield an empty list rather than
raise. ``run()`` with no active runner must return the plain no-runner line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcteam.workflow import DefinitionStore, parse_definition
from arctrust.paths import workflows_dir

from arcgateway.commands.workflow_provider import GatewayWorkflowProvider


def _document(workflow_id: str, description: str) -> dict[str, Any]:
    return {
        "workflow": {"id": workflow_id, "owner": "@sales", "description": description},
        "trigger": {"type": "manual"},
        "node": [{"id": "a", "kind": "agent", "agent": "@sales"}],
    }


@pytest.fixture
def workflows_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the deployment's workflow root at a temp dir and seed bundles."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    root = workflows_dir()
    store = DefinitionStore(root)
    store.save_draft(
        parse_definition(_document("briefing", "Morning briefing")),
        actor_did="did:arc:agent:sales",
        expected_version=None,
    )
    store.save_draft(
        parse_definition(_document("cleanup", "Nightly cleanup")),
        actor_did="did:arc:agent:sales",
        expected_version=None,
    )
    store.save_draft(
        parse_definition(_document("retired", "Old flow")),
        actor_did="did:arc:agent:sales",
        expected_version=None,
    )
    store.archive("retired", actor_did="did:arc:ui:operator")
    return root


def test_specs_lists_active_workflows_with_descriptions(workflows_root: Path) -> None:
    provider = GatewayWorkflowProvider()
    specs = {spec.name: spec.description for spec in provider.specs()}

    assert specs == {"briefing": "Morning briefing", "cleanup": "Nightly cleanup"}


def test_specs_excludes_archived(workflows_root: Path) -> None:
    provider = GatewayWorkflowProvider()
    names = {spec.name for spec in provider.specs()}

    assert "retired" not in names


def test_specs_empty_when_no_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No workflows dir on disk at all — resilient, not a raise.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert GatewayWorkflowProvider().specs() == []


@pytest.mark.asyncio
async def test_run_without_active_runner_reports_no_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcgateway.workflow_runner_host import RunnerHost

    monkeypatch.setattr(RunnerHost, "_active", None, raising=False)
    reply = await GatewayWorkflowProvider().run("briefing", actor_did="did:arc:user:x", args="")

    assert reply == "Workflows aren't running on this deployment yet."
