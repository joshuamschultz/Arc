"""System-level (user-wide ``~/.arc``) config editor routes.

``GET/PATCH /api/system-config/{file}`` for ``file`` in
``{arcagent, arcllm, arcrun}`` — the three fleet-wide TOML files that live
directly under the user config root (``${ARC_CONFIG_DIR:-~/.arc}``). Per-agent
``team/<agent>/<file>.toml`` layer OVER these; this editor is the fleet-default
layer an operator sees and edits.

Mirrors ``routes/agent_detail/config_files.py`` exactly (tomlkit round-trip,
operator-gated PATCH, re-parse-before-write, viewer secret redaction, 3-file
allowlist, missing file → 200 empty sections). The only difference is the target
path: the user config root rather than ``team/<agent>/``.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path

import tomlkit
from arctrust import arc_home
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.routes.agent_detail.config_files import (
    _CONFIG_FILES,
    _MAX_BODY_BYTES,
    _error,
    _redact,
)
from arcui.routes.arcllm_config import _atomic_write, _deep_merge, _tomlkit_to_plain
from arcui.schemas import AgentConfigFileResponse

logger = logging.getLogger("arcui.routes.system_config")


def _system_path(file: str) -> Path:
    """Resolve ``<config_dir>/{file}.toml`` under the user-wide config root."""
    return arc_home() / f"{file}.toml"


async def get_system_config(request: Request) -> JSONResponse:
    """GET /api/system-config/{file} — return one fleet config file's sections.

    Missing file → 200 with empty sections and ``mtime = 0.0`` so the editor
    shows a friendly empty state instead of an error.
    """
    file = request.path_params["file"]
    if file not in _CONFIG_FILES:
        return _error(f"Unknown config file: {file}", 404)

    path = _system_path(file)
    if not path.exists():
        return JSONResponse(
            AgentConfigFileResponse(file=file, sections={}, mtime=0.0).model_dump(mode="json")
        )

    try:
        with open(path, encoding="utf-8") as f:
            doc = tomlkit.load(f)
    except (OSError, tomlkit.exceptions.ParseError):
        logger.exception("Failed to read system %s.toml", file)
        return _error("Failed to read config", 500)

    sections = _tomlkit_to_plain(dict(doc))
    if getattr(request.state, "role", None) != "operator":
        sections = _redact(sections)
    return JSONResponse(
        AgentConfigFileResponse(
            file=file, sections=sections, mtime=path.stat().st_mtime
        ).model_dump(mode="json")
    )


async def patch_system_config(request: Request) -> JSONResponse:
    """PATCH /api/system-config/{file} — deep-merge a section update.

    Operator-only. Preserves comments via tomlkit and refuses to write a result
    that would not re-parse as TOML.
    """
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)

    file = request.path_params["file"]
    if file not in _CONFIG_FILES:
        return _error(f"Unknown config file: {file}", 404)

    path = _system_path(file)
    if not path.exists():
        return _error(f"{file}.toml not found", 404)

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return _error("Request body too large", 413)

    try:
        updates = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return _error("Invalid JSON body", 400)
    if not isinstance(updates, dict):
        return _error("Body must be a JSON object", 400)

    try:
        with open(path, encoding="utf-8") as f:
            doc = tomlkit.load(f)
    except (OSError, tomlkit.exceptions.ParseError):
        logger.exception("Failed to read system %s.toml", file)
        return _error("Failed to read config", 500)

    _deep_merge(doc, updates)

    # Refuse to persist a document that no longer parses as TOML.
    try:
        tomllib.loads(tomlkit.dumps(doc))
    except tomllib.TOMLDecodeError as exc:
        return _error(f"resulting config is not valid toml: {exc}", 400)

    try:
        _atomic_write(path, doc)
    except OSError as exc:
        logger.exception("Failed to write system %s.toml", file)
        return _error(f"Failed to write config: {type(exc).__name__}", 500)

    logger.info("System %s.toml updated: %s", file, list(updates.keys()))
    return JSONResponse(
        AgentConfigFileResponse(
            file=file,
            sections=_tomlkit_to_plain(dict(doc)),
            mtime=path.stat().st_mtime,
        ).model_dump(mode="json")
    )


routes = [
    Route("/api/system-config/{file}", get_system_config, methods=["GET"]),
    Route("/api/system-config/{file}", patch_system_config, methods=["PATCH"]),
]

__all__ = ["get_system_config", "patch_system_config", "routes"]
