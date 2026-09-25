"""Read and render a bounded agent HTML report in an isolated document."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import arcagent
import nh3
from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError, ReadAuthorizationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from arcui.audit import emit_read_audit
from arcui.query_validators import safe_choice
from arcui.report_authorization import ReportReadGrant, ReportReadRequest, ReportReadWorkerPool
from arcui.routes.agent_detail._common import (
    _VALID_ROOTS,
    _agent_did,
    _agent_root,
    _is_key_material,
    _resolve_root_path,
)

_TAGS = {
    "article",
    "aside",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "dd",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "small",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_CLEANER = nh3.Cleaner(
    tags=_TAGS,
    clean_content_tags={"script", "style", "iframe", "object", "svg", "form", "template"},
    attributes={
        "*": {"style", "title"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan"},
    },
    filter_style_properties={
        "background-color",
        "border",
        "border-color",
        "border-radius",
        "border-style",
        "border-width",
        "color",
        "display",
        "font-size",
        "font-weight",
        "height",
        "margin",
        "margin-left",
        "margin-right",
        "padding",
        "padding-left",
        "padding-right",
        "text-align",
        "width",
    },
    url_relative="deny",
    url_schemes=set(),
)
_CSP = (
    "default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; "
    "style-src 'unsafe-inline'; font-src 'none'; object-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'; navigate-to 'none'; sandbox"
)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def get_report_preview(request: Request) -> HTMLResponse | JSONResponse:
    """Return sanitized static HTML through the audited agent file reader."""
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    path = request.query_params.get("path", "")
    if Path(path).suffix.lower() not in {".html", ".htm"}:
        return _error("Choose an HTML report", 400)
    root, err = safe_choice(
        request.query_params.get("root", "workspace"), _VALID_ROOTS, error_label="Invalid root"
    )
    if err is not None:
        return err
    base = _resolve_root_path(agent_root, root)
    target = f"agent:{agent_id}:{root}:{path}"
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    auth = getattr(request.app.state, "auth_config", None)
    session = auth.identify(token) if auth is not None else None
    factory = getattr(request.app.state, "user_store_factory", None)
    if session is None or factory is None:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("An identified account session is required for report preview", 403)
    try:
        user = await asyncio.to_thread(lambda: factory().get(session.email))
    except Exception:
        emit_read_audit(request, target=target, operation="report.preview", outcome="error")
        return _error("Account authority is unavailable", 503)
    if user is None or user.disabled or user.did != session.did:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Session is no longer authorized", 403)
    agent_did = _agent_did(request, agent_id)
    if agent_did is None:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Agent identity is unavailable", 503)
    if getattr(request.state, "role", None) != "operator" or not user.is_operator:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Operator account required for report preview", 403)
    authority = getattr(request.app.state, "report_read_authority", None)
    pool = getattr(request.app.state, "report_read_workers", None)
    if authority is None or not isinstance(pool, ReportReadWorkerPool):
        emit_read_audit(request, target=target, operation="report.preview", outcome="error")
        return _error("Report read authority is unavailable", 503)
    try:
        expected_root = await pool.run(lambda: os.stat(base, follow_symlinks=False))
    except (OSError, RuntimeError):
        emit_read_audit(request, target=target, operation="report.preview", outcome="error")
        return _error("Report root is unavailable", 503)

    grant: ReportReadGrant | None = None

    def authorize_opened(root_stat: os.stat_result, file_stat: os.stat_result) -> bool:
        nonlocal grant
        if (root_stat.st_dev, root_stat.st_ino) != (
            expected_root.st_dev,
            expected_root.st_ino,
        ):
            return False
        request_model = ReportReadRequest(
            caller_did=session.did,
            agent_did=agent_did,
            report_id=path,
            root=root,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            size=file_stat.st_size,
            modified_ns=file_stat.st_mtime_ns,
        )
        decision = authority.authorize(request_model)
        if decision is None or not isinstance(decision, ReportReadGrant):
            return False
        if (
            decision.caller_did != session.did
            or decision.agent_did != agent_did
            or decision.report_id != path
        ):
            return False
        grant = decision
        return True

    try:
        if _is_key_material(base / path):
            emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
            return _error("Private key material cannot be viewed", 403)
        content = await pool.run(
            lambda: fs_reader.read_file(
                scope="agent",
                agent_id=agent_id,
                agent_root=base,
                rel_path=path,
                caller_did=session.did,
                authorize_opened=authorize_opened,
            )
        )
    except ReadAuthorizationError:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Report access denied", 403)
    except PathTraversalError as exc:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error(str(exc), 400)
    except FileTooLargeError as exc:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error(str(exc), 413)
    except FileNotFoundError:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Report not found", 404)
    except (OSError, RuntimeError, UnicodeError):
        emit_read_audit(request, target=target, operation="report.preview", outcome="error")
        return _error("Report is unavailable", 503)
    except Exception:
        emit_read_audit(request, target=target, operation="report.preview", outcome="error")
        return _error("Report read authority is unavailable", 503)
    if content.content_type != "text":
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Report is not text HTML", 415)
    digest = hashlib.sha256(content.content.encode("utf-8")).hexdigest()
    if grant is None or digest != grant.source_sha256:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Report provenance is invalid", 403)
    if arcagent.find_secret(content.content) is not None:
        emit_read_audit(request, target=target, operation="report.preview", outcome="denied")
        return _error("Report contains protected key material", 403)
    try:
        sanitized = await pool.run(lambda: _CLEANER.clean(content.content))
    except Exception:
        emit_read_audit(request, target=target, operation="report.preview", outcome="error")
        return _error("Report preview failed", 503)
    emit_read_audit(request, target=target, operation="report.preview", outcome="ok")
    response = HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        "</head><body>" + sanitized + "</body></html>"
    )
    response.headers.update(
        {
            "Content-Security-Policy": _CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": "inline",
            "X-Arc-Report-Source": grant.source_id,
            "X-Arc-Report-Sha256": grant.source_sha256,
        }
    )
    return response
