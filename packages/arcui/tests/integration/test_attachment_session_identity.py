"""End-to-end custody and WebSocket attachment identity coverage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcgateway.adapters.web import WebPlatformAdapter
from arcgateway.identity import derive_viewer_did
from arcgateway.media_store import AttachmentClaimError, MediaStore
from arcgateway.parts import MediaPart
from arcgateway.session import build_session_key
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.routes.attachments import routes


class _Entry:
    agent_id = "olivia"
    did = "did:arc:agent:olivia"


class _Auth:
    def validate_token(self, token: str) -> str | None:
        return "viewer" if token in {"viewer", "other"} else None


class _Socket:
    async def send_json(self, payload: dict[str, Any]) -> None:
        json.dumps(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason


def _app(tmp_path: Path) -> Starlette:
    app = Starlette(routes=routes)
    app.state.auth_config = _Auth()
    app.state.roster_provider = lambda: [_Entry()]
    app.state.attachment_store_for = lambda did: MediaStore(workspace=tmp_path, max_bytes=1024)
    return app


@pytest.mark.asyncio
async def test_upload_claims_after_rotation_once_and_rejects_replay_or_other_user(
    tmp_path: Path,
) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/api/agents/olivia/attachments",
            headers={"Authorization": "Bearer viewer", "x-session-key": "client-controlled"},
            files={"file": ("photo.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
        )
    assert response.status_code == 201, response.text
    manifest = response.json()
    attachment_id = manifest["attachment_id"]
    owner_did = derive_viewer_did("viewer")
    stable_session = build_session_key(_Entry.did, owner_did)
    assert manifest["session_key"] == stable_session

    store = MediaStore(workspace=tmp_path, max_bytes=1024)
    claims: list[tuple[str, str]] = []
    events: list[Any] = []

    async def on_message(event: Any) -> None:
        events.append(event)

    def claim(
        user_did: str,
        agent_did: str,
        session_key: str,
        _chat_id: str,
        ids: list[str],
    ) -> list[MediaPart]:
        claims.append((user_did, session_key))
        return [
            MediaPart(
                kind="file",
                mime=stored.mime,
                declared_name=stored.declared_name,
                ref=stored.ref,
            )
            for stored in (
                store.claim(
                    attachment_id=identifier,
                    owner_did=user_did,
                    agent_did=agent_did,
                    session_key=session_key,
                )
                for identifier in ids
            )
        ]

    adapter = WebPlatformAdapter(on_message=on_message, claim_attachments=claim)
    rotated_socket = _Socket()
    rotated_chat = "rotated-chat-generation-1"
    adapter.register_socket(rotated_socket, _Entry.did, owner_did, rotated_chat)

    # The browser's chat id has advanced after /new, but the upload remains
    # bound to the stable (agent, owner) conversation key.
    await adapter.ingest(
        rotated_chat,
        "",
        client_seq=1,
        ws=rotated_socket,
        attachment_ids=[attachment_id],
    )
    assert len(events) == 1
    assert events[0].parts[0].ref == manifest["workspace_ref"]
    assert claims == [(owner_did, stable_session)]

    # The adapter's per-socket monotonic sequence guard prevents a replay from
    # invoking the claim path a second time.
    with pytest.raises(ValueError, match="replay"):
        await adapter.ingest(
            rotated_chat,
            "",
            client_seq=1,
            ws=rotated_socket,
            attachment_ids=[attachment_id],
        )
    assert len(claims) == 1

    other_socket = _Socket()
    other_did = derive_viewer_did("other")
    other_chat = "other-user-chat"
    adapter.register_socket(other_socket, _Entry.did, other_did, other_chat)
    with pytest.raises(AttachmentClaimError, match="identity mismatch"):
        await adapter.ingest(
            other_chat,
            "",
            client_seq=1,
            ws=other_socket,
            attachment_ids=[attachment_id],
        )
    await adapter.disconnect()
