"""A request nobody waits on is closed as expired (D3, sweep 2026-10-03).

The gate gives up after its timeout, or the run ends while a tool still waits on
an approval. Either way the row must stop being approvable.
"""

from __future__ import annotations

import asyncio

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcagent.tools.approval_channel import ArcStoreApprovalChannel
from arcagent.tools.human_gate import ApprovalRequest

pytestmark = pytest.mark.asyncio

_TRIFECTA = frozenset({"private_data", "external_comms", "untrusted_input"})
_AGENT = "did:arc:example:org:agent:abc"


async def _store() -> tuple[ApprovalStore, FakeBackend]:
    backend = FakeBackend()
    await backend.start()
    return ApprovalStore(backend), backend


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="egress", agent_did=_AGENT, legs=_TRIFECTA, call_hash="hash123"
    )


async def _wait_for_row(store: ApprovalStore, approval_id: str) -> None:
    for _ in range(200):
        if await store.get(approval_id) is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the channel never wrote its pending row")


async def test_gate_timeout_closes_the_row_as_expired() -> None:
    store, backend = await _store()
    try:
        channel = ArcStoreApprovalChannel(store, id_factory=lambda: "req-timeout")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(channel(_request()), timeout=0.05)
        row = await store.get("req-timeout")
        assert row is not None
        assert row.status == "expired"
        assert row.resolved_at is not None
    finally:
        await backend.stop()


async def test_cancelled_wait_closes_the_row_as_expired() -> None:
    store, backend = await _store()
    try:
        channel = ArcStoreApprovalChannel(store, id_factory=lambda: "req-cancel")
        waiting = asyncio.create_task(channel(_request()))
        await _wait_for_row(store, "req-cancel")
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        row = await store.get("req-cancel")
        assert row is not None
        assert row.status == "expired"
    finally:
        await backend.stop()


async def test_an_operator_decision_is_not_overwritten_by_expiry() -> None:
    store, backend = await _store()
    try:
        channel = ArcStoreApprovalChannel(
            store, id_factory=lambda: "req-denied", poll_interval_seconds=0.01
        )
        waiting = asyncio.create_task(channel(_request()))
        await _wait_for_row(store, "req-denied")
        await store.resolve("req-denied", status="denied", actor_did="op", resolved_by="op")
        assert await asyncio.wait_for(waiting, timeout=2) is None
        row = await store.get("req-denied")
        assert row is not None
        assert row.status == "denied"
    finally:
        await backend.stop()


async def test_a_run_that_ends_while_approval_pends_expires_the_row() -> None:
    from arcrun import StaticProvider
    from arcrun.loop import run_async
    from arcrun.types import Tool
    from packages.arcrun.tests.conftest import LLMResponse, MockModel
    from packages.arcrun.tests.conftest import ToolCall as ModelToolCall

    store, backend = await _store()
    try:
        channel = ArcStoreApprovalChannel(store, id_factory=lambda: "req-run")

        async def _gated(params: dict, ctx: object) -> str:
            await channel(_request())
            return "ran"

        tool = Tool(
            name="gated",
            description="gated",
            input_schema={"type": "object", "properties": {}},
            execute=_gated,
        )
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ModelToolCall(id="tc1", name="gated", arguments={})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="done", stop_reason="end_turn"),
            ]
        )
        handle = await run_async(model, StaticProvider([tool]), "prompt", "task")
        await _wait_for_row(store, "req-run")
        pending = await store.get("req-run")
        assert pending is not None
        assert pending.status == "pending"

        await handle.cancel("did:arc:operator", reason="stop")
        await handle.result()

        row = await store.get("req-run")
        assert row is not None
        assert row.status == "expired"
    finally:
        await backend.stop()
