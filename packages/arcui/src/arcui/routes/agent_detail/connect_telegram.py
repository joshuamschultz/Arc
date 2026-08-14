"""``POST /api/agents/{id}/connect-telegram`` — operator-gated Telegram connect.

The arcui surface behind the "Connect Telegram" panel: an operator submits a bot token
and their Telegram user-ID; this binds a per-agent Telegram bot by delegating to the
shared :func:`arcgateway.connect.connect_telegram` core (the same one ``arc gateway
connect-telegram`` uses). The token is written ONLY to the env file (0600) by the core —
it is never logged, never audited (only the block name is), and never persisted by arcui.

Why a route + settings-panel form and not a chat ``/slash`` command: a token typed into
agent chat would be stored in the session transcript and could reach the LLM (LLM07). A
form POSTs the secret straight here, out of band.

The gateway reads the token from its process env, which is populated at start, so the new
bot goes live on the next gateway restart — the response flags ``restart_required``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_root
from arcui.schemas import ErrorResponse


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _agent_did(agent_root: Path) -> str:
    cfg = agent_root / "arcagent.toml"
    did = tomllib.loads(cfg.read_text(encoding="utf-8")).get("identity", {}).get("did", "")
    return str(did)


async def connect_telegram_route(request: Request) -> JSONResponse:
    """Bind a Telegram bot to the agent from an operator-submitted token + user ID."""
    if not _is_operator(request):
        return _error("connecting Telegram requires operator mode.", 403)

    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None or not (agent_root / "arcagent.toml").is_file():
        return _error("agent not found.", 404)

    body = await _json_body(request)
    if body is None:
        return _error("invalid JSON body.", 400)
    token = str(body.get("token") or "")
    user_id = body.get("user_id")
    if not isinstance(user_id, int):
        return _error("user_id must be your Telegram numeric user ID.", 400)

    from arcgateway.connect import connect_telegram
    from arctrust.paths import config_file, env_file

    try:
        result = connect_telegram(
            agent_slug=agent_root.name,
            agent_did=_agent_did(agent_root),
            token=token,
            user_id=user_id,
            gateway_config=config_file("gateway.toml"),
            env_file=env_file(),
        )
    except ValueError as exc:
        # The message never contains the token (validation is format-only).
        return _error(str(exc), 400)

    # Audit records the BLOCK name only — never the token.
    emit_mutation_audit(
        request,
        target=f"telegram:{agent_root.name}",
        operation="connect_telegram",
        outcome="allow",
        detail=result["block"],
    )
    return JSONResponse(
        {
            "connected": True,
            "block": result["block"],
            "token_env": result["token_env"],
            "restart_required": True,
            "message": "Bot wired. Restart the gateway to bring it live, then message it.",
        }
    )


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
    except Exception:  # reason: malformed body — surface a 400, never a 500
        return None
    return data if isinstance(data, dict) else None
