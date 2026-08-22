"""Transport-safe stream events exposed by the ArcAgent public facade."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class DeliveryStreamEvent:
    """Base event emitted to a transport for one interactive agent run."""

    run_id: str
    sequence: int


@dataclass(frozen=True)
class DeliveryTextEvent(DeliveryStreamEvent):
    """A safe incremental text fragment."""

    text: str


@dataclass(frozen=True)
class DeliveryToolEvent(DeliveryStreamEvent):
    """A public tool-start notification without arguments or results."""

    name: str


@dataclass(frozen=True)
class DeliveryTerminalEvent(DeliveryStreamEvent):
    """The one terminal outcome for a transport stream."""

    status: Literal["completed", "cancelled", "failed"]


@runtime_checkable
class DeliveryStreamSource(Protocol):
    """Public ArcAgent facade consumed by transport executors."""

    def stream_delivered_message(
        self,
        *,
        caller_did: str,
        message: str,
        session_key: str,
        reply_target: str | None = None,
        reply_label: str | None = None,
        parts: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[DeliveryStreamEvent]: ...
