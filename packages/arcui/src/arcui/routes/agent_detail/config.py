"""`/api/agents/{id}/config` + `/files/tree` + `/files/read` route handlers.

The config endpoint exposes the whitelisted TOML config (no secrets);
the file-tree and file-read endpoints expose the agent's file tree and
individual file content through ``arcgateway.fs_reader`` (single audited
chokepoint per SPEC-022).
"""

from __future__ import annotations

import tomllib
from typing import Any

from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.query_validators import safe_choice
from arcui.routes.agent_detail._common import (
    _CALLER_DID,
    _CONFIG_WHITELIST,
    _VALID_ROOTS,
    _agent_root,
    _resolve_root_path,
)
from arcui.schemas import (
    ConfigResponse,
    ErrorResponse,
    FileReadResponse,
    FilesTreeEntry,
    FilesTreeResponse,
)


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
            config=_whitelist_config(parsed),
            raw=content.content,
            mtime=content.mtime,
        ).model_dump(mode="json")
    )


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
