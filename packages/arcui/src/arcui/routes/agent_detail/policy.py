"""`/api/agents/{id}/policy[/bullets|/stats]` route handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcgateway import fs_reader, policy_parser
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import _CALLER_DID, _agent_root
from arcui.schemas import (
    ErrorResponse,
    PolicyBulletsResponse,
    PolicyResponse,
    PolicyStatsResponse,
)


async def get_policy(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    workspace = agent_root / "workspace"
    raw, err = _read_policy(agent_id, workspace)
    if err is not None:
        return err
    bullets = [_bullet_to_dict(b) for b in policy_parser.parse_bullets(raw)]
    return JSONResponse(PolicyResponse(raw=raw, bullets=bullets).model_dump(mode="json"))


async def get_policy_bullets(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    workspace = agent_root / "workspace"
    raw, err = _read_policy(agent_id, workspace)
    if err is not None:
        return err
    bullets = [_bullet_to_dict(b) for b in policy_parser.parse_bullets(raw)]
    return JSONResponse(PolicyBulletsResponse(bullets=bullets).model_dump(mode="json"))


async def get_policy_stats(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    workspace = agent_root / "workspace"
    raw, err = _read_policy(agent_id, workspace)
    if err is not None:
        return err
    bullets = policy_parser.parse_bullets(raw)
    active = [b for b in bullets if not b.retired]
    retired = [b for b in bullets if b.retired]
    avg = sum(b.score for b in active) / len(active) if active else 0.0
    return JSONResponse(
        PolicyStatsResponse(
            total=len(bullets),
            active=len(active),
            retired=len(retired),
            avg_score=avg,
        ).model_dump(mode="json")
    )


def _read_policy(agent_id: str, workspace: Path) -> tuple[str, JSONResponse | None]:
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path="policy.md",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return "", None  # missing policy is treated as empty, not an error
    except (PathTraversalError, FileTooLargeError) as exc:
        return "", JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )
    return content.content, None


def _bullet_to_dict(b: policy_parser.PolicyBullet) -> dict[str, Any]:
    return {
        "id": b.id,
        "text": b.text,
        "score": b.score,
        "uses": b.uses,
        "reviewed": b.reviewed.isoformat() if b.reviewed else None,
        "created": b.created.isoformat() if b.created else None,
        "source": b.source,
        "retired": b.retired,
    }
