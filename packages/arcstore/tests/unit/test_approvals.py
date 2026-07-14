"""arcstore ``approvals`` domain — PendingApproval + ApprovalStore.

The shared directory the agent's HumanGate and the operator surfaces meet on:
create a ``pending`` row, resolve it once (race-safe) to ``approved`` (with an
operator grant) / ``denied`` / ``expired``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.sqlite import SqliteBackend

_AGENT = "did:arc:test:exec/aaaaaaaa"
_OPERATOR = "did:arc:test:human/operator"


async def _backend(tmp_path: Path) -> SqliteBackend:
    be = SqliteBackend(tmp_path / "store.db")
    await be.start()
    return be


def _pending(pid: str = "req1") -> PendingApproval:
    return PendingApproval(
        id=pid,
        agent_did=_AGENT,
        agent_label="josh_agent",
        tool="send_message",
        legs=["external_comms", "private_data", "untrusted_input"],
        call_hash="abc123def456",
    )


class TestApprovalStore:
    async def test_create_then_get_roundtrips(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = ApprovalStore(be)
            created = await store.create(_pending())
            assert created.created_at is not None
            got = await store.get("req1")
            assert got is not None
            assert got.status == "pending"
            assert got.call_hash == "abc123def456"
            assert got.legs == ["external_comms", "private_data", "untrusted_input"]
        finally:
            await be.stop()

    async def test_list_filters_by_status(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = ApprovalStore(be)
            await store.create(_pending("a"))
            await store.create(_pending("b"))
            await store.resolve("b", status="denied", actor_did=_OPERATOR, resolved_by=_OPERATOR)
            pending = await store.list(status="pending")
            assert [a.id for a in pending] == ["a"]
        finally:
            await be.stop()

    async def test_resolve_approved_attaches_grant(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = ApprovalStore(be)
            await store.create(_pending())
            wire = {"call_hash": "abc123def456", "approver_did": _OPERATOR, "signature": "x"}
            resolved = await store.resolve(
                "req1", status="approved", actor_did=_OPERATOR, resolved_by=_OPERATOR, grant=wire
            )
            assert resolved is not None
            assert resolved.status == "approved"
            assert resolved.grant == wire
            assert resolved.resolved_by == _OPERATOR
            assert resolved.resolved_at is not None
        finally:
            await be.stop()

    async def test_resolve_missing_returns_none(self, tmp_path: Path) -> None:
        be = await _backend(tmp_path)
        try:
            store = ApprovalStore(be)
            assert await store.resolve("nope", status="denied", actor_did=_OPERATOR) is None
        finally:
            await be.stop()

    async def test_double_resolve_exactly_one_wins(self, tmp_path: Path) -> None:
        # Two operators (or an operator + the timeout sweep) resolve at once —
        # the pending-conditional write must admit exactly one.
        be = await _backend(tmp_path)
        try:
            store = ApprovalStore(be)
            await store.create(_pending())
            barrier = asyncio.Barrier(2)

            async def resolve(status: str) -> PendingApproval | None:
                await barrier.wait()
                return await store.resolve(
                    "req1", status=status, actor_did=_OPERATOR, resolved_by=_OPERATOR  # type: ignore[arg-type]
                )

            results = await asyncio.gather(resolve("approved"), resolve("denied"))
            winners = [r for r in results if r is not None]
            assert len(winners) == 1
            final = await store.get("req1")
            assert final is not None and final.status in ("approved", "denied")
        finally:
            await be.stop()
