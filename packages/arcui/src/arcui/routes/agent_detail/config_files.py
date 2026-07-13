"""Per-agent, per-file config editor routes.

``GET/PATCH /api/agents/{id}/config/{file}`` for ``file`` in
``{arcagent, arcllm, arcrun}`` — the three TOML files that live directly under
``team/<agent>/``. Reads return each file's top-level sections; PATCH deep-merges
one section update and atomic-writes it back, preserving comments and formatting
(tomlkit) and refusing any edit whose result would not re-parse as TOML.

Writes are operator-gated, mirroring ``arcllm_config.patch_arcllm_config``. The
round-trip helpers (``_tomlkit_to_plain`` / ``_deep_merge`` / ``_atomic_write``)
are shared with that module — the write recipe lives in one place.
"""

from __future__ import annotations

import json
import logging
import tomllib
from typing import Any

import tomlkit
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import _agent_root
from arcui.routes.arcllm_config import _atomic_write, _deep_merge, _tomlkit_to_plain
from arcui.schemas import AgentConfigFileResponse, ErrorResponse

logger = logging.getLogger("arcui.routes.agent_detail.config_files")

# The three per-agent config files, addressed by bare name (no extension).
# Anything else is rejected before a path is built — this is the traversal guard.
_CONFIG_FILES = frozenset({"arcagent", "arcllm", "arcrun"})

# Substrings marking a key whose value is redacted for non-operator readers
# (SC-28 / LLM07). Operators edit the real values; viewers never PATCH (403).
_SENSITIVE_SUBSTRINGS = ("key", "secret", "token", "password", "seed", "private")

# 64KB request-body ceiling, matching arcllm_config (LLM10 unbounded input).
_MAX_BODY_BYTES = 65_536


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    return any(sub in low for sub in _SENSITIVE_SUBSTRINGS)


def _redact(obj: Any) -> Any:
    """Recursively mask scalar values under sensitive-looking keys."""
    if isinstance(obj, dict):
        return {
            k: (
                "***" if _is_sensitive(k) and not isinstance(v, (dict, list)) else _redact(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


async def get_config_file(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/config/{file} — return one config file's sections.

    Missing file → 200 with empty sections and ``mtime = 0.0`` so the editor
    shows a friendly empty state instead of an error.
    """
    file = request.path_params["file"]
    if file not in _CONFIG_FILES:
        return _error(f"Unknown config file: {file}", 404)

    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    path = agent_root / f"{file}.toml"
    if not path.exists():
        return JSONResponse(
            AgentConfigFileResponse(file=file, sections={}, mtime=0.0).model_dump(mode="json")
        )

    try:
        with open(path, encoding="utf-8") as f:
            doc = tomlkit.load(f)
    except (OSError, tomlkit.exceptions.ParseError):
        logger.exception("Failed to read %s.toml for %s", file, agent_id)
        return _error("Failed to read config", 500)

    sections = _tomlkit_to_plain(dict(doc))
    if getattr(request.state, "role", None) != "operator":
        sections = _redact(sections)
    return JSONResponse(
        AgentConfigFileResponse(
            file=file, sections=sections, mtime=path.stat().st_mtime
        ).model_dump(mode="json")
    )


async def patch_config_file(request: Request) -> JSONResponse:
    """PATCH /api/agents/{id}/config/{file} — deep-merge a section update.

    Operator-only. Preserves comments via tomlkit and refuses to write a result
    that would not re-parse as TOML.
    """
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)

    file = request.path_params["file"]
    if file not in _CONFIG_FILES:
        return _error(f"Unknown config file: {file}", 404)

    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    path = agent_root / f"{file}.toml"
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
        logger.exception("Failed to read %s.toml for %s", file, agent_id)
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
        logger.exception("Failed to write %s.toml for %s", file, agent_id)
        return _error(f"Failed to write config: {type(exc).__name__}", 500)

    logger.info("Agent %s %s.toml updated: %s", agent_id, file, list(updates.keys()))
    return JSONResponse(
        AgentConfigFileResponse(
            file=file,
            sections=_tomlkit_to_plain(dict(doc)),
            mtime=path.stat().st_mtime,
        ).model_dump(mode="json")
    )


__all__ = ["get_config_file", "patch_config_file"]
