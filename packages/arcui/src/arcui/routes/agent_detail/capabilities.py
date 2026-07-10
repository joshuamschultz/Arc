"""`/api/agents/{id}/capabilities` — the faithful capability mirror (COMP-008).

Replaces arcui's hand-rolled skill/tool globbing (REQ-096): this route runs
arcagent's capability inventory seam at the agent's real trust posture and
returns every skill / capability tool across the loader's scan roots with its
verbatim load verdict (``source_root`` + ``status``), plus — for a live,
chat-loaded agent — its runtime-registered tool list (REQ-093/094/095).

arcagent is imported lazily inside the handler, not at module load: arcagent
already depends on arcui (the UIBridgeSink), so a top-level import would close a
dependency cycle. The embedded gateway already runs ArcAgent in-process, so the
package is always importable at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import _agent_did, _agent_root, logger
from arcui.schemas import ErrorResponse


async def agent_skill_rows(agent_root: Path, live_agent: Any = None) -> list[dict[str, Any]]:
    """Skill-kind inventory rows for one agent, via the arcagent seam.

    The single skill-discovery path for both the per-agent Skills tab and the
    fleet Tools & Skills page — no globbing in arcui (REQ-096). Each row carries
    the loader's ``source_root`` + verbatim ``status``. Returns ``[]`` when the
    agent has no config on disk.
    """
    config_path = agent_root / "arcagent.toml"
    if not config_path.is_file():
        return []
    from arcagent.capabilities.inventory import collect_agent_capability_inventory

    # Fleet-safe: one unloadable agent config contributes no rows, never crashes
    # the aggregation. The strict per-agent route below surfaces errors instead.
    try:
        inventory = await collect_agent_capability_inventory(config_path, live_agent=live_agent)
    except Exception:  # reason: fleet resilience — see comment above
        logger.warning(
            "skill inventory failed for %s; contributing none", agent_root, exc_info=True
        )
        return []
    return [item.model_dump(mode="json") for item in inventory.items if item.kind == "skill"]


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

    # Lazy import — see module docstring (arcagent -> arcui dependency cycle).
    from arcagent.capabilities.inventory import collect_agent_capability_inventory

    try:
        inventory = await collect_agent_capability_inventory(
            config_path, live_agent=_live_agent(request, agent_id)
        )
    except Exception as exc:  # reason: surface failure explicitly, never fail-open empty
        logger.exception("capability inventory failed for %s", agent_id)
        return JSONResponse(
            ErrorResponse(error=f"Capability inventory failed: {exc}").model_dump(mode="json"),
            status_code=500,
        )
    return JSONResponse(inventory.model_dump(mode="json"))
