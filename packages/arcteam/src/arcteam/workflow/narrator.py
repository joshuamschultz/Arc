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
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from arcteam.types import Message, MsgType, Priority, parse_uri

logger = logging.getLogger(__name__)


class NarrationSender(Protocol):
    """The one messenger method narration needs. There is no receive half."""

    async def send(self, message: Message) -> Message: ...


class RunNarrator:
    """Posts run transitions to the workflow's bound group channel."""

    def __init__(
        self,
        sender: NarrationSender | None,
        *,
        sender_did: str,
        ensure_channel: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._sender = sender
        self._sender_did = sender_did
        self._ensure_channel = ensure_channel
        self._admitted: set[str] = set()

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

    async def runner_degraded(
        self, *, channel: str | None, consecutive_failures: int, last_error: str
    ) -> None:
        """The engine itself is failing repeatedly — not scoped to one run.

        Posted to every channel a run was bound to as of the last tick that
        could actually list active runs, since a whole-tick failure means
        that listing is exactly what broke and there is no fresher channel to
        resolve.
        """
        await self._post(
            channel,
            f"Workflow engine degraded: {consecutive_failures} consecutive tick "
            f"failures (last error: {last_error})",
            event="runner.degraded",
        )

    async def _post(self, channel: str | None, body: str, **meta: Any) -> None:
        """Build and send one narration message; a failed delivery is not an error.

        An empty / unset binding means no narration. Any valid messaging URI is
        a legal target — a group ``channel://``, an ``agent://`` (whose gateway
        relays it out to Telegram/Slack), a ``user://`` or ``role://``. Only the
        channel scheme gates membership, so admission runs for channels alone.
        """
        if not channel or self._sender is None:
            return
        scheme, name = parse_uri(channel)
        if scheme == "channel" and self._ensure_channel is not None and name not in self._admitted:
            # The messenger refuses a sender that is not a registered member of
            # the target channel, so admission has to happen before the first
            # post or every one is silently dropped. Once per channel.
            self._admitted.add(name)
            await self._ensure_channel(name)
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
    """Validate a workflow's response binding once, at run start.

    Empty or unset means no narration — the common case, and it must not raise.
    A non-empty value must be a valid messaging URI (``channel://``,
    ``agent://``, ``user://``, or ``role://``); ``parse_uri`` rejects a bare
    name or an unknown scheme. A bare channel name saved by an older UI is the
    exact ``Invalid URI: ''``-class defect this guard now reports clearly.
    """
    if not channel:
        return
    parse_uri(channel)


__all__ = ["NarrationSender", "RunNarrator", "assert_channel_binding"]
