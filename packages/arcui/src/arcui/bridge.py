"""UIBridgeSink — bridges arctrust.AuditEvent into arcui UIEvent pipeline.

Implements the arctrust.AuditSink protocol so that a single call to
``arctrust.audit.emit(event, UIBridgeSink(...))`` simultaneously writes to the
canonical audit log AND pushes a UIEvent to all subscribed browser clients.

No double-emit: the bridge is the single emission point. Callers should NOT
emit UIEvents separately when using this sink.

Field mapping
-------------
AuditEvent field     → UIEvent field
─────────────────────────────────────
actor_did            → agent_id
action (dots→_)      → event_type
tier                 → data["tier"]
outcome              → data["outcome"]
target               → data["target"]
request_id           → data["request_id"]
classification       → data["classification"]
payload_hash         → data["payload_hash"]
ts                   → timestamp
extra                → data["extra"] (merged into data dict)

The layer is configurable (default "agent") because different callers may
want to route these events to different dashboard layers.

Security (ASI07 / AU-2 / AU-9):
- Bridge does not drop any AuditEvent fields — full content flows to UI.
- No secrets are extracted or re-emitted; AuditEvent already excludes raw payloads.
- Sequence numbers are monotonically increasing per bridge instance.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from arctrust.audit import AuditEvent

from arcui.types import UIEvent

if TYPE_CHECKING:
    from arcui.event_buffer import EventBuffer

# UIEvent.event_type must match ^[a-z_]+$
# Dots and hyphens in AuditEvent.action are replaced with underscores.
_ACTION_CHARS_REPLACE = str.maketrans({".": "_", "-": "_"})


def _sanitize_event_type(action: str) -> str:
    """Convert AuditEvent.action to a UIEvent-compatible event_type.

    UIEvent requires ^[a-z_]+$. We lowercase and replace dots/hyphens.
    Unknown characters are replaced with underscore to be safe.
    """
    cleaned = action.lower().translate(_ACTION_CHARS_REPLACE)
    # Keep only [a-z_] characters
    return "".join(c if c in "abcdefghijklmnopqrstuvwxyz_" else "_" for c in cleaned)


class UIBridgeSink:
    """AuditSink that converts AuditEvent → UIEvent and pushes to EventBuffer.

    Satisfies the arctrust.AuditSink protocol (structural subtyping — no
    inheritance required). Type checkers verify via ``isinstance(sink, AuditSink)``.

    Args:
        event_buffer: The arcui EventBuffer to push converted events into.
        layer: UI layer for emitted events. Default is "agent" (most audit
            events come from agent-level policy/tool decisions).
        source_id: Identifies this bridge in UIEvent.source_id. Defaults to
            "arctrust.audit.bridge".
        agent_name: Human-readable name for the emitting agent in UIEvent.
            Defaults to empty string; callers can set this to improve UX.
    """

    def __init__(
        self,
        event_buffer: EventBuffer,
        layer: Literal["llm", "run", "agent", "team"] = "agent",
        source_id: str = "arctrust.audit.bridge",
        agent_name: str = "",
    ) -> None:
        self._buffer = event_buffer
        self.layer = layer
        self._source_id = source_id
        self._agent_name = agent_name
        self._seq = 0
        self._lock = threading.Lock()

    def _next_seq(self) -> int:
        with self._lock:
            seq = self._seq
            self._seq += 1
            return seq

    def write(self, event: AuditEvent) -> None:
        """Convert AuditEvent to UIEvent and push to EventBuffer.

        Called by arctrust.audit.emit(). Never raises — push errors are
        swallowed per AU-5 (audit must never interrupt the audited path).
        """
        try:
            self._push(event)
        except Exception:
            # Never let bridge errors reach the caller (AU-5 equivalent for UI path).
            # The canonical arctrust sink (e.g. JsonlSink) still gets the event.
            import logging

            logging.getLogger(__name__).warning(
                "UIBridgeSink failed to push event %r — swallowing",
                event.action,
                exc_info=True,
            )

    def _push(self, event: AuditEvent) -> None:
        """Internal: convert and push, raising on failure (wrapped by write())."""
        event_type = _sanitize_event_type(event.action)
        timestamp = event.ts or datetime.now(UTC).isoformat()

        # Build data payload — all AuditEvent fields preserved
        data: dict[str, object] = {
            "action": event.action,
            "target": event.target,
            "outcome": event.outcome,
        }
        if event.tier is not None:
            data["tier"] = event.tier
        if event.request_id is not None:
            data["request_id"] = event.request_id
        if event.classification is not None:
            data["classification"] = event.classification
        if event.payload_hash is not None:
            data["payload_hash"] = event.payload_hash
        if event.extra:
            data["extra"] = dict(event.extra)

        ui_event = UIEvent(
            layer=self.layer,
            event_type=event_type,
            agent_id=event.actor_did,
            agent_name=self._agent_name,
            source_id=self._source_id,
            timestamp=timestamp,
            data=data,
            sequence=self._next_seq(),
        )
        self._buffer.push(ui_event)
