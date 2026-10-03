"""The workflow list never silently drops a bundle (alpha-2 regression).

A legacy bundle that no longer parses, or a signature that no longer verifies,
must come back as a row carrying the reason and the fix command, so the page
can show it in red instead of claiming "No workflows yet".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner
from arctrust import OperatorKey, sign_artifact_with_signer

from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import build_dashboard_plane

_LEGACY = """
[workflow]
id = "morning"
version = 1
owner = "@olivia"

[[node]]
id = "a"
kind = "agent"
agent = "@olivia"
join = "all"
"""

_ACTOR = OperatorActor(did="did:arc:ui:operator", session_id="s1")


@pytest.fixture
async def plane(tmp_path: Path) -> Any:
    key = OperatorKey.generate()
    key.save(tmp_path / "operator" / "operator.key")
    backend = FakeBackend()
    await backend.start()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
    )
    built = build_dashboard_plane(runner=runner)
    built.test_root = tmp_path / "workflows"
    built.test_key = key
    yield built
    await backend.stop()


def _bundle(root: Path, wid: str, text: str) -> Path:
    bundle = root / wid
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(text.replace('id = "morning"', f'id = "{wid}"'))
    return bundle


async def test_a_legacy_bundle_is_listed_as_unreadable_with_the_fix(plane: Any) -> None:
    _bundle(plane.test_root, "morning", _LEGACY)

    rows = await plane.list_workflows(actor=_ACTOR)

    assert [r["id"] for r in rows] == ["morning"]
    row = rows[0]
    assert row["status"] == "unreadable"
    assert "join" in row["health_detail"]
    assert "arc workflow migrate" in row["health_fix"]


async def test_a_stale_signature_is_listed_as_needing_resign(plane: Any) -> None:
    bundle = _bundle(plane.test_root, "onboarding", _LEGACY.replace('join = "all"\n', ""))
    stale = sign_artifact_with_signer(
        b"old-canonical-form", signer_did="operator:x", signer=plane.test_key.into_signer()
    )
    (bundle / "workflow.toml.arcsig").write_text(stale.to_json())

    rows = await plane.list_workflows(actor=_ACTOR)

    assert rows[0]["id"] == "onboarding"
    assert rows[0]["health"] == "needs_resign"
    assert "arc workflow sign onboarding" in rows[0]["health_fix"]


async def test_a_healthy_bundle_has_no_health_problem(plane: Any) -> None:
    _bundle(plane.test_root, "fine", _LEGACY.replace('join = "all"\n', ""))

    rows = await plane.list_workflows(actor=_ACTOR)

    assert rows[0]["health"] == "unsigned"
    assert rows[0]["status"] == "draft"
