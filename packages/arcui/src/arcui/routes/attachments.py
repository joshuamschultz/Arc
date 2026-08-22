"""Authenticated, reference-only ArcUI attachment upload route."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from arcgateway.identity import derive_viewer_did
from arcgateway.media_store import (
    AttachmentManifest,
    AttachmentQuotaError,
    AttachmentValidationError,
    MediaStore,
    MediaTooLargeError,
)
from arcgateway.session import build_session_key
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class _FileStream:
    def __init__(self, file: Any, limit: int) -> None:
        self._file = file
        self._limit = limit
        self._read = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        chunk = self._file.read(min(64 * 1024, self._limit - self._read + 1))
        if hasattr(chunk, "__await__"):
            chunk = await chunk
        if not chunk:
            raise StopAsyncIteration
        self._read += len(chunk)
        return cast(bytes, chunk)


async def upload_attachment(request: Request) -> JSONResponse:
    """Store one multipart file using the authenticated caller identity."""
    token = request.headers.get("authorization", "")
    token = token.removeprefix("Bearer ")
    role = request.app.state.auth_config.validate_token(token)
    if role not in ("viewer", "operator"):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    agent_id = request.path_params["agent_id"]
    roster = getattr(request.app.state, "roster_provider", None)
    agent_did = None
    if roster:
        agent_did = next((entry.did for entry in roster() if entry.agent_id == agent_id), None)
    if not agent_did:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    owner_did = derive_viewer_did(token)
    session_key = build_session_key(agent_did, owner_did)
    store_for = getattr(request.app.state, "attachment_store_for", None)
    store: MediaStore | None = store_for(agent_did) if store_for else None
    if store is None:
        return JSONResponse({"error": "agent workspace unavailable"}, status_code=404)
    try:
        form = await request.form()
    except (ValueError, RuntimeError):
        return JSONResponse({"error": "malformed multipart"}, status_code=400)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse({"error": "multipart field 'file' is required"}, status_code=400)
    try:
        manifest = await store.store_stream(
            stream=_FileStream(upload.file, store.max_bytes),
            declared_name=str(upload.filename),
            declared_mime=str(upload.content_type or "application/octet-stream"),
            kind="file",
            owner_did=owner_did,
            agent_did=agent_did,
            session_key=session_key,
        )
    except MediaTooLargeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except AttachmentQuotaError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except AttachmentValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    payload = AttachmentManifest.model_validate(manifest).model_dump(mode="json")
    return JSONResponse(payload, status_code=201)


routes = [Route("/api/agents/{agent_id}/attachments", upload_attachment, methods=["POST"])]
