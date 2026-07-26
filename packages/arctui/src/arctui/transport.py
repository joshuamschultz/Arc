"""Chat transport seam — how the TUI drives a turn without owning an agent.

SPEC-058 Phase 3: arctui is a *viewpoint* onto a served arc agent, not an
agent owner. It never constructs an ``ArcAgent`` (that would grab a second
single-writer WORM lock — the collision ``arcgateway.fleet`` warns about).
Instead the app sends a user turn through a ``ChatTransport`` and renders the
``TurnEvent`` stream it yields back.

The only production transport is :class:`arctui.gateway_client.GatewayChatClient`
(a WebSocket client of the gateway's ``/ws/chat/{agent_id}`` route — the same
route the browser dashboard and every platform adapter reach the agent
through). Keeping the app coupled to this small Protocol rather than to a
concrete client means a future in-process or NATS transport drops in without
touching render code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# Web is a send-only transport, so the gateway accumulates a turn's tokens and
# delivers one final agent frame per turn (block-at-turn). "message" carries
# that reply; "error" carries a turn-level failure; "done" closes the turn.
TurnEventKind = Literal["message", "error", "done"]


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """One rendered chunk of an agent turn.

    Attributes:
        kind: "message" (agent reply text), "error" (turn failed), or "done"
            (turn terminator — ``text`` is empty).
        text: The reply or error text; empty for "done".
    """

    kind: TurnEventKind
    text: str = ""


@runtime_checkable
class ChatTransport(Protocol):
    """Contract for driving one agent turn from the TUI.

    Implementations attach to an already-served agent; they must never start
    one. ``send_turn`` is an async generator: it yields zero or more
    ``TurnEvent(kind="message"|"error")`` then exactly one
    ``TurnEvent(kind="done")`` as the final item.
    """

    def send_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Send *text* as a user turn and stream the agent's reply back."""
        ...

    async def aclose(self) -> None:
        """Release the underlying connection. Idempotent."""
        ...
