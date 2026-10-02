"""The operator notifier is wired: a dead connection reaches a human, once.

``_notify_operator`` only logged: the production ``ConnectedDataService`` was built
with no ``operator_notifier``, so a Google account could be dead for a month and
nobody was told. The notifier now asks the module bus, and the messaging module
answers over the gateway's channel delivery (the path ``notify_user`` uses) to the
channel the operator last reached the agent on. The connected-data module names no
channel and imports no core: that boundary is enforced by an architecture test.

These run the real bus, the real handler and the real known-channels file; only the
gateway's delivery callback is a double.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.core import known_channels
from arcagent.core.module_bus import ModuleBus
from arcagent.modules.connected_data import _runtime as connected_runtime
from arcagent.modules.connected_data.capabilities import (
    OPERATOR_ATTENTION_EVENT,
    ConnectedData,
    _operator_notifier,
)
from arcagent.modules.messaging import _runtime as messaging_runtime
from arcagent.modules.messaging.capabilities import deliver_connection_attention


class _Delivery:
    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fails = fails

    async def __call__(self, target: str, text: str) -> None:
        if self._fails:
            raise ConnectionError("gateway down")
        self.sent.append((target, text))


def _answering_bus() -> ModuleBus:
    """A bus with the messaging module's handler registered, as an agent has it."""
    bus = ModuleBus()
    bus.subscribe(OPERATOR_ATTENTION_EVENT, deliver_connection_attention, priority=100)
    return bus


@pytest.fixture
def runtimes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The connected-data state and the messaging state the handler reads.

    The messaging state is built directly: its ``configure`` bootstraps arcteam and
    demands a real operator signer, which is that module's startup contract and not
    the routing under test.
    """
    connected_runtime.configure(agent_did="did:agent", workspace=tmp_path, bus=_answering_bus())
    messaging = SimpleNamespace(workspace=tmp_path, channel_deliver_fn=None, identity=None)
    monkeypatch.setattr(messaging_runtime, "state", lambda: messaging)
    yield connected_runtime.state(), messaging
    connected_runtime.reset()


async def test_the_notice_goes_to_the_channel_the_operator_last_used(
    runtimes: Any, tmp_path: Path
) -> None:
    connected, messaging = runtimes
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")
    delivery = _Delivery()
    messaging.channel_deliver_fn = delivery

    delivered = await _operator_notifier(connected)("gmail-blackarc", "auth_required")

    assert delivered is True
    assert [target for target, _ in delivery.sent] == ["telegram:42"]
    assert "gmail-blackarc" in delivery.sent[0][1]
    assert "auth_required" in delivery.sent[0][1]


async def test_no_channel_known_is_reported_undeliverable_not_swallowed(runtimes: Any) -> None:
    connected, messaging = runtimes
    delivery = _Delivery()
    messaging.channel_deliver_fn = delivery

    assert await _operator_notifier(connected)("gmail-blackarc", "auth_required") is False
    assert delivery.sent == []


async def test_a_standalone_agent_with_no_gateway_reports_undeliverable(
    runtimes: Any, tmp_path: Path
) -> None:
    connected, _ = runtimes
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")

    assert await _operator_notifier(connected)("gmail-blackarc", "auth_required") is False


async def test_a_gateway_that_throws_does_not_break_the_sync_loop(
    runtimes: Any, tmp_path: Path
) -> None:
    connected, messaging = runtimes
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")
    messaging.channel_deliver_fn = _Delivery(fails=True)

    assert await _operator_notifier(connected)("gmail-blackarc", "auth_required") is False


async def test_with_no_bus_the_notice_is_undeliverable(tmp_path: Path) -> None:
    connected_runtime.configure(agent_did="did:agent", workspace=tmp_path)
    try:
        assert await _operator_notifier(connected_runtime.state())("c", "auth_required") is False
    finally:
        connected_runtime.reset()


async def test_the_production_service_is_built_with_the_notifier(tmp_path: Path) -> None:
    """The wiring itself: a service with no notifier is the bug."""
    from arcagent.extension.source_catalog import SourceCatalog

    connected_runtime.configure(agent_did="did:agent", workspace=tmp_path, bus=ModuleBus())
    state = connected_runtime.state()
    state.source_catalog = SourceCatalog()
    capability = ConnectedData()
    await capability.setup(SimpleNamespace())
    try:
        service = capability.service
        assert service is not None
        assert service._operator_notifier is not None
        assert service._failure_ceiling == state.config.consecutive_failure_ceiling
    finally:
        await capability.teardown()
        connected_runtime.reset()
