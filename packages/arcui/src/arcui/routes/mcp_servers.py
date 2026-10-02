"""P12 — add your own MCP server from the web: preview its tools, then add it.

Two operator-only routes. ``POST /api/mcp-servers/preview`` asks the server what it offers
and writes nothing. ``POST /api/mcp-servers`` generates a signed connector bundle for it,
installs it and grants it, through :meth:`arcagent.Connections.add_mcp_server` — the same
seam ``arc connector add-mcp`` drives, so every safety rule (what may launch, where it may
connect, which tools it may expose, who signed it) is the generator's, not re-derived here.

What this module adds, and only this: the operator gate, the body cap, and a response that
has no field able to hold a credential. A secret the operator types is consumed and dropped.
It is not returned, not logged, not in any error this module raises, and not in the audit
detail, which carries the SHA-256 of the spec (a spec holds no secret) and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any

import arcagent
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail.config_files import BodyTooLargeError, _error, read_json_object
from arcui.routes.connectors import (
    _connections,
    _is_operator,
    _submitted_agents,
    _submitted_secrets,
    _unknown_agents,
)
from arcui.schemas import McpPreviewResponse, McpServerAddedResponse, McpToolView

logger = logging.getLogger("arcui.routes.mcp_servers")

#: Seconds a preview may wait for a server to list its tools.
_PREVIEW_TIMEOUT = 30.0

_SPEC_FIELDS = ("name", "display", "description", "transport", "url", "auth_header", "auth_scheme")


def _tags_for(transport: str) -> list[str]:
    tags = arcagent.DEFAULT_HTTP_TAGS if transport == "http" else arcagent.DEFAULT_STDIO_TAGS
    return list(tags)


def _spec_from(body: dict[str, Any], *, with_tools: bool) -> arcagent.McpServerSpec:
    """Build the spec from a request body, or raise ``ValueError`` naming fields only.

    Pydantic's own messages quote the offending input, so they are never relayed: a
    refusal that echoed a field back could carry whatever the operator mistyped into it.
    """
    fields: dict[str, Any] = {key: body[key] for key in _SPEC_FIELDS if key in body}
    argv, env_refs = body.get("argv", []), body.get("env_refs", {})
    if not isinstance(argv, list) or not all(isinstance(token, str) for token in argv):
        raise ValueError("argv must be a list of strings")
    if not isinstance(env_refs, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_refs.items()
    ):
        raise ValueError("env_refs must map credential field names to variable names")
    fields["argv"], fields["env_refs"] = tuple(argv), env_refs
    fields["tools"] = _tools_from(body.get("tools")) if with_tools else {}
    try:
        return arcagent.McpServerSpec(**fields)
    except (ValidationError, TypeError) as exc:
        names = (
            sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
            if isinstance(exc, ValidationError)
            else []
        )
        raise ValueError(
            f"invalid server description: {', '.join(names) or 'check the fields'}"
        ) from None


def _tools_from(raw: object) -> dict[str, arcagent.McpToolChoice]:
    if not isinstance(raw, dict):
        raise ValueError("tools must map each chosen tool name to its settings")
    chosen: dict[str, arcagent.McpToolChoice] = {}
    for verb, settings in raw.items():
        if not isinstance(verb, str) or not isinstance(settings, dict):
            raise ValueError("each chosen tool needs a settings object")
        try:
            chosen[verb] = arcagent.McpToolChoice(**settings)
        except (ValidationError, TypeError):
            raise ValueError(
                "tool settings accept classification, capability_tags, description"
            ) from None
    return chosen


async def _read_body(request: Request) -> dict[str, Any] | JSONResponse:
    if not _is_operator(request):
        return _error("Operator role required", 403)
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)
    return body


async def post_mcp_preview(request: Request) -> JSONResponse:
    """POST /api/mcp-servers/preview — list the tools a server offers. Writes nothing."""
    body = await _read_body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        spec = _spec_from(body, with_tools=False)
        found = await _connections(request).preview_mcp_server(
            spec,
            secret_values=_submitted_secrets(body),
            timeout=float(getattr(request.app.state, "mcp_preview_timeout", _PREVIEW_TIMEOUT)),
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except arcagent.ExtensionError as exc:
        return _error(exc.message, 502 if exc.code == "MCP_DISCOVERY_FAILED" else 400)
    return JSONResponse(
        McpPreviewResponse(
            tools=[
                McpToolView(
                    name=tool.name,
                    description=tool.description,
                    usable=tool.usable,
                    reason=tool.reason,
                )
                for tool in found
            ],
            suggested_tags=_tags_for(spec.transport),
        ).model_dump(mode="json")
    )


async def post_mcp_server(request: Request) -> JSONResponse:
    """POST /api/mcp-servers — generate, sign, install and grant an operator's MCP server."""
    body = await _read_body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        spec = _spec_from(body, with_tools=True)
    except ValueError as exc:
        return _error(str(exc), 400)
    agents = _submitted_agents(body)
    unknown = _unknown_agents(request, agents)
    if unknown:
        return _error(f"no agent named {', '.join(unknown)} on this deployment", 400)

    target = f"connector:{spec.name}"
    digest = arcagent.mcp_spec_digest(spec)
    try:
        added = await _connections(request).add_mcp_server(
            spec, agents=agents, secret_values=_submitted_secrets(body)
        )
    except arcagent.ExtensionError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="mcp_server.add",
            outcome="denied",
            detail=f"{exc.code} {digest}",
        )
        return _error(exc.message, 400)
    emit_mutation_audit(
        request,
        target=target,
        operation="mcp_server.add",
        outcome="applied",
        detail=added.spec_sha256,
    )
    return JSONResponse(
        McpServerAddedResponse(
            instance=added.report.instance,
            extension=added.report.extension,
            tools=list(added.report.tools),
            detail=added.report.detail,
            agents=agents,
            spec_sha256=added.spec_sha256,
        ).model_dump(mode="json")
    )


routes = [
    Route("/api/mcp-servers/preview", post_mcp_preview, methods=["POST"]),
    Route("/api/mcp-servers", post_mcp_server, methods=["POST"]),
]
