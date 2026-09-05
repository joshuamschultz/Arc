"""``/api/agents/{id}/connect-voice`` (+ GET status) — operator-gated voice connect.

The arcui surface behind the "Voice (hey Olivia)" panel in the agent Connect tab.
An operator wires the desk voice channel to this agent by delegating to the shared
:func:`arcgateway.connect.connect_voice` core (the same one ``arc gateway connect-voice``
uses): it generates a pairing token (written ONLY to the env file, 0600), writes a
``[platforms.voice]`` block bound to the agent with a Kokoro cascade, and turns on
pairing. The generated token is returned once so the operator can run the desk client;
it is never audited (only the agent target is) or otherwise persisted by arcui.

The gateway reads config + token at start, so voice goes live on the next restart —
the response flags ``restart_required``.
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
    return str(tomllib.loads(cfg.read_text(encoding="utf-8")).get("identity", {}).get("did", ""))


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # reason: malformed body — surface a 400, never a 500
        return {}
    return data if isinstance(data, dict) else {}


def _voice_status(agent_did: str) -> dict[str, Any]:
    """Read the current [platforms.voice] state for this agent from gateway.toml."""
    from arctrust.paths import config_file

    cfg = config_file("gateway.toml")
    if not cfg.is_file():
        return {"enabled": False}
    voice = tomllib.loads(cfg.read_text(encoding="utf-8")).get("platforms", {}).get("voice", {})
    if not voice or not voice.get("enabled"):
        return {"enabled": False}
    bound = str(voice.get("agent_did", ""))
    engine = voice.get("engine", {})
    kokoro = engine.get("kokoro", {}) if isinstance(engine, dict) else {}
    return {
        "enabled": True,
        "bound_to_this_agent": bound == agent_did,
        "bound_agent_did": bound,
        "tts": engine.get("tts") if isinstance(engine, dict) else None,
        "stt": engine.get("stt") if isinstance(engine, dict) else None,
        "blend": kokoro.get("blend"),
        "speed": kokoro.get("speed"),
        "port": voice.get("port"),
    }


async def get_voice_status(request: Request) -> JSONResponse:
    """GET the voice-channel status for this agent (enabled, engine, blend, binding)."""
    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None or not (agent_root / "arcagent.toml").is_file():
        return _error("agent not found.", 404)
    return JSONResponse(_voice_status(_agent_did(agent_root)))


async def connect_voice_route(request: Request) -> JSONResponse:
    """Wire the desk voice channel to this agent (operator-gated)."""
    if not _is_operator(request):
        return _error("connecting voice requires operator mode.", 403)

    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None or not (agent_root / "arcagent.toml").is_file():
        return _error("agent not found.", 404)

    body = await _json_body(request)
    blend = str(body.get("blend") or "af_jessica:0.6,af_nicole:0.4")
    try:
        speed = float(body.get("speed") or 1.12)
    except (TypeError, ValueError):
        return _error("speed must be a number (0.8-1.3).", 400)

    from arcgateway.connect import connect_voice
    from arctrust.paths import config_file, env_file

    try:
        result = connect_voice(
            agent_did=_agent_did(agent_root),
            gateway_config=config_file("gateway.toml"),
            env_file=env_file(),
            blend=blend,
            speed=speed,
        )
    except ValueError as exc:
        return _error(str(exc), 400)

    emit_mutation_audit(
        request,
        target=f"voice:{agent_root.name}",
        operation="connect_voice",
        outcome="allow",
        detail=result["agent_did"],
    )
    return JSONResponse(
        {
            "connected": True,
            "token": result["token"],  # shown once so the operator can run the client
            "token_env": result["token_env"],
            "restart_required": True,
            "message": "Voice wired. Restart the gateway, pull the models, then run arc-voice.",
        }
    )
