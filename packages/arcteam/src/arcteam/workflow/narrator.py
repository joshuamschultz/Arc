"""RunNarrator — the channel carries the story (SPEC-061 COMP-010, REQ-244).

Tasks carry the work, DMs carry the signals, the channel carries the story.
This module is only the third of those. Every message it posts is
narration-class: addressed to a channel, never to an agent; ``INFO``, never a
task or request; no mentions, because a mention fans out to an inbox and wakes
an agent. Nothing about a run's progress may depend on any of it arriving,
which is why a delivery failure is swallowed here rather than raised (D-538).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from arcteam.types import Message, MsgType, Priority, parse_uri

logger = logging.getLogger(__name__)


class NarrationSender(Protocol):
    """The one messenger method narration needs. There is no receive half."""

    async def send(self, message: Message) -> Message: ...


class RunNarrator:
    """Posts run transitions to the workflow's bound group channel."""

    def __init__(self, sender: NarrationSender | None, *, sender_did: str) -> None:
        self._sender = sender
        self._sender_did = sender_did

    async def run_started(
        self, *, channel: str | None, run_id: str, workflow_id: str, version: int
    ) -> None:
        await self._post(
            channel,
            f"Run started: {workflow_id} v{version}",
            run_id=run_id,
            event="run.started",
        )

    async def node_started(
        self, *, channel: str | None, run_id: str, node_id: str, owner: str | None
    ) -> None:
        await self._post(
            channel,
            f"Node {node_id} started" + (f" ({owner})" if owner else ""),
            run_id=run_id,
            event="node.started",
            node_id=node_id,
        )

    async def node_completed(
        self, *, channel: str | None, run_id: str, node_id: str, summary: str = ""
    ) -> None:
        await self._post(
            channel,
            f"Node {node_id} completed{': ' + summary if summary else ''}",
            run_id=run_id,
            event="node.completed",
            node_id=node_id,
        )

    async def handoff(
        self, *, channel: str | None, run_id: str, node_id: str, owner: str | None
    ) -> None:
        """Narrate a handoff. The task row already IS the handoff; this is the story."""
        await self._post(
            channel,
            f"Handed {node_id} to {owner or 'the queue'}",
            run_id=run_id,
            event="node.handoff",
            node_id=node_id,
        )

    async def gate_waiting(self, *, channel: str | None, run_id: str, node_id: str) -> None:
        await self._post(
            channel,
            f"Waiting on gate {node_id}",
            run_id=run_id,
            event="gate.waiting",
            node_id=node_id,
        )

    async def gate_resolved(
        self,
        *,
        channel: str | None,
        run_id: str,
        node_id: str,
        decision: str,
        by: str | None = None,
    ) -> None:
        await self._post(
            channel,
            f"Gate {node_id} {decision}" + (f" by {by}" if by else ""),
            run_id=run_id,
            event="gate.resolved",
            node_id=node_id,
        )

    async def run_outcome(
        self, *, channel: str | None, run_id: str, status: str, detail: str
    ) -> None:
        await self._post(
            channel,
            f"Run {status}{': ' + detail if detail else ''}",
            run_id=run_id,
            event="run.outcome",
        )

    async def _post(self, channel: str | None, body: str, **meta: Any) -> None:
        """Build and send one narration message; a failed delivery is not an error.

        Raises ``ValueError`` for a channel binding that is not a channel — a
        misbound workflow is a definition defect, caught once when the run
        starts, not a per-message surprise.
        """
        if channel is None or self._sender is None:
            return
        scheme, _ = parse_uri(channel)
        if scheme != "channel":
            raise ValueError(f"narration binds to a channel, not {channel!r}")
        message = Message(
            sender=self._sender_did,
            to=[channel],
            msg_type=MsgType.INFO,
            priority=Priority.LOW,
            action_required=False,
            mentions=[],
            body=body,
            meta={"class": "narration", **meta},
        )
        try:
            await self._sender.send(message)
        except Exception:  # reason: narration must never be able to fail a run
            logger.debug("narration dropped for run %s", meta.get("run_id"), exc_info=True)


def assert_channel_binding(channel: str | None) -> None:
    """Validate a workflow's channel binding once, at run start."""
    if channel is None:
        return
    scheme, _ = parse_uri(channel)
    if scheme != "channel":
        raise ValueError(f"workflow channel binding must be a channel URI, got {channel!r}")


__all__ = ["NarrationSender", "RunNarrator", "assert_channel_binding"]
