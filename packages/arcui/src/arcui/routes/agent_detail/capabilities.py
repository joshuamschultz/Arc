"""`/api/agents/{id}/capabilities` — the faithful capability mirror (COMP-008).

Replaces arcui's hand-rolled skill/tool globbing (REQ-096): this route runs
arcagent's capability inventory seam at the agent's real trust posture and
returns every skill / capability tool across the loader's scan roots with its
verbatim load verdict (``source_root`` + ``status``), plus — for a live,
chat-loaded agent — its runtime-registered tool list (REQ-093/094/095).

arcagent is imported lazily inside the handler, not at module load: the
capability-inventory seam is the ONE approved arcagent import from arcui
(SPEC-023 §2.2), and importing it at call time keeps arcui from taking a hard
package dependency that would let the surface widen unnoticed. The embedded
gateway already runs ArcAgent in-process, so the package is always importable
at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import _agent_did, _agent_root, logger
from arcui.schemas import ErrorResponse


async def _agent_capability_rows(
    agent_root: Path, live_agent: Any, kind: str
) -> list[dict[str, Any]]:
    """Capability-kind (``"skill"`` or ``"tool"``) inventory rows for one agent.

    Shared by :func:`agent_skill_rows` and :func:`agent_tool_rows` — same
    fleet-resilience contract: one unloadable agent config contributes no rows,
    never crashes the aggregation. The strict per-agent route (``get_capabilities``)
    surfaces errors instead.
    """
    config_path = agent_root / "arcagent.toml"
    if not config_path.is_file():
        return []
    try:
        inventory = await arcagent.collect_agent_capability_inventory(
            config_path, live_agent=live_agent
        )
    except Exception:  # reason: fleet resilience — see docstring
        logger.warning(
            "%s inventory failed for %s; contributing none", kind, agent_root, exc_info=True
        )
        return []
    rows = [item.model_dump(mode="json") for item in inventory.items if item.kind == kind]
    if kind == "tool" and inventory.runtime:
        _append_runtime_only_tools(rows, inventory.runtime_tools)
    return rows


def _append_runtime_only_tools(rows: list[dict[str, Any]], runtime_tools: list[Any]) -> None:
    """Surface a live-registered tool that has no static scan root of its own.

    A connector's per-verb tools register into the live registry through
    :class:`~arcagent.extension.bridge.CapabilityBridge` under an
    ``extension:<name>`` scan root — there is no ``.py`` file anywhere for the
    static inventory scan to find, so an attached extension's tools would
    otherwise never appear in ``kind == "tool"`` items at all (H-031). Each
    runtime tool carries its own true scan-root in ``source`` (H-030's
    ``RuntimeToolItem.source``); a tool already found by the static scan is
    left as-is (fuller ``source_path``/``version`` there) rather than
    overwritten by this best-effort runtime row.
    """
    seen = {row["name"] for row in rows}
    for tool in runtime_tools:
        name = getattr(tool, "name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append(
            {
                "kind": "tool",
                "name": name,
                "version": "",
                "description": getattr(tool, "description", "") or "",
                "source_root": getattr(tool, "source", "") or "",
                "source_path": "",
                "status": "loaded",
                "status_detail": (
                    "runtime-registered; no static scan root (e.g. an attached extension)"
                ),
            }
        )


async def agent_skill_rows(agent_root: Path, live_agent: Any = None) -> list[dict[str, Any]]:
    """Skill-kind inventory rows for one agent, via the arcagent seam.

    The single skill-discovery path for both the per-agent Skills tab and the
    fleet Tools & Skills page — no globbing in arcui (REQ-096). Each row carries
    the loader's ``source_root`` + verbatim ``status``. Returns ``[]`` when the
    agent has no config on disk.
    """
    return await _agent_capability_rows(agent_root, live_agent, "skill")


async def agent_tool_rows(agent_root: Path, live_agent: Any = None) -> list[dict[str, Any]]:
    """Tool-kind inventory rows for one agent, via the arcagent seam.

    Used by the tool detail drawer (U6) to resolve a capability-authored tool's
    source file (``<agent>/capabilities/*.py`` or
    ``<agent>/workspace/capabilities/*.py``) when the plain tool-directory scan
    (``tools/``, ``extensions/``) doesn't find it — the loader's own
    ``source_path`` is authoritative there. Returns ``[]`` when the agent has no
    config on disk.
    """
    return await _agent_capability_rows(agent_root, live_agent, "tool")


def _live_agent(request: Request, agent_id: str) -> Any:
    """Return the in-process ArcAgent for ``agent_id`` if the embedded gateway
    has one cached, else None. The cache is keyed by agent DID."""
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    if cache is None:
        return None
    did = _agent_did(request, agent_id)
    if did is None:
        return None
    return cache.get(did)


async def get_capabilities(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )
    config_path = agent_root / "arcagent.toml"
    if not config_path.is_file():
        return JSONResponse(
            ErrorResponse(error="Agent config not found").model_dump(mode="json"),
            status_code=404,
        )

    try:
        inventory = await arcagent.collect_agent_capability_inventory(
            config_path, live_agent=_live_agent(request, agent_id)
        )
    except Exception as exc:  # reason: surface failure explicitly, never fail-open empty
        logger.exception("capability inventory failed for %s", agent_id)
        return JSONResponse(
            ErrorResponse(error=f"Capability inventory failed: {exc}").model_dump(mode="json"),
            status_code=500,
        )
    return JSONResponse(inventory.model_dump(mode="json"))
