"""HTTP boundary tests for agent-scoped capability archive review."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.capability_imports import routes

_SKILL = b"""---
name: imported
version: 1.0.0
description: imported skill
triggers: [imported]
tools: []
---

## Resources

## Contract

## Knowledge

## Steps

Use the skill.

## Anti Patterns

## Examples

## Validation
"""


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[object, dict[str, object]]] = []

    def audit_event(self, event: object, details: dict[str, object]) -> None:
        self.events.append((event, details))


def _archive(*entries: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def _client(tmp_path: Path) -> tuple[TestClient, Path, _Audit]:
    workspace = tmp_path / "ada" / "workspace"
    workspace.mkdir(parents=True)
    audit = _Audit()
    app = Starlette(routes=routes)
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = audit
    app.state.roster_provider = lambda: [
        SimpleNamespace(
            agent_id="ada",
            display_name="Ada",
            did="did:arc:agent:ada",
            workspace_path=str(workspace),
        )
    ]
    return TestClient(app), workspace, audit


def test_upload_returns_review_evidence_without_activating_capabilities(tmp_path: Path) -> None:
    client, workspace, audit = _client(tmp_path)
    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={
            "file": (
                "portable.zip",
                _archive(("skills/imported/SKILL.md", _SKILL)),
                "application/zip",
            )
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "review_ready"
    assert body["target_agent_did"] == "did:arc:agent:ada"
    assert body["skills"] == ["imported"]
    assert body["activation"] == "review_only"
    assert body["review_digest"]
    assert (workspace / "capabilities" / "imports" / ".staging" / body["import_id"]).is_dir()
    assert not (workspace / "capabilities" / "skills").exists()
    assert any(details["operation"] == "capability_import.upload" for _, details in audit.events)

    listed = client.get(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
    )
    assert listed.status_code == 200
    assert listed.json()["imports"][0]["import_id"] == body["import_id"]
    assert "source" not in listed.json()["imports"][0]


def test_upload_rejects_unsafe_archive_without_creating_staging(tmp_path: Path) -> None:
    client, workspace, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer operator"},
        files={"file": ("unsafe.zip", _archive(("../tools/evil.py", b"bad")), "application/zip")},
    )

    assert response.status_code == 422
    assert "unsafe" in response.json()["error"].lower()
    assert not (workspace / "capabilities" / "imports" / ".staging").exists()


def test_upload_requires_existing_agent(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/nope/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("portable.zip", _archive(("tools/hello.py", b"x")), "application/zip")},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("bad_name", ["portable.txt", "portable.tar"])
def test_upload_rejects_non_zip_names(tmp_path: Path, bad_name: str) -> None:
    client, _, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": (bad_name, b"not an archive", "application/octet-stream")},
    )

    assert response.status_code == 422
