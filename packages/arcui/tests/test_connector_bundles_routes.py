"""UJ-6 — the Connections page adds a connector package: upload, review, approve, remove.

Real routes, a real ``Connections`` seam and a real signed bundle on disk; only the arcstore
backend is in memory. What these pin is the trust shape: operator-only writes, every write
audited (refusals too), a body cap that holds before parsing, refusals that carry a reason
code, and an approval that installs exactly what was reviewed.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcstore.backends.memory import FakeBackend
from arctrust.paths import installed_extensions_dir
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connector_bundles import routes as bundle_routes

FIXTURE = (
    Path(__file__).resolve().parents[2] / "arcagent" / "tests" / "fixtures" / "notes_connector"
)
OPERATOR = {"Authorization": "Bearer operator"}
VIEWER = {"Authorization": "Bearer viewer"}


def fixture_files() -> dict[str, bytes]:
    return {
        p.relative_to(FIXTURE).as_posix(): p.read_bytes()
        for p in sorted(FIXTURE.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "dot-arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return tmp_path


@pytest.fixture
def app_client(world: Path) -> tuple[TestClient, MagicMock]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=bundle_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.arcstore_backend = FakeBackend()
    audit = MagicMock()
    app.state.audit = audit
    return TestClient(app), audit


def _mutations(audit: MagicMock) -> list[dict[str, Any]]:
    return [call.args[1] for call in audit.audit_event.call_args_list]


def _upload(client: TestClient, data: bytes, headers: dict[str, str] = OPERATOR) -> Any:
    return client.post(
        "/api/connector-bundles/upload",
        files={"file": ("notes.zip", data, "application/zip")},
        headers=headers,
    )


def test_a_viewer_cannot_upload(app_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = app_client
    response = _upload(client, make_zip(fixture_files()), VIEWER)
    assert response.status_code == 403
    assert not (installed_extensions_dir() / ".staging").exists()


def test_upload_returns_a_review_and_installs_nothing(
    app_client: tuple[TestClient, MagicMock],
) -> None:
    client, audit = app_client

    response = _upload(client, make_zip(fixture_files()))

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["staging_id"]) == 32
    assert 3500 < body["expires_in"] <= 3600
    review = body["review"]
    assert review["name"] == "notes"
    assert review["publisher"] == {"status": "unsigned", "signer_did": ""}
    assert review["confirm_required"] is True
    assert review["tools"][0]["name"] == "notes_echo"
    assert review["secrets"][0]["name"] == "api_token"
    assert {f["path"] for f in review["files"]} >= {"extension.toml", "arc_ext_notes/__init__.py"}
    assert review["update"] is None
    assert not (installed_extensions_dir() / "notes").exists()
    assert [(m["operation"], m["outcome"]) for m in _mutations(audit)] == [
        ("connector_bundle.upload", "applied")
    ]


def test_approve_signs_and_installs_then_lists(app_client: tuple[TestClient, MagicMock]) -> None:
    client, audit = app_client
    staged = _upload(client, make_zip(fixture_files())).json()

    wrong = client.post(
        f"/api/connector-bundles/{staged['staging_id']}/approve",
        json={"confirm_name": "nope"},
        headers=OPERATOR,
    )
    assert wrong.status_code == 400
    assert wrong.json()["reason"] == "confirm_mismatch"

    approved = client.post(
        f"/api/connector-bundles/{staged['staging_id']}/approve",
        json={"confirm_name": "notes"},
        headers=OPERATOR,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["installed"]["name"] == "notes"
    assert (installed_extensions_dir() / "notes" / "extension.toml.arcsig").is_file()

    listing = client.get("/api/connector-bundles", headers=VIEWER)
    assert listing.status_code == 200
    assert [(b["name"], b["version"], b["used_by"]) for b in listing.json()["installed"]] == [
        ("notes", "1.0.0", [])
    ]
    outcomes = [(m["operation"], m["outcome"]) for m in _mutations(audit)]
    assert ("connector_bundle.approve", "denied") in outcomes
    assert ("connector_bundle.approve", "applied") in outcomes


def test_a_viewer_cannot_approve_or_remove(app_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = app_client
    staged = _upload(client, make_zip(fixture_files())).json()
    approve = client.post(
        f"/api/connector-bundles/{staged['staging_id']}/approve",
        json={"confirm_name": "notes"},
        headers=VIEWER,
    )
    assert approve.status_code == 403
    assert client.delete("/api/connector-bundles/notes", headers=VIEWER).status_code == 403
    assert (
        client.delete(
            f"/api/connector-bundles/staging/{staged['staging_id']}", headers=VIEWER
        ).status_code
        == 403
    )


def test_files_swapped_after_review_are_refused(app_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = app_client
    staged = _upload(client, make_zip(fixture_files())).json()
    code = next(
        (installed_extensions_dir() / ".staging").glob("*/notes/arc_ext_notes/__init__.py")
    )
    code.write_text("import os\n")

    response = client.post(
        f"/api/connector-bundles/{staged['staging_id']}/approve",
        json={"confirm_name": "notes"},
        headers=OPERATOR,
    )

    assert response.status_code == 409
    assert response.json()["reason"] == "changed_after_review"
    assert not (installed_extensions_dir() / "notes").exists()


def test_zip_slip_upload_is_refused_with_a_reason(
    app_client: tuple[TestClient, MagicMock],
) -> None:
    client, audit = app_client
    response = _upload(client, make_zip({**fixture_files(), "../evil.py": b"x = 1\n"}))
    assert response.status_code == 400
    assert response.json()["reason"] == "unsafe_path"
    assert [(m["operation"], m["outcome"], m["detail"]) for m in _mutations(audit)] == [
        ("connector_bundle.upload", "denied", "unsafe_path")
    ]


def test_an_oversized_body_is_refused_before_it_is_parsed(
    app_client: tuple[TestClient, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcagent.extension import bundle_import

    monkeypatch.setattr(bundle_import, "MAX_ARCHIVE_BYTES", 10)
    monkeypatch.setattr("arcui.routes.connector_bundles._BODY_SLACK", 10)
    client, _ = app_client
    response = _upload(client, make_zip(fixture_files()))
    assert response.status_code == 413
    assert response.json()["reason"] == "too_large"


def test_remove_lists_the_connections_still_using_it(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, _ = app_client
    staged = _upload(client, make_zip(fixture_files())).json()
    client.post(
        f"/api/connector-bundles/{staged['staging_id']}/approve",
        json={"confirm_name": "notes"},
        headers=OPERATOR,
    )
    registry = ConnectionRegistry(world / "arc")
    registry.define("work_notes", Connection(extension="notes"))

    in_use = client.delete("/api/connector-bundles/notes", headers=OPERATOR)
    assert in_use.status_code == 409
    assert in_use.json()["used_by"] == ["work_notes"]

    registry.forget("work_notes")
    removed = client.delete("/api/connector-bundles/notes", headers=OPERATOR)
    assert removed.status_code == 200
    assert not (installed_extensions_dir() / "notes").exists()
    assert client.delete("/api/connector-bundles/notes", headers=OPERATOR).status_code == 404


def test_discarding_a_staged_package(app_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = app_client
    staged = _upload(client, make_zip(fixture_files())).json()
    path = f"/api/connector-bundles/staging/{staged['staging_id']}"
    assert client.delete(path, headers=OPERATOR).status_code == 200
    assert client.delete(path, headers=OPERATOR).status_code == 404
    approve = client.post(
        f"/api/connector-bundles/{staged['staging_id']}/approve",
        json={"confirm_name": "notes"},
        headers=OPERATOR,
    )
    assert approve.status_code == 404


def test_an_unsigned_package_already_here_is_listed_and_can_be_staged(
    app_client: tuple[TestClient, MagicMock], world: Path
) -> None:
    client, _ = app_client
    planted = world / "arc" / "extensions" / "notes"
    for name, data in fixture_files().items():
        (planted / name).parent.mkdir(parents=True, exist_ok=True)
        (planted / name).write_bytes(data)

    listing = client.get("/api/connector-bundles", headers=OPERATOR).json()
    assert [b["name"] for b in listing["unsigned_local"]] == ["notes"]
    assert "review and sign" in listing["unsigned_local"][0]["reason"]

    staged = client.post(
        "/api/connector-bundles/stage-local", json={"name": "notes"}, headers=OPERATOR
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["review"]["name"] == "notes"
    assert (
        client.post(
            "/api/connector-bundles/stage-local", json={"name": "notes"}, headers=VIEWER
        ).status_code
        == 403
    )
