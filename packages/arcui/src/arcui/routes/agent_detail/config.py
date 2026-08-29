"""`/api/agents/{id}/config` + `/files/tree` + `/files/read` route handlers.

The config endpoint exposes the whitelisted TOML config (no secrets);
the file-tree and file-read endpoints expose the agent's file tree and
individual file content through ``arcgateway.fs_reader`` (single audited
chokepoint per SPEC-022).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

import arcagent
from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.query_validators import safe_choice
from arcui.routes.agent_detail._common import (
    _CALLER_DID,
    _CONFIG_WHITELIST,
    _VALID_ROOTS,
    _agent_root,
    _is_key_material,
    _resolve_root_path,
)
from arcui.schemas import (
    ConfigResponse,
    ErrorResponse,
    FileReadResponse,
    FilesTreeEntry,
    FilesTreeResponse,
)

_logger = logging.getLogger("arcui.agent_detail.config")

# H-039: an on-disk arcagent.toml is allowed to omit a key entirely — hand
# edits, or a file scaffolded before a field existed. The dashboard still
# shows the FULL option set (every field the models declare, present at its
# default, real value where the file has one) rather than only what happened
# to survive to disk. `_AGENT_TIERS` is duplicated from
# `arccli.commands.agent._common.AGENT_TIERS` in spelling only — arcui may
# not depend on arccli (a top-layer CLI, not something a sibling surface
# imports), and this is one closed set of three literal strings, not a field
# list.
_AGENT_TIERS = frozenset({"personal", "enterprise", "federal"})


async def get_config(request: Request) -> JSONResponse:
    """Return the agent's whitelisted config + raw TOML.

    The whitelisted ``config`` object is the safe surface — drop any section
    not on :data:`_CONFIG_WHITELIST`. Raw text is returned as a separate
    ``raw`` field for the operator's "View raw" toggle, which already lives
    on the gateway side of the trust boundary (the operator can read these
    files directly, this is just convenience).
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=agent_root,
            rel_path="arcagent.toml",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse(
            ErrorResponse(error="arcagent.toml not found").model_dump(mode="json"),
            status_code=404,
        )
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )

    try:
        parsed = tomllib.loads(content.content)
    except tomllib.TOMLDecodeError as exc:
        return JSONResponse(
            ErrorResponse(error=f"invalid toml: {exc}").model_dump(mode="json"),
            status_code=500,
        )

    return JSONResponse(
        ConfigResponse(
            config=_whitelist_config(_with_full_option_defaults(agent_id, parsed)),
            raw=content.content,
            mtime=content.mtime,
        ).model_dump(mode="json")
    )


def _with_full_option_defaults(agent_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """Fill in every option this agent's file doesn't carry, at its default.

    Real on-disk values always win (``deep_merge``'s "later wins" — parsed is
    the override). Best-effort: a defaults-generation failure must never take
    down the config tab, so this falls back to the file's own content alone.
    """
    try:
        name = parsed.get("agent", {}).get("name") or agent_id
        tier = parsed.get("security", {}).get("tier")
        if tier not in _AGENT_TIERS:
            tier = "personal"
        defaults = arcagent.config_render.default_agent_config_dict(name=name, tier=tier)
        return arcagent.deep_merge(defaults, parsed)
    except Exception:  # reason: fail-open — the raw/parsed view must still render
        _logger.warning("could not compute full config defaults for %s", agent_id, exc_info=True)
        return parsed


def _whitelist_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Keep only top-level sections on the whitelist; drop everything else."""
    return {k: cfg[k] for k in _CONFIG_WHITELIST if k in cfg}


async def get_files_tree(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    root_arg, err = safe_choice(
        request.query_params.get("root", "workspace"),
        _VALID_ROOTS,
        error_label="Invalid root",
    )
    if err is not None:
        return err

    base = _resolve_root_path(agent_root, root_arg)
    # Lazy, one level at a time: the tree UI fetches each directory's immediate
    # children via ``?path=`` as folders are expanded. ``max_depth=0`` lists only
    # the direct children (no grandchildren) so each entry renders exactly once —
    # max_depth=1 returned two levels and the UI drew grandchildren as siblings.
    # ``rel_path`` is traversal-validated in list_tree.
    rel = request.query_params.get("path", "")
    try:
        entries = fs_reader.list_tree(
            scope="agent",
            agent_id=agent_id,
            agent_root=base,
            rel_path=rel,
            max_depth=0,
            caller_did=_CALLER_DID,
        )
    except PathTraversalError as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )

    # H-018 hardening: private key material is never exportable, so it must
    # never appear in a listing either — filtering here (by literal on-disk
    # name; ``get_file_read`` below closes the symlink-disguise case at read
    # time via the resolved path) keeps a key file out of the tree the same
    # way ``.audit/`` and other hidden entries are already excluded by
    # ``fs_reader._walk``.
    entries = [e for e in entries if not _is_key_material(Path(e.path))]

    return JSONResponse(
        FilesTreeResponse(
            root=root_arg,
            entries=[
                FilesTreeEntry(path=e.path, type=e.type, size=e.size, mtime=e.mtime)
                for e in entries
            ],
        ).model_dump(mode="json")
    )


async def get_file_read(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    rel = request.query_params.get("path")
    if not rel:
        return JSONResponse(
            ErrorResponse(error="Missing path").model_dump(mode="json"),
            status_code=400,
        )

    root_arg, err = safe_choice(
        request.query_params.get("root", "workspace"),
        _VALID_ROOTS,
        error_label="Invalid root",
    )
    if err is not None:
        return err

    base = _resolve_root_path(agent_root, root_arg)

    # H-018 hardening: keys are non-exportable by construction (LLM07) — no
    # verb on this dashboard may render one's bytes. Resolve independently of
    # fs_reader (which has no such check) so a symlink DISGUISED under an
    # innocuous name (e.g. ``notes.md`` -> ``operator.key``) is still caught:
    # the check runs on the RESOLVED target's name, not the request string.
    # Best-effort: any resolution error here just falls through to
    # fs_reader's own (audited) path-traversal handling below.
    try:
        resolved = (base / rel).resolve()
    except OSError:
        resolved = None
    if resolved is not None and _is_key_material(resolved):
        emit_mutation_audit(
            request,
            target=f"{root_arg}:{rel}",
            operation="file_read",
            outcome="denied",
            detail="key_material_blocked",
        )
        return JSONResponse(
            ErrorResponse(
                error=f"'{rel}' is private key material and cannot be viewed from the dashboard."
            ).model_dump(mode="json"),
            status_code=403,
        )

    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=base,
            rel_path=rel,
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse(
            ErrorResponse(error="File not found").model_dump(mode="json"),
            status_code=404,
        )
    except PathTraversalError as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )
    except FileTooLargeError as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=413,
        )

    return JSONResponse(
        FileReadResponse(
            path=content.path,
            size=content.size,
            mtime=content.mtime,
            content=content.content,
            content_type=content.content_type,
            mime=content.mime,
        ).model_dump(mode="json")
    )
