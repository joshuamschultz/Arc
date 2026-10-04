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

Item 13 adds live status (``GET .../voice`` carries ``live``: the gateway adapter, the
mic client and the speech engine, each with a heartbeat and a plain reason when down),
``POST .../voice/listening`` (persist + apply live, no restart), ``POST .../voice/wake``
(typed wake word, same) and ``POST .../voice/test`` (30 s transcript echo for "say it
now"). All three POSTs are operator-gated and audited.
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


def _voice_adapter(request: Request, agent_did: str) -> Any:
    """The live voice adapter this process hosts for ``agent_did``, or ``None``.

    Found by the adapter's ``name`` on the embedded gateway arcui already holds;
    no arcgateway internal import. A gateway running in another process has no
    adapter here, so the card then says so instead of guessing.
    """
    gateway = getattr(request.app.state, "embedded_gateway", None)
    for adapter in getattr(gateway, "adapters", ()) or ():
        if (
            getattr(adapter, "name", None) == "voice"
            and getattr(adapter, "agent_did", "") == agent_did
        ):
            return adapter
    return None


def _offline_live(voice: dict[str, Any]) -> dict[str, Any]:
    """The honest live block when this process hosts no voice adapter."""
    wake = voice.get("wake", {}) if isinstance(voice.get("wake"), dict) else {}
    words = wake.get("words") or [str(voice.get("chat_id", "voice")).lower()]
    reason = "The gateway is not running voice in this process. Restart the gateway."
    waiting = "Waiting for the voice adapter."
    return {
        "listening": bool(voice.get("listening", True)),
        "state": "offline",
        "client_connected": False,
        "reason": reason,
        "wake_words": words,
        "wake_mode": wake.get("mode", "stt"),
        "adapter": {"up": False, "heartbeat_at": None, "reason": reason},
        "client": {"up": False, "heartbeat_at": None, "reason": waiting},
        "engine": {"up": False, "heartbeat_at": None, "reason": waiting},
    }


def _read_voice_table() -> dict[str, Any]:
    from arctrust.paths import config_file

    cfg = config_file("gateway.toml")
    if not cfg.is_file():
        return {}
    voice = tomllib.loads(cfg.read_text(encoding="utf-8")).get("platforms", {}).get("voice", {})
    return voice if isinstance(voice, dict) else {}


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
        "listening": bool(voice.get("listening", True)),
        "wake": voice.get("wake", {}) if isinstance(voice.get("wake"), dict) else {},
    }


async def get_voice_status(request: Request) -> JSONResponse:
    """GET the voice-channel status: configuration plus live state of the three parts."""
    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None or not (agent_root / "arcagent.toml").is_file():
        return _error("agent not found.", 404)
    agent_did = _agent_did(agent_root)
    status = _voice_status(agent_did)
    if status.get("enabled") and status.get("bound_to_this_agent"):
        adapter = _voice_adapter(request, agent_did)
        status["live"] = (
            adapter.status().model_dump(mode="json")
            if adapter is not None
            else _offline_live(_read_voice_table())
        )
    return JSONResponse(status)


def _denied(request: Request, agent_id: str, operation: str) -> JSONResponse:
    emit_mutation_audit(request, target=f"voice:{agent_id}", operation=operation, outcome="denied")
    return _error(f"{operation.replace('_', ' ')} requires operator mode.", 403)


def _bound_voice(request: Request) -> tuple[Path, str] | JSONResponse:
    """Resolve the agent and confirm voice is connected to it, or an error reply."""
    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None or not (agent_root / "arcagent.toml").is_file():
        return _error("agent not found.", 404)
    agent_did = _agent_did(agent_root)
    voice = _read_voice_table()
    if not voice.get("enabled") or voice.get("agent_did") != agent_did:
        return _error("voice is not connected to this agent. Connect voice first.", 409)
    return agent_root, agent_did


def _actor(request: Request) -> str:
    return f"operator:{getattr(request.state, 'session_id', None) or 'unknown'}"


async def post_voice_listening(request: Request) -> JSONResponse:
    """Turn listening ON/OFF: persisted to gateway.toml and applied live, no restart."""
    if not _is_operator(request):
        return _denied(request, request.path_params["id"], "voice_listening")
    bound = _bound_voice(request)
    if isinstance(bound, JSONResponse):
        return bound
    agent_root, agent_did = bound
    on = (await _json_body(request)).get("on")
    if not isinstance(on, bool):
        return _error("'on' must be true or false.", 400)

    from arcgateway.connect import set_voice_listening
    from arctrust.paths import config_file

    set_voice_listening(gateway_config=config_file("gateway.toml"), on=on)
    adapter = _voice_adapter(request, agent_did)
    clients = await adapter.set_listening(on, actor_did=_actor(request)) if adapter else 0
    emit_mutation_audit(
        request,
        target=f"voice:{agent_root.name}",
        operation="voice_listening",
        outcome="allow",
        detail="on" if on else "off",
    )
    return JSONResponse(
        {"listening": on, "applied_live": adapter is not None, "clients_notified": clients}
    )


async def post_voice_wake(request: Request) -> JSONResponse:
    """Set the typed wake word(s): validated, persisted, pushed to the mic box live."""
    if not _is_operator(request):
        return _denied(request, request.path_params["id"], "voice_wake")
    bound = _bound_voice(request)
    if isinstance(bound, JSONResponse):
        return bound
    agent_root, agent_did = bound
    body = await _json_body(request)
    words = body.get("words")
    if not isinstance(words, list) or not words or not all(isinstance(w, str) for w in words):
        return _error("'words' must be a list of 1 to 3 words.", 400)

    from arcgateway.connect import set_voice_wake
    from arctrust.paths import config_file

    mode, match = body.get("mode"), body.get("match")
    try:
        wake = set_voice_wake(
            gateway_config=config_file("gateway.toml"),
            words=words,
            mode=mode if isinstance(mode, str) else None,
            match=match if isinstance(match, str) else None,
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    adapter = _voice_adapter(request, agent_did)
    clients = await adapter.set_wake(wake, actor_did=_actor(request)) if adapter else 0
    emit_mutation_audit(
        request,
        target=f"voice:{agent_root.name}",
        operation="voice_wake",
        outcome="allow",
        detail=",".join(wake.words),
    )
    return JSONResponse(
        {
            "wake": wake.model_dump(),
            "applied_live": adapter is not None,
            "clients_notified": clients,
            "note": (
                "Custom words are matched on a local transcript of what the mic box "
                "hears. They are not a trained wake-word model, so they cost a little "
                "CPU while people talk and a similar-sounding word can wake it."
            ),
        }
    )


async def post_voice_test(request: Request) -> JSONResponse:
    """Open a 30 s window where the mic box shows what it heard ("say it now")."""
    if not _is_operator(request):
        return _denied(request, request.path_params["id"], "voice_test")
    bound = _bound_voice(request)
    if isinstance(bound, JSONResponse):
        return bound
    agent_root, agent_did = bound
    adapter = _voice_adapter(request, agent_did)
    if adapter is None:
        return _error("the gateway is not running voice in this process.", 409)
    clients = await adapter.start_test(actor_did=_actor(request))
    emit_mutation_audit(
        request, target=f"voice:{agent_root.name}", operation="voice_test", outcome="allow"
    )
    return JSONResponse({"seconds": 30, "clients_notified": clients})


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

    wake_words = body.get("wake_words")
    try:
        result = connect_voice(
            wake_words=wake_words if isinstance(wake_words, list) else None,
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
            "message": (
                "Voice is set up. Restart Arc, then start the Arc mic app "
                "on the computer with the microphone."
            ),
        }
    )
