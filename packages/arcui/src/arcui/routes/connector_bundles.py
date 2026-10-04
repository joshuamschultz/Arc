"""UJ-6 — add a connector package from the Connections page: upload, review, sign, remove.

The web re-derives nothing about trust. Every step hands off to
:class:`arcagent.BundleStaging` and the ``Connections`` seam, which sign with the operator
signer handle and install through the same ``install_bundle`` the CLI uses, so the bundle
an operator approves here is byte-for-byte the bundle ``arc connector sign`` +
``install-bundle`` would have produced.

What this module adds, and only this:

* **The operator gate.** Every write is ``operator``-only and audited (accepted and refused
  alike). The listing is readable by a viewer.
* **A bounded body.** An upload is read with a hard cap before multipart parsing starts, so
  an oversized body is refused without being buffered.
* **Plain refusals.** A refusal carries its ``reason`` code (and ``used_by`` for a package
  still in use) so the page can say what to do. No refusal names a command.

Staged packages live in one :class:`arcagent.BundleStaging` per process, which holds the
reviewed digests in memory and expires a staging after an hour.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import arcagent
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Message

from arcui.audit import emit_mutation_audit, emit_read_audit, operator_audit_sink
from arcui.routes.agent_detail.config_files import BodyTooLargeError, read_json_object
from arcui.routes.connectors import _connections, _is_operator

logger = logging.getLogger("arcui.routes.connector_bundles")

#: Multipart framing on top of the largest archive arcagent accepts.
_BODY_SLACK = 1024 * 1024
_STAGING_ID = re.compile(r"^[0-9a-f]{32}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: How each refusal reads over HTTP. Anything unlisted is a 400.
_STATUS = {
    "expired": 404,
    "not_installed": 404,
    "not_found": 404,
    "changed_after_review": 409,
    "in_use": 409,
    "too_large": 413,
    "federal_no_allowlist": 403,
    "federal_unsigned": 403,
}


def _error(message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _staging(request: Request) -> arcagent.BundleStaging:
    """The process's one staging area, made on first use."""
    staging = getattr(request.app.state, "connector_bundle_staging", None)
    if staging is None:
        staging = arcagent.BundleStaging()
        request.app.state.connector_bundle_staging = staging
    return staging


def _refused(exc: arcagent.ExtensionError) -> JSONResponse:
    reason = str(exc.details.get("reason", exc.code))
    extra: dict[str, Any] = {"reason": reason}
    for key in ("action", "used_by"):
        if key in exc.details:
            extra[key] = exc.details[key]
    return _error(exc.message, _STATUS.get(reason, 400), **extra)


def _review_payload(review: arcagent.BundleReview) -> dict[str, Any]:
    update = review.update
    return {
        "name": review.name,
        "display_name": review.display_name,
        "version": review.version,
        "description": review.description,
        "attachment": review.attachment,
        "tier_floor": review.tier_floor,
        "publisher": {
            "status": review.publisher.status,
            "signer_did": review.publisher.signer_did,
        },
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "classification": tool.classification,
                "capability_tags": list(tool.capability_tags),
                "network": tool.network,
            }
            for tool in review.tools
        ],
        "secrets": [
            {
                "name": secret.name,
                "prompt": secret.prompt,
                "sensitive": secret.sensitive,
                "required": secret.required,
            }
            for secret in review.secrets
        ],
        "host_programs": list(review.host_programs),
        "egress_hosts": list(review.egress_hosts),
        "skills": list(review.skills),
        "files": [
            {"path": item.path, "size": item.size, "executes": item.executes}
            for item in review.files
        ],
        "executes_code": review.executes_code,
        "needs_network": review.needs_network,
        "flags": list(review.flags),
        "digest": review.digest,
        "confirm_required": review.confirm_required,
        "update": None
        if update is None
        else {
            "installed_version": update.installed_version,
            "tools_added": list(update.tools_added),
            "tools_removed": list(update.tools_removed),
            "tools_changed": list(update.tools_changed),
            "new_secrets": list(update.new_secrets),
            "new_egress": list(update.new_egress),
        },
    }


def _installed_payload(bundle: arcagent.InstalledBundle) -> dict[str, Any]:
    return {
        "name": bundle.name,
        "display_name": bundle.display_name,
        "version": bundle.version,
        "signer_did": bundle.signer_did,
        "used_by": list(bundle.used_by),
    }


def _staged_payload(request: Request, staged: arcagent.StagedBundle) -> dict[str, Any]:
    remaining = max(0, int(staged.expires_at - _staging(request).now()))
    return {
        "staging_id": staged.staging_id,
        "expires_in": remaining,
        "review": _review_payload(staged.review),
    }


# --- listing ------------------------------------------------------------------------


async def list_bundles(request: Request) -> JSONResponse:
    """GET /api/connector-bundles — installed packages, and unsigned ones waiting."""
    try:
        connections = _connections(request)
        installed = arcagent.installed_bundles(connections)
        waiting = [
            {"name": bundle.name, "reason": bundle.reason}
            for bundle in arcagent.unsigned_local_bundles(connections)
        ]
    except arcagent.ExtensionError as exc:
        return _refused(exc)
    emit_read_audit(
        request, target="connector_bundles", operation="connector_bundle.list", outcome="ok"
    )
    return JSONResponse(
        {"installed": [_installed_payload(b) for b in installed], "unsigned_local": waiting}
    )


# --- staging --------------------------------------------------------------------------


async def _capped_body(request: Request, cap: int) -> bytes | None:
    """The request body, or ``None`` the moment it passes ``cap``."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _uploaded_archive(request: Request, body: bytes) -> Path | JSONResponse:
    """Parse the multipart body already read, and spill its one file to a private temp file."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    try:
        form = await Request(request.scope, replay).form(max_files=1, max_fields=4)
    except (RuntimeError, ValueError, AssertionError):
        return _error("send the package as a multipart upload named 'file'", 400)
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return _error("send the package as a multipart upload named 'file'", 400)
    data = await upload.read()
    descriptor, raw = tempfile.mkstemp(prefix="arc-connector-package-")
    path = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            await asyncio.to_thread(handle.write, data)
        path.chmod(0o600)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


async def upload_bundle(request: Request) -> JSONResponse:
    """POST /api/connector-bundles/upload — stage and review a package. Installs nothing."""
    if not _is_operator(request):
        return _error("Operator role required", 403)
    body = await _capped_body(request, _staging(request).max_archive_bytes + _BODY_SLACK)
    if body is None:
        _audit(request, "upload", "?", "denied", "too_large")
        return _error("the package is over 50 MB", 413, reason="too_large")
    archive = await _uploaded_archive(request, body)
    if isinstance(archive, JSONResponse):
        _audit(request, "upload", "?", "denied", "malformed")
        return archive
    try:
        staged = await asyncio.to_thread(
            _staging(request).stage_archive,
            archive,
            connections=_connections(request),
            audit_sink=operator_audit_sink(request),
        )
    except arcagent.ExtensionError as exc:
        _audit(request, "upload", "?", "denied", str(exc.details.get("reason", exc.code)))
        return _refused(exc)
    finally:
        archive.unlink(missing_ok=True)
    _audit(request, "upload", staged.review.name, "applied", staged.review.digest)
    return JSONResponse(_staged_payload(request, staged))


async def stage_local_bundle(request: Request) -> JSONResponse:
    """POST /api/connector-bundles/stage-local — review an unsigned package already here."""
    if not _is_operator(request):
        return _error("Operator role required", 403)
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    name = body.get("name") if body is not None else None
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return _error("name the package to review", 400)
    try:
        staged = await asyncio.to_thread(
            _staging(request).stage_local,
            name,
            connections=_connections(request),
            audit_sink=operator_audit_sink(request),
        )
    except arcagent.ExtensionError as exc:
        _audit(request, "stage_local", name, "denied", str(exc.details.get("reason", exc.code)))
        return _refused(exc)
    _audit(request, "stage_local", name, "applied", staged.review.digest)
    return JSONResponse(_staged_payload(request, staged))


async def approve_bundle(request: Request) -> JSONResponse:
    """POST /api/connector-bundles/{staging_id}/approve — sign the reviewed bytes, install."""
    if not _is_operator(request):
        return _error("Operator role required", 403)
    staging_id = request.path_params["staging_id"]
    if not _STAGING_ID.fullmatch(staging_id):
        return _error("no such package", 404, reason="expired")
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    confirm = body.get("confirm_name", "") if body is not None else ""
    if not isinstance(confirm, str):
        return _error("confirm_name must be text", 400)
    staging = _staging(request)
    pending = staging.get(staging_id)
    name = pending.review.name if pending is not None else "?"
    try:
        installed = await asyncio.to_thread(
            staging.approve,
            staging_id,
            confirm_name=confirm,
            connections=_connections(request),
            audit_sink=operator_audit_sink(request),
        )
    except arcagent.ExtensionError as exc:
        _audit(request, "approve", name, "denied", str(exc.details.get("reason", exc.code)))
        return _refused(exc)
    _audit(request, "approve", installed.name, "applied", pending.review.digest if pending else "")
    return JSONResponse({"installed": _installed_payload(installed)})


async def discard_staging(request: Request) -> JSONResponse:
    """DELETE /api/connector-bundles/staging/{staging_id} — drop a package under review."""
    if not _is_operator(request):
        return _error("Operator role required", 403)
    staging_id = request.path_params["staging_id"]
    discarded = bool(_STAGING_ID.fullmatch(staging_id)) and _staging(request).discard(staging_id)
    _audit(request, "discard", staging_id, "applied" if discarded else "denied", "")
    if not discarded:
        return _error("no such package", 404, reason="expired")
    return JSONResponse({"discarded": staging_id})


async def remove_bundle(request: Request) -> JSONResponse:
    """DELETE /api/connector-bundles/{name} — remove a package no connection uses."""
    if not _is_operator(request):
        return _error("Operator role required", 403)
    name = request.path_params["name"]
    if not _NAME.fullmatch(name):
        return _error("no such package", 404, reason="not_installed")
    try:
        await asyncio.to_thread(
            arcagent.remove_installed_bundle,
            name,
            connections=_connections(request),
            audit_sink=operator_audit_sink(request),
        )
    except arcagent.ExtensionError as exc:
        _audit(request, "remove", name, "denied", str(exc.details.get("reason", exc.code)))
        return _refused(exc)
    _audit(request, "remove", name, "applied", "")
    return JSONResponse({"removed": name})


def _audit(request: Request, operation: str, name: str, outcome: str, detail: str) -> None:
    emit_mutation_audit(
        request,
        target=f"connector_bundle:{name}",
        operation=f"connector_bundle.{operation}",
        outcome=outcome,
        detail=detail,
    )


routes = [
    Route("/api/connector-bundles", list_bundles, methods=["GET"]),
    Route("/api/connector-bundles/upload", upload_bundle, methods=["POST"]),
    Route("/api/connector-bundles/stage-local", stage_local_bundle, methods=["POST"]),
    Route("/api/connector-bundles/{staging_id}/approve", approve_bundle, methods=["POST"]),
    Route("/api/connector-bundles/staging/{staging_id}", discard_staging, methods=["DELETE"]),
    Route("/api/connector-bundles/{name}", remove_bundle, methods=["DELETE"]),
]
