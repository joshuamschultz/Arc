"""Helpers for tests whose connected-data writes go through the real sync worker.

Nothing here fakes the worker: ``serve_from`` binds this process's real
supervisor (a real child process) to the test's arcstore backend, exactly as the
connected-data module binds it to an agent's. ``LoopbackHost`` drives the main
process's host service in-process, for unit tests of the worker-side authorities.
"""

from __future__ import annotations

from typing import Any

from arcagent.modules.connected_data.sync_worker import process_supervisor
from arcagent.modules.connected_data.sync_worker.host import HostService


def serve_from(backend: Any, *agent_dids: str, audit_sink: Any = None) -> None:
    """Let this process's sync worker reach ``backend`` for ``agent_dids``' stores."""

    async def opener() -> Any:
        return backend

    host = process_supervisor().host
    host.bind(arcstore_opener=opener, audit_sink=audit_sink)
    for did in agent_dids:
        host.serve_agent(did)


async def approve_document_mapping(
    backend: Any, agent_did: str, *, approval_id: str | None = None
) -> str:
    """An operator-approved document-only mapping for ``agent_did``; its approval id.

    What a migrating agent's own approval looks like to the worker, which re-reads
    it before it lets the agent claim or adopt into a shared store.
    """
    from arcstore.approvals import ApprovalStore, PendingApproval

    store = ApprovalStore(backend)
    await store.start()
    row = await store.create(
        PendingApproval(
            id=approval_id or f"approval-{agent_did.rsplit(':', 1)[-1]}",
            agent_did=agent_did,
            tool="memory.map_source",
            call_hash=f"hash-{agent_did}",
            arguments={"homes": "document"},
        )
    )
    await store.resolve(row.id, status="approved", actor_did="did:arc:test:operator")
    return row.id


class LoopbackHost:
    """The worker's ``HostClient`` surface, answered by a ``HostService`` in this process."""

    def __init__(self, backend: Any, *agent_dids: str) -> None:
        async def opener() -> Any:
            return backend

        self.service = HostService(arcstore_opener=opener, audit_sink=None)
        for did in agent_dids:
            self.service.serve_agent(did)

    async def call(self, op: str, args: dict[str, Any]) -> Any:
        result, _ = await self.service.handle(op, args, b"")
        return result


__all__ = ["LoopbackHost", "approve_document_mapping", "serve_from"]
