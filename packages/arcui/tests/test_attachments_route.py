from __future__ import annotations

from pathlib import Path

from arcgateway.attachment_scanner import ScanStatus
from arcgateway.media_store import MediaStore
from arcgateway.session import build_session_key
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.routes.attachments import routes


class Entry:
    agent_id = "olivia"
    did = "did:arc:agent:olivia"


def _app(tmp_path: Path, **store_options: object) -> Starlette:
    app = Starlette(routes=routes)
    app.state.auth_config = type("Auth", (), {"validate_token": lambda _, token: "viewer" if token == "viewer" else None})()
    app.state.roster_provider = lambda: [Entry()]
    app.state.attachment_store_for = lambda did: MediaStore(
        workspace=tmp_path, max_bytes=100, **store_options
    )
    return app


def test_upload_returns_opaque_manifest_and_derives_identity(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/api/agents/olivia/attachments",
            headers={"Authorization": "Bearer viewer", "x-session-key": "session-1"},
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["attachment_id"].startswith("att_")
    assert body["owner_did"].startswith("did:")
    assert "/" not in body["attachment_id"]
    assert str(tmp_path) not in response.text
    assert body["session_key"] == build_session_key(Entry.did, body["owner_did"])
    assert body["session_key"] != "session-1"


def test_upload_requires_auth_and_roster(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        assert client.post("/api/agents/olivia/attachments").status_code == 401
        assert client.post("/api/agents/missing/attachments", headers={"Authorization": "Bearer viewer"}).status_code == 404


def test_actual_bytes_are_limited_without_content_length(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/api/agents/olivia/attachments",
            headers={"Authorization": "Bearer viewer", "Content-Length": "1"},
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n" + b"x" * 200, "image/png")},
        )
    assert response.status_code == 413


def test_file_quota_is_enforced(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, max_files=0)) as client:
        response = client.post(
            "/api/agents/olivia/attachments",
            headers={"Authorization": "Bearer viewer"},
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
        )
    assert response.status_code == 413


def test_scanner_rejection_is_typed(tmp_path: Path) -> None:
    class Scanner:
        async def scan(self, path: Path, *, mime: str, sha256: str) -> ScanStatus:
            return ScanStatus.REJECTED

    with TestClient(_app(tmp_path, scanner=Scanner())) as client:
        response = client.post(
            "/api/agents/olivia/attachments",
            headers={"Authorization": "Bearer viewer"},
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
        )
    assert response.status_code == 422
