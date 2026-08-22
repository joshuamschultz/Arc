"""Durable connector-reconciliation commands.

Connection grants live outside an agent process.  A management process therefore
records one command for every affected agent before attempting its local fast
path.  The owning agent drains its own commands when it is alive (and again on
startup), records an acknowledgement, and leaves failed work pending for retry.

The mutable-plane protocol is deliberately structural: ``arcstore`` remains an
optional peer of ``arcagent`` while production composition still uses its
durable PostgreSQL backend.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import time_ns
from typing import Any, Protocol, cast

from arcagent.connector_control import ConnectorReconcileResult

COMMAND_COLLECTION = "connector_reconcile_commands"
ACK_COLLECTION = "connector_reconcile_acks"


class MutableConnectorBackend(Protocol):
    """The narrow ArcStore mutable-plane contract this queue needs."""

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]: ...

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ConnectorReconcileCommand:
    """One idempotent request for one agent to project current grants."""

    command_id: str
    agent: str
    revision: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ConnectorReconcileQueue:
    """Append-only per-agent command ledger with durable acknowledgements."""

    def __init__(
        self,
        opener: Callable[[], Awaitable[Any]],
        *,
        actor_did: str,
    ) -> None:
        self._opener = opener
        self._actor_did = actor_did

    async def enqueue(self, agents: Sequence[str]) -> tuple[ConnectorReconcileCommand, ...]:
        """Persist one command per agent before any live-process optimisation."""
        unique_agents = tuple(dict.fromkeys(agents))
        if not unique_agents:
            return ()
        backend = await self._backend()
        revision = time_ns()
        commands = tuple(
            ConnectorReconcileCommand(
                command_id=f"{revision}:{agent}", agent=agent, revision=revision
            )
            for agent in unique_agents
        )
        await backend.mutable_create_batch(
            COMMAND_COLLECTION,
            [
                (
                    command.command_id,
                    {
                        "command_id": command.command_id,
                        "agent": command.agent,
                        "revision": command.revision,
                        "status": "pending",
                        "attempts": 0,
                        "created_at": _now(),
                    },
                )
                for command in commands
            ],
            actor_did=self._actor_did,
        )
        return commands

    async def acknowledge(
        self, command: ConnectorReconcileCommand, result: ConnectorReconcileResult
    ) -> None:
        """Record the exact live projection that completed this command."""
        backend = await self._backend()
        ack = {
            "command_id": command.command_id,
            "agent": command.agent,
            "revision": command.revision,
            "status": result.status,
            "applied_revision": result.revision,
            "tools": list(result.tools),
            "detail": result.detail,
            "acknowledged_at": _now(),
        }
        await backend.mutable_write(
            ACK_COLLECTION, command.command_id, ack, actor_did=self._actor_did
        )
        if result.status == "applied":
            await backend.mutable_merge(
                COMMAND_COLLECTION,
                command.command_id,
                {"status": "applied", "acknowledged_at": ack["acknowledged_at"]},
                actor_did=self._actor_did,
            )

    async def retry(self, command: ConnectorReconcileCommand, detail: str) -> None:
        """Leave a failed command pending and persist why the next attempt exists."""
        backend = await self._backend()
        rows = await backend.mutable_query(
            COMMAND_COLLECTION, where={"command_id": command.command_id}
        )
        attempts = int(rows[0].get("attempts", 0)) + 1 if rows else 1
        await backend.mutable_write(
            ACK_COLLECTION,
            command.command_id,
            {
                "command_id": command.command_id,
                "agent": command.agent,
                "revision": command.revision,
                "status": "pending",
                "detail": detail,
                "attempts": attempts,
                "acknowledged_at": _now(),
            },
            actor_did=self._actor_did,
        )
        await backend.mutable_merge(
            COMMAND_COLLECTION,
            command.command_id,
            {"status": "pending", "attempts": attempts, "last_error": detail},
            actor_did=self._actor_did,
        )

    async def drain(
        self,
        agent: str,
        reconcile: Callable[[], Awaitable[ConnectorReconcileResult]],
    ) -> tuple[ConnectorReconcileResult, ...]:
        """Apply every outstanding command for ``agent`` without fail-fast loss."""
        backend = await self._backend()
        rows = await backend.mutable_query(COMMAND_COLLECTION, where={"agent": agent})
        commands = sorted(
            (
                ConnectorReconcileCommand(
                    command_id=str(row["command_id"]),
                    agent=agent,
                    revision=int(row["revision"]),
                )
                for row in rows
                if row.get("status") != "applied"
            ),
            key=lambda command: command.revision,
        )
        outcomes: list[ConnectorReconcileResult] = []
        for command in commands:
            try:
                result = await reconcile()
                result = ConnectorReconcileResult(
                    status=result.status,
                    agent=agent,
                    revision=command.revision,
                    tools=result.tools,
                    detail=result.detail,
                )
                await self.acknowledge(command, result)
            except Exception as exc:
                await self.retry(command, str(exc))
                outcomes.append(
                    ConnectorReconcileResult(
                        status="activation_pending",
                        agent=agent,
                        revision=command.revision,
                        detail=f"reconcile retry pending: {exc}",
                    )
                )
                continue
            outcomes.append(result)
        return tuple(outcomes)

    async def _backend(self) -> MutableConnectorBackend:
        return cast(MutableConnectorBackend, await self._opener())


__all__ = [
    "ACK_COLLECTION",
    "COMMAND_COLLECTION",
    "ConnectorReconcileCommand",
    "ConnectorReconcileQueue",
]
