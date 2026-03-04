"""Agent WebSocket route — /api/agent/connect.

Agents authenticate with a first-message token + registration payload,
then enter a bidirectional loop: agent sends UIEvents, server sends
ControlMessages. Server stamps agent_id on all received events.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import pydantic
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from arcui.aggregator import RollingAggregator
from arcui.types import AgentRegistration, ControlResponse, UIEvent
from arcui.ws_helpers import (
    CLOSE_CAPACITY_FULL,
    MAX_WS_MESSAGE_SIZE,
    authenticate_ws,
    heartbeat_loop,
    run_ws_tasks,
)

logger = logging.getLogger(__name__)


async def agent_ws_endpoint(ws: WebSocket) -> None:
    """Agent WebSocket endpoint with first-message auth and event streaming."""
    await ws.accept()

    auth_config = ws.app.state.auth_config
    registry = ws.app.state.agent_registry
    event_buffer = ws.app.state.event_buffer
    aggregator = ws.app.state.aggregator
    pending_controls = ws.app.state.pending_controls
    audit = getattr(ws.app.state, "audit", None)

    # --- Auth phase ---
    role, msg = await authenticate_ws(ws, auth_config, require_role="agent")
    if role is None:
        if audit:
            audit.audit_event("auth.failure", {"transport": "agent_ws"})
        return

    # --- Registration ---
    if registry.is_full():
        await ws.send_json({"error": "Server at capacity"})
        await ws.close(code=CLOSE_CAPACITY_FULL)
        if audit:
            audit.audit_event("capacity.rejected", {"transport": "agent_ws"})
        return

    agent_id = uuid.uuid4().hex
    reg_data = msg.get("registration", {})
    registration = AgentRegistration(
        agent_id=agent_id,
        agent_name=reg_data.get("agent_name", "unknown"),
        model=reg_data.get("model", "unknown"),
        provider=reg_data.get("provider", "unknown"),
        team=reg_data.get("team"),
        tools=reg_data.get("tools", []),
        modules=reg_data.get("modules", []),
        workspace=reg_data.get("workspace"),
        meta=reg_data.get("meta", {}),
        connected_at=datetime.now(UTC).isoformat(),
    )

    entry = registry.register(agent_id, ws, registration)

    # Create per-agent aggregator for FR-10 drill-down stats
    entry.aggregator = RollingAggregator()

    await ws.send_json({"type": "auth_ok", "agent_id": agent_id})

    if audit:
        audit.audit_event(
            "agent.connect",
            {"agent_id": agent_id, "agent_name": registration.agent_name},
        )
        audit.audit_event("auth.success", {"transport": "agent_ws", "agent_id": agent_id})

    # --- Bidirectional loop ---
    async def _receive() -> None:
        """Receive events and control responses from agent."""
        try:
            while True:
                raw_msg = await ws.receive_text()

                # DoS prevention: reject oversized messages
                if len(raw_msg) > MAX_WS_MESSAGE_SIZE:
                    logger.warning(
                        "Oversized message from agent %s (%d bytes), skipping",
                        agent_id,
                        len(raw_msg),
                    )
                    continue

                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from agent %s", agent_id)
                    continue

                msg_type = data.get("type", "")

                if msg_type == "event":
                    try:
                        payload = data.get("payload", {})
                        payload["agent_id"] = agent_id
                        event = UIEvent(**payload)
                    except pydantic.ValidationError:
                        logger.warning(
                            "Malformed UIEvent from agent %s, skipping", agent_id
                        )
                        continue

                    entry.registration.last_event_at = datetime.now(UTC).isoformat()
                    entry.registration.sequence = event.sequence

                    event_buffer.push(event)

                    if entry.aggregator is not None:
                        entry.aggregator.ingest(payload)
                    if aggregator is not None:
                        aggregator.ingest(payload)

                elif msg_type == "control_response":
                    try:
                        payload = data.get("payload", {})
                        resp = ControlResponse(**payload)
                    except pydantic.ValidationError:
                        logger.warning(
                            "Malformed ControlResponse from agent %s, skipping",
                            agent_id,
                        )
                        continue

                    future = pending_controls.get(resp.request_id)
                    if future is not None and not future.done():
                        future.set_result(resp)

        except (WebSocketDisconnect, RuntimeError):
            pass

    try:
        await run_ws_tasks(heartbeat_loop(ws), _receive())
    finally:
        registry.unregister(agent_id)
        # Only error futures targeting THIS disconnecting agent
        for req_id, (target_id, future) in list(pending_controls.items()):
            if target_id == agent_id and not future.done():
                future.set_exception(
                    TimeoutError(f"Agent {agent_id} disconnected")
                )
                pending_controls.pop(req_id, None)

        if audit:
            audit.audit_event(
                "agent.disconnect",
                {"agent_id": agent_id, "agent_name": registration.agent_name},
            )


routes = [
    WebSocketRoute("/api/agent/connect", agent_ws_endpoint),
]
