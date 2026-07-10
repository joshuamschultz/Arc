"""Team Chat routes — observe inter-agent messaging via arcteam.

Endpoint surface:

* ``GET /api/team/channels`` — list channels (id, members, created).
* ``GET /api/team/channels/{channel_name}/messages`` — chronological
  message stream (oldest → newest), paginated by sequence.

Both routes are read-only; sending messages remains the responsibility
of agents themselves (via the ``messaging_send`` tool registered by
``arcagent.modules.messaging``). The handlers read through
``request.app.state.messaging_service`` so tests can inject a fake
service without standing up a live NATS backend.

NIST SI-10: channel names go through ``_VALID_CHANNEL_NAME_RE`` before
hitting the backend; arcteam's URI validator only matches the broader
agent/user/channel/role grammar, so we re-check here to keep
filesystem paths well-bounded.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)

# Channel names share arcteam's URI grammar — alphanumeric + ``_-`` —
# but constrained to a sane length here so the backend never sees a
# pathological key.
_VALID_CHANNEL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

_DEFAULT_MSG_LIMIT = 100
_MAX_MSG_LIMIT = 500


def _parse_limit(raw: str | None, default: int, ceiling: int) -> int:
    """Clamp ``raw`` to ``[1, ceiling]``; fall back to ``default``."""
    if raw is None:
        return default
    try:
        return max(1, min(ceiling, int(raw)))
    except (ValueError, TypeError):
        return default


def _parse_after_seq(raw: str | None) -> int:
    """Return a non-negative integer for the ``after_seq`` query param."""
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return 0


def _service_or_none(request: Request) -> Any | None:
    """Return the configured arcteam MessagingService, or None.

    None means the deployment's team messaging is not wired (no team root,
    a down broker, or a missing audit authority). The routes surface that as
    an explicit unavailable error — never a fabricated empty list, which would
    hide real channels that ``arc team channels`` lists (REQ-090).
    """
    return getattr(request.app.state, "messaging_service", None)


def _unavailable() -> JSONResponse:
    """503 payload distinguishing 'messaging is down' from 'no channels yet'."""
    return JSONResponse(
        ErrorResponse(error="team_messaging_unavailable").model_dump(mode="json"),
        status_code=503,
    )


async def list_channels(request: Request) -> JSONResponse:
    """GET /api/team/channels — channels visible to the operator."""
    svc = _service_or_none(request)
    if svc is None:
        return _unavailable()

    try:
        channels = await svc.list_channels()
    except Exception:  # reason: surface failure — never fabricate an empty list
        logger.exception("team_chat: list_channels failed")
        return _unavailable()

    return JSONResponse({"channels": [ch.model_dump(mode="json") for ch in channels]})


async def channel_messages(request: Request) -> JSONResponse:
    """GET /api/team/channels/{channel_name}/messages — chronological."""
    channel_name = request.path_params["channel_name"]
    if not _VALID_CHANNEL_NAME_RE.match(channel_name):
        return JSONResponse(
            ErrorResponse(error="Invalid channel name").model_dump(mode="json"),
            status_code=400,
        )

    svc = _service_or_none(request)
    if svc is None:
        return _unavailable()

    limit = _parse_limit(
        request.query_params.get("limit"),
        _DEFAULT_MSG_LIMIT,
        _MAX_MSG_LIMIT,
    )
    after_seq = _parse_after_seq(request.query_params.get("after_seq"))

    try:
        messages = await svc.list_channel_messages(
            channel_name=channel_name,
            after_seq=after_seq,
            limit=limit,
        )
    except Exception:  # reason: surface failure — never fabricate an empty list
        logger.exception("team_chat: list_channel_messages failed for %s", channel_name)
        return _unavailable()

    # ``next_after_seq`` is set only when we filled the page; an empty or
    # short result means we hit the end of the stream so the SPA can stop
    # polling for more without a separate "is there more?" round-trip.
    next_after_seq: int | None = None
    if messages and len(messages) >= limit:
        next_after_seq = messages[-1].seq

    return JSONResponse(
        {
            "channel": channel_name,
            "messages": [m.model_dump(mode="json") for m in messages],
            "next_after_seq": next_after_seq,
        }
    )


routes = [
    Route("/api/team/channels", list_channels),
    Route("/api/team/channels/{channel_name}/messages", channel_messages),
]
