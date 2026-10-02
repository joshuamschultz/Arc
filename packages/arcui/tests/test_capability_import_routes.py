"""HTTP boundary tests for agent-scoped capability archive review."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from arctrust import OperatorKey, default_operator_key_path, load_validators
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


def _folder_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in ("portable/", "portable/skills/", "portable/skills/imported/"):
            archive.writestr(directory, b"")
        archive.writestr("portable/skills/imported/SKILL.md", _SKILL)
    return buffer.getvalue()


def _client(
    tmp_path: Path, config: str = '[security]\ntier = "federal"\n'
) -> tuple[TestClient, Path, _Audit]:
    workspace = tmp_path / "ada" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "arcagent.toml").write_text(
        f'[agent]\nname = "ada"\n\n{config}', encoding="utf-8"
    )
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


_BARE_SKILL = b"---\nname: bare\ndescription: only the required fields\n---\n\nDo the thing.\n"


def _upload_bare_skill(client: TestClient) -> object:
    return client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("SKILL.md", _BARE_SKILL, "text/markdown")},
    )


def test_import_route_strict_at_federal_when_configured(tmp_path: Path) -> None:
    """Federal + strict_skill_sections refuses a skill missing the Arc sections AT REVIEW."""
    client, _, _ = _client(
        tmp_path,
        '[security]\ntier = "federal"\n[capabilities]\nstrict_skill_sections = true\n',
    )
    response = _upload_bare_skill(client)
    assert response.status_code == 422, response.text  # type: ignore[attr-defined]  # reason: TestClient response
    listed = client.get(
        "/api/agents/ada/capability-imports", headers={"Authorization": "Bearer viewer"}
    )
    assert listed.json()["imports"] == []  # nothing is reviewable, so nothing can be promoted


def test_import_lenient_below_federal(tmp_path: Path) -> None:
    """Below federal a missing section is a review finding, even with strict configured."""
    client, _, _ = _client(
        tmp_path,
        '[security]\ntier = "enterprise"\n[capabilities]\nstrict_skill_sections = true\n',
    )
    response = _upload_bare_skill(client)
    assert response.status_code == 201, response.text  # type: ignore[attr-defined]  # reason: TestClient response
    assert response.json()["findings"]  # type: ignore[attr-defined]  # reason: TestClient response


def test_plain_skill_markdown_uses_the_review_pipeline(tmp_path: Path) -> None:
    client, workspace, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("SKILL.md", _SKILL, "text/markdown")},
    )
    assert response.status_code == 201, response.text
    review = response.json()
    assert review["status"] == "review_ready"
    assert review["skills"] == ["imported"]
    assert review["files"][0]["path"] == "skills/imported/SKILL.md"
    assert not (workspace / "capabilities" / "skills" / "imported").exists()


def test_plain_skill_markdown_accepts_uppercase_extension(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("SKILL.MD", _SKILL, "text/markdown")},
    )
    assert response.status_code == 201
    assert response.json()["skills"] == ["imported"]


def test_browser_upload_accepts_a_normal_zipped_folder(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("portable.zip", _folder_archive(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json()["skills"] == ["imported"]


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


def test_promote_and_revoke_are_operator_signed_and_viewer_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    operator = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    client, workspace, audit = _client(tmp_path)
    uploaded = client.post(
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
    import_id = uploaded.json()["import_id"]
    promote_path = f"/api/agents/ada/capability-imports/{import_id}/promote"
    revoke_path = f"/api/agents/ada/capability-imports/{import_id}/revoke"

    denied = client.post(promote_path, headers={"Authorization": "Bearer viewer"})
    assert denied.status_code == 403

    promoted = client.post(promote_path, headers={"Authorization": "Bearer operator"})
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"
    artifact = workspace / "capabilities" / "skills" / "imported" / "SKILL.md"
    assert artifact.is_file()
    assert artifact.with_name("SKILL.md.arcsig").is_file()
    validators = load_validators(workspace / "arcagent.toml")
    assert operator.public_key.hex() in validators.trusted_keys
    assert {entry.name for entry in validators.approved} == {"imported"}

    revoked = client.post(revoke_path, headers={"Authorization": "Bearer operator"})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert not artifact.exists()
    assert not artifact.with_name("SKILL.md.arcsig").exists()
    validators = load_validators(workspace / "arcagent.toml")
    assert validators.approved == ()
    assert operator.public_key.hex() not in validators.trusted_keys
    operations = [details["operation"] for _, details in audit.events]
    assert "capability_import.promote" in operations
    assert "capability_import.revoke" in operations


def test_revoke_rejects_stale_review_without_removing_promoted_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    client, workspace, _ = _client(tmp_path)
    uploaded = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer operator"},
        files={
            "file": (
                "portable.zip",
                _archive(("skills/imported/SKILL.md", _SKILL)),
                "application/zip",
            )
        },
    )
    import_id = uploaded.json()["import_id"]
    promote_path = f"/api/agents/ada/capability-imports/{import_id}/promote"
    revoke_path = f"/api/agents/ada/capability-imports/{import_id}/revoke"
    assert (
        client.post(promote_path, headers={"Authorization": "Bearer operator"}).status_code == 200
    )
    staged = (
        workspace
        / "capabilities"
        / "imports"
        / ".staging"
        / import_id
        / "skills"
        / "imported"
        / "SKILL.md"
    )
    staged.write_bytes(staged.read_bytes().replace(b"Use the skill.", b"Changed after review."))

    stale = client.post(revoke_path, headers={"Authorization": "Bearer operator"})
    assert stale.status_code == 409
    artifact = workspace / "capabilities" / "skills" / "imported" / "SKILL.md"
    assert artifact.is_file()
    listed = client.get(
        "/api/agents/ada/capability-imports", headers={"Authorization": "Bearer viewer"}
    )
    assert listed.json()["imports"][0]["status"] == "modified"


def test_list_skips_malformed_or_oversized_review_metadata(tmp_path: Path) -> None:
    client, workspace, _ = _client(tmp_path)
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
    import_id = response.json()["import_id"]
    manifest_path = workspace / "capabilities" / "imports" / ".staging" / import_id / "import.json"

    malformed = json.loads(manifest_path.read_text(encoding="utf-8"))
    malformed["supplier_metadata"] = "do-not-return-this"
    manifest_path.write_text(json.dumps(malformed), encoding="utf-8")
    listed = client.get(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
    )
    assert listed.status_code == 200
    assert listed.json() == {"imports": []}
    assert "do-not-return-this" not in listed.text

    malformed["supplier_metadata"] = {}
    malformed["unexpected"] = "do-not-return-this"
    manifest_path.write_text(json.dumps(malformed), encoding="utf-8")
    listed = client.get(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
    )
    assert listed.status_code == 200
    assert listed.json() == {"imports": []}
    assert "do-not-return-this" not in listed.text

    manifest_path.write_bytes(b"{" + (b"x" * (1024 * 1024 + 1)))
    listed = client.get(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
    )
    assert listed.status_code == 200
    assert listed.json() == {"imports": []}


def test_upload_requires_existing_agent(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/nope/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": ("portable.zip", _archive(("tools/hello.py", b"x")), "application/zip")},
    )

    assert response.status_code == 404


def test_reviewed_file_read_and_operator_edit_regenerate_evidence(tmp_path: Path) -> None:
    client, workspace, audit = _client(tmp_path)
    uploaded = client.post(
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
    import_id = uploaded.json()["import_id"]
    read = client.get(
        f"/api/agents/ada/capability-imports/{import_id}/files/skills/imported/SKILL.md",
        headers={"Authorization": "Bearer viewer"},
    )
    assert read.status_code == 200
    assert read.json()["content"] == _SKILL.decode()

    denied = client.put(
        f"/api/agents/ada/capability-imports/{import_id}/files/skills/imported/SKILL.md",
        headers={"Authorization": "Bearer viewer"},
        json={"content": _SKILL.decode().replace("Use the skill.", "Changed.")},
    )
    assert denied.status_code == 403

    edited = client.put(
        f"/api/agents/ada/capability-imports/{import_id}/files/skills/imported/SKILL.md",
        headers={"Authorization": "Bearer operator"},
        json={
            "path": "skills/imported/SKILL.md",
            "content": _SKILL.decode().replace("Use the skill.", "Changed."),
        },
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "review_ready"
    assert edited.json()["review_digest"] != uploaded.json()["review_digest"]
    assert "capability_import.edit" in {details["operation"] for _, details in audit.events}
    assert "Changed." in (
        workspace / "capabilities/imports/.staging" / import_id / "skills/imported/SKILL.md"
    ).read_text(encoding="utf-8")


def test_reviewed_file_route_rejects_staging_metadata_and_traversal(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    uploaded = client.post(
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
    import_id = uploaded.json()["import_id"]
    for path in ("import.json", "../import.json"):
        response = client.get(
            f"/api/agents/ada/capability-imports/{import_id}/files/{path}",
            headers={"Authorization": "Bearer viewer"},
        )
        assert response.status_code in {404, 422}


@pytest.mark.parametrize("bad_name", ["portable.txt", "portable.tar"])
def test_upload_rejects_non_zip_names(tmp_path: Path, bad_name: str) -> None:
    client, _, _ = _client(tmp_path)
    response = client.post(
        "/api/agents/ada/capability-imports",
        headers={"Authorization": "Bearer viewer"},
        files={"file": (bad_name, b"not an archive", "application/octet-stream")},
    )

    assert response.status_code == 422


def _skill_version(version: str) -> bytes:
    return _SKILL.replace(b"version: 1.0.0", f"version: {version}".encode()).replace(
        b"Use the skill.", f"Use the skill, v{version}.".encode()
    )


def test_importing_a_new_version_becomes_a_new_anchored_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arcagent
    from arctrust import FileJournalAnchor

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    operator = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    signer = operator.into_signer()
    client, workspace, _audit = _client(tmp_path)
    anchors = tmp_path / "anchors"

    def factory(did: str, name: str) -> FileJournalAnchor:
        return FileJournalAnchor(
            anchors, scope=arcagent.skill_revision_scope(did, name), signer=signer
        )

    client.app.state.skill_revision_anchor_factory = factory

    def _upload_and_promote(version: str) -> dict[str, object]:
        uploaded = client.post(
            "/api/agents/ada/capability-imports",
            headers={"Authorization": "Bearer viewer"},
            files={
                "file": (
                    "portable.zip",
                    _archive(("skills/imported/SKILL.md", _skill_version(version))),
                    "application/zip",
                )
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        promoted = client.post(
            f"/api/agents/ada/capability-imports/{uploaded.json()['import_id']}/promote",
            headers={"Authorization": "Bearer operator"},
        )
        assert promoted.status_code == 200, promoted.text
        body: dict[str, object] = promoted.json()
        return body

    _upload_and_promote("1.0.0")
    second = _upload_and_promote("1.1.0")
    assert second["status"] == "promoted"
    resolver = arcagent.AnchoredSkillRevisionResolver(
        agent_did="did:arc:agent:ada",
        config_path=workspace / "arcagent.toml",
        anchor_factory=factory,
    )
    folder = workspace / "capabilities" / "skills" / "imported"
    history = resolver.revision_history(folder)
    assert [(version, "v1.1.0" in body) for _, version, body, _ in history] == [
        (2, True),
        (1, False),
    ]


def test_importing_a_new_version_without_an_authority_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    client, _workspace, _audit = _client(tmp_path)
    statuses = []
    for version in ("1.0.0", "1.1.0"):
        uploaded = client.post(
            "/api/agents/ada/capability-imports",
            headers={"Authorization": "Bearer viewer"},
            files={
                "file": (
                    "portable.zip",
                    _archive(("skills/imported/SKILL.md", _skill_version(version))),
                    "application/zip",
                )
            },
        )
        statuses.append(
            client.post(
                f"/api/agents/ada/capability-imports/{uploaded.json()['import_id']}/promote",
                headers={"Authorization": "Bearer operator"},
            ).status_code
        )
    assert statuses == [200, 422]
