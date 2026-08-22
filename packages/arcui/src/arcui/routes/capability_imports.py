"""Agent-scoped, non-activating capability archive review routes.

Uploads are copied into ArcAgent's quarantine/staging seam and statically
reviewed there. This surface intentionally has no activation endpoint: the
existing trust approval route signs already-active loader artifacts, while a
staged import needs a dedicated promotion contract before it can be executable.
Returning a fake success here would turn review into an un-audited install.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import arcagent
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit

_MAX_UPLOAD_BYTES = arcagent.CapabilityImportLimits().max_compressed_bytes
_CHUNK_SIZE = 64 * 1024


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _agent(request: Request, agent_id: str) -> tuple[Path, str] | None:
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            did = str(getattr(entry, "did", ""))
            if did:
                return Path(entry.workspace_path), did
    return None


async def _write_upload(upload: UploadFile) -> Path:
    """Copy a multipart body to a temporary file without blocking the loop."""
    descriptor, raw_path = tempfile.mkstemp(prefix="arc-capability-import-", suffix=".zip")
    path = Path(raw_path)
    target = None
    try:
        total = 0
        target = os.fdopen(descriptor, "wb")
        descriptor = -1
        while chunk := await upload.read(_CHUNK_SIZE):
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise arcagent.CapabilityImportError("uploaded archive exceeds configured limit")
            await asyncio.to_thread(target.write, chunk)
        await asyncio.to_thread(target.flush)
        await asyncio.to_thread(os.fsync, target.fileno())
        await asyncio.to_thread(path.chmod, 0o600)
        return path
    except Exception:
        await asyncio.to_thread(path.unlink, missing_ok=True)
        raise
    finally:
        if target is not None:
            await asyncio.to_thread(target.close)
        elif descriptor >= 0:
            await asyncio.to_thread(os.close, descriptor)


async def list_imports(request: Request) -> JSONResponse:
    """List review metadata for one agent without exposing staged source bytes."""
    agent_id = request.path_params["agent_id"]
    resolved = _agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    workspace, _ = resolved
    service = arcagent.CapabilityImportService(workspace / "capabilities")
    rows = await asyncio.to_thread(service.list_reviews)
    return JSONResponse({"imports": [row.model_dump(mode="json") for row in rows]})


async def upload_import(request: Request) -> JSONResponse:
    """Stage and statically review one ZIP for exactly one agent workspace."""
    agent_id = request.path_params["agent_id"]
    resolved = _agent(request, agent_id)
    if resolved is None:
        return _error("agent_not_found", 404)
    workspace, target_did = resolved
    try:
        form = await request.form()
    except (RuntimeError, ValueError):
        return _error("malformed multipart", 400)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return _error("multipart field 'file' is required", 400)
    filename = str(upload.filename or "")
    if not filename.lower().endswith(".zip"):
        return _error("capability imports must be ZIP archives", 422)

    target = f"capability_import:{agent_id}"
    temporary: Path | None = None
    try:
        temporary = await _write_upload(upload)
        limits = arcagent.CapabilityImportLimits()
        capabilities_root = workspace / "capabilities"
        service = arcagent.CapabilityImportService(capabilities_root)
        intake = await asyncio.to_thread(
            arcagent.intake_capability_archive,
            temporary,
            capabilities_root,
            limits=limits,
        )
        manifest = await asyncio.to_thread(
            service.review, intake, target_agent_did=target_did, limits=limits
        )
        review = await asyncio.to_thread(service.review_summary, manifest)
        payload = review.model_dump(mode="json")
    except arcagent.CapabilityImportError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="capability_import.upload",
            outcome="denied",
            detail=type(exc).__name__,
        )
        return _error(str(exc), 422)
    except (OSError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="capability_import.upload",
            outcome="error",
            detail=type(exc).__name__,
        )
        return _error("capability import unavailable", 503)
    finally:
        if temporary is not None:
            await asyncio.to_thread(temporary.unlink, missing_ok=True)

    emit_mutation_audit(
        request,
        target=f"capability_import:{agent_id}:{payload['import_id']}",
        operation="capability_import.upload",
        outcome="applied",
    )
    return JSONResponse(payload, status_code=201)


routes = [
    Route("/api/agents/{agent_id}/capability-imports", list_imports, methods=["GET"]),
    Route("/api/agents/{agent_id}/capability-imports", upload_import, methods=["POST"]),
]

__all__ = ["list_imports", "routes", "upload_import"]
