"""Knowledge page API — ``GET /api/knowledge/{agent_id}``.

Returns the structured payload consumed by the Knowledge UI page:
context budget, memory entries (with previews), workspace tree, and
optional code-graph stats. Falls back to ``graph.available: false``
when the code-review-graph MCP server is unreachable so the dashboard
keeps rendering.

Per SDD §3.5 the route is a thin assembler — every datum comes from
``arcgateway.fs_reader`` (file system) or the optional MCP query.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcgateway import fs_reader
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Memory previews kept short — the page renders them as a hover tooltip.
_PREVIEW_BYTES = 200
# Workspace tree caps from SDD §3.5.
_TREE_DEPTH = 1
_TREE_ENTRY_CAP = 200


def _resolve_agent(ws_app_state: Any, agent_id: str) -> Any:
    """Look up a roster entry by id (or name) — the same matching the WS route uses."""
    roster_provider = getattr(ws_app_state, "roster_provider", None)
    if roster_provider is None:
        return None
    for entry in roster_provider():
        if getattr(entry, "agent_id", None) == agent_id:
            return entry
        if getattr(entry, "name", None) == agent_id:
            return entry
    return None


def _agent_root(team_root: Path | None, agent_id: str) -> Path | None:
    """Path to ``team_root/<name>_agent/`` for fs_reader scoping."""
    if team_root is None:
        return None
    candidate = team_root / f"{agent_id}_agent"
    if candidate.is_dir():
        return candidate
    return None


def _file_preview(path: Path, n: int = _PREVIEW_BYTES) -> str:
    """First n characters of a text file — empty string on read error."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(n)
    except OSError:
        return ""


def _memory_entries(agent_root: Path) -> tuple[list[dict[str, Any]], int]:
    """Build the ``memory.entries`` array and total byte count."""
    memory_dir = agent_root / "memory"
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    if not memory_dir.is_dir():
        return entries, total_bytes
    for path in sorted(memory_dir.iterdir()):
        if not path.is_file():
            continue
        st = path.stat()
        total_bytes += st.st_size
        entries.append(
            {
                "filename": path.name,
                "type": "text",
                "size_bytes": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                "classification": "UNCLASS",
                "created_by": agent_root.name.removesuffix("_agent"),
                "preview": _file_preview(path),
            }
        )
    return entries, total_bytes


def _workspace_tree(agent_id: str, agent_root: Path) -> tuple[list[dict[str, Any]], int, bool]:
    """Top-level workspace tree (caps at 200 entries; deeper children deferred)."""
    workspace_dir = agent_root / "workspace"
    if not workspace_dir.is_dir():
        return [], 0, False
    entries = fs_reader.list_tree(
        scope="agent",
        agent_id=agent_id,
        agent_root=agent_root,
        rel_path="workspace",
        max_depth=_TREE_DEPTH,
        caller_did="did:arc:ui:knowledge",
    )
    tree: list[dict[str, Any]] = []
    total_files = 0
    for entry in entries[:_TREE_ENTRY_CAP]:
        node: dict[str, Any] = {
            "path": entry.path,
            "type": entry.type,
            "modified_at": datetime.fromtimestamp(entry.mtime, tz=UTC).isoformat(),
        }
        if entry.type == "file":
            node["size_bytes"] = entry.size
            total_files += 1
        else:
            node["children"] = None
            node["child_count"] = 0  # client requests deeper expansion separately
        tree.append(node)
    truncated = len(entries) > _TREE_ENTRY_CAP
    return tree, total_files, truncated


def _graph_stats() -> dict[str, Any]:
    """Code-graph stats with ``available: false`` fallback.

    The actual MCP call lives in the runtime — for v1 we declare unavailable
    so the dashboard still renders. SDD §3.5 step 5 — wired for production
    when the project's MCP client is plumbed through ``app.state``.
    """
    return {"available": False}


def _recent_memory_events(state: Any, agent_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Pull the most recent memory FileChangeEvents for this agent (best-effort)."""
    bridge = getattr(state, "file_change_bridge", None)
    if bridge is None:
        return []
    history = getattr(bridge, "recent_events", None)
    if not callable(history):
        return []
    try:
        events = history(agent_id, limit=limit)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for ev in events:
        out.append(
            {
                "timestamp": getattr(ev, "timestamp", ""),
                "event_type": getattr(ev, "event_type", ""),
                "filename": getattr(ev, "filename", ""),
                "size_bytes": getattr(ev, "size_bytes", 0),
                "by_agent": agent_id,
            }
        )
    return out


async def get_knowledge(request: Request) -> JSONResponse:
    """GET /api/knowledge/{agent_id}.

    Returns the agent's memory + workspace + graph snapshot per SDD §5.2.
    Returns 404 when the agent_id is not in the roster, 200 with
    ``graph.available: false`` when the code-graph MCP server is down.
    """
    agent_id = request.path_params["agent_id"]
    state = request.app.state
    agent = _resolve_agent(state, agent_id)
    if agent is None:
        return JSONResponse({"error": f"agent {agent_id!r} not found"}, status_code=404)

    team_root: Path | None = getattr(state, "team_root", None)
    agent_root = _agent_root(team_root, getattr(agent, "name", agent_id))

    memory_entries: list[dict[str, Any]] = []
    memory_total_bytes = 0
    workspace_tree: list[dict[str, Any]] = []
    workspace_total = 0
    workspace_truncated = False
    if agent_root is not None:
        memory_entries, memory_total_bytes = _memory_entries(agent_root)
        workspace_tree, workspace_total, workspace_truncated = _workspace_tree(
            agent_id, agent_root
        )

    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_did": getattr(agent, "did", ""),
        "context": {
            "model": getattr(agent, "model", "") or "",
            "input_tokens": 0,
            "memory_used_tokens": memory_total_bytes // 4,  # ~4 bytes/token coarse heuristic
            "memory_percent_of_window": 0.0,
            "truncation_policy": "recency",
            "last_truncation_at": None,
        },
        "memory": {
            "entries": memory_entries,
            "total_bytes": memory_total_bytes,
            "recent_events": _recent_memory_events(state, agent_id),
        },
        "workspace": {
            "tree": workspace_tree,
            "total_files": workspace_total,
            "truncated": workspace_truncated,
        },
        "graph": _graph_stats(),
    }
    return JSONResponse(payload)


routes = [
    Route("/api/knowledge/{agent_id}", get_knowledge),
]
