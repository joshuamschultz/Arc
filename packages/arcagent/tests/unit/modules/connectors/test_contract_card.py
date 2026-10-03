"""D4: a brand-new tool the upstream started serving must ask the card for approval.

A suspended (changed) contract already did; a NEW one stayed silently uncallable
with a card that said "healthy". Both now report ``contract_changed`` so the one
primary button reads "Approve ...".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from arcstore.backends.memory import FakeBackend
from arctrust.audit import NullSink

from arcagent.extension.attachment import ToolSpec
from arcagent.extension.contract_ledger import ToolContractLedger
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore
from arcagent.modules.connectors.capabilities import _servable_tools

INSTANCE = "work"
ACTOR = "did:arc:operator:test"


class _Upstream:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self.specs = specs

    async def describe_tools(self) -> list[ToolSpec]:
        return list(self.specs)


async def _ctx() -> tuple[Any, ConnectionStateStore]:
    store = ConnectionStateStore(FakeBackend())
    await store.create(ConnectionRecord(connection=INSTANCE, custody="arc"), actor_did=ACTOR)
    return SimpleNamespace(store=store, sink=NullSink()), store


def _loaded() -> Any:
    policy = SimpleNamespace(declared=[])
    return SimpleNamespace(manifest=SimpleNamespace(tools=policy))


async def test_a_new_unapproved_tool_raises_the_approve_reason_on_the_card() -> None:
    ctx, store = await _ctx()
    approved = ToolSpec(name="list", description="List things.")
    await ToolContractLedger(store, connection=INSTANCE, sink=NullSink()).approve(
        [approved], actor_did=ACTOR
    )
    added = ToolSpec(name="delete_everything", description="A tool nobody approved.")

    served = await _servable_tools(ctx, INSTANCE, _loaded(), _Upstream([approved, added]))

    assert [spec.name for spec in served] == ["list"], "the new tool stays uncallable"
    record = await store.get(INSTANCE)
    assert (record.status, record.action, record.reason_code) == (
        "needs_you",
        "approve",
        "contract_changed",
    )
    assert "new or changed" in (record.reason_text or "")


async def test_nothing_new_leaves_the_card_alone() -> None:
    ctx, store = await _ctx()
    approved = ToolSpec(name="list", description="List things.")
    await ToolContractLedger(store, connection=INSTANCE, sink=NullSink()).approve(
        [approved], actor_did=ACTOR
    )

    await _servable_tools(ctx, INSTANCE, _loaded(), _Upstream([approved]))

    record = await store.get(INSTANCE)
    assert record.reason_code is None
