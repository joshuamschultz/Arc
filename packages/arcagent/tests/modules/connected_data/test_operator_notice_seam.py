"""``ArcAgent.notify_operator``: the one seam that puts a notice in front of the operator.

A dead connection (or a failed workflow) has no turn behind it, so nothing else would
ever say so. The seam emits on the agent's module bus; the messaging module answers
over the gateway's channel delivery (the path ``notify_user`` uses) to the channel
the operator last reached the agent on. The sender owns the wording and the
once-only guarantee; this seam only delivers, and says honestly whether anyone took
it.

These run the real bus, the real handler and the real known-channels file; only the
gateway's delivery callback is a double.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.core import known_channels
from arcagent.core.agent import ArcAgent
from arcagent.core.module_bus import ModuleBus
from arcagent.modules.connected_data import _runtime as connected_runtime
from arcagent.modules.connected_data.capabilities import ConnectedData
from arcagent.modules.messaging import _runtime as messaging_runtime
from arcagent.modules.messaging.capabilities import deliver_operator_notice


class _Delivery:
    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fails = fails

    async def __call__(self, target: str, text: str) -> None:
        if self._fails:
            raise ConnectionError("gateway down")
        self.sent.append((target, text))


def _agent(bus: ModuleBus | None, *, started: bool = True) -> Any:
    """An object carrying exactly what ``notify_operator`` reads off an agent."""
    return SimpleNamespace(_started=started, _bus=bus)


async def _notify(agent: Any, text: str = "hello", key: str = "k1") -> str | None:
    return await ArcAgent.notify_operator(agent, text, idempotency_key=key)


@pytest.fixture
def messaging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The messaging state the handler reads, built directly.

    Its ``configure`` bootstraps arcteam and demands a real operator signer, which is
    that module's startup contract and not the routing under test.
    """
    state = SimpleNamespace(
        workspace=tmp_path,
        channel_deliver_fn=None,
        identity=None,
        delivered_notice_keys=OrderedDict(),
    )
    monkeypatch.setattr(messaging_runtime, "state", lambda: state)
    return state


@pytest.fixture
def bus() -> ModuleBus:
    answering = ModuleBus()
    answering.subscribe("agent:operator_notice", deliver_operator_notice, priority=100)
    return answering


async def test_the_notice_goes_to_the_channel_the_operator_last_used(
    messaging: Any, bus: ModuleBus, tmp_path: Path
) -> None:
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")
    messaging.channel_deliver_fn = delivery = _Delivery()

    channel = await _notify(_agent(bus), "Connection 'gmail' needs you.")

    assert channel == "telegram"
    assert delivery.sent == [("telegram:42", "Connection 'gmail' needs you.")]


async def test_no_channel_known_is_reported_undeliverable_not_swallowed(
    messaging: Any, bus: ModuleBus
) -> None:
    messaging.channel_deliver_fn = delivery = _Delivery()

    assert await _notify(_agent(bus)) is None
    assert delivery.sent == []


async def test_a_standalone_agent_with_no_gateway_reports_undeliverable(
    messaging: Any, bus: ModuleBus, tmp_path: Path
) -> None:
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")

    assert await _notify(_agent(bus)) is None


async def test_a_gateway_that_throws_is_undeliverable_not_an_exception(
    messaging: Any, bus: ModuleBus, tmp_path: Path
) -> None:
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")
    messaging.channel_deliver_fn = _Delivery(fails=True)

    assert await _notify(_agent(bus)) is None


async def test_an_agent_that_is_not_started_or_has_no_bus_delivers_nothing() -> None:
    assert await _notify(_agent(ModuleBus(), started=False)) is None
    assert await _notify(_agent(None)) is None


async def test_the_same_idempotency_key_is_sent_once(
    messaging: Any, bus: ModuleBus, tmp_path: Path
) -> None:
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")
    messaging.channel_deliver_fn = delivery = _Delivery()
    agent = _agent(bus)

    first = await _notify(agent, key="connection-health:gmail:1")
    again = await _notify(agent, key="connection-health:gmail:1")
    other = await _notify(agent, key="connection-health:gmail:2")

    assert (first, again, other) == ("telegram", "telegram", "telegram")
    assert len(delivery.sent) == 2


async def test_remembered_keys_are_bounded(messaging: Any, bus: ModuleBus, tmp_path: Path) -> None:
    known_channels.record(tmp_path, target="telegram:42", label="Telegram - Josh")
    messaging.channel_deliver_fn = _Delivery()
    agent = _agent(bus)

    for index in range(300):
        await _notify(agent, key=f"k{index}")

    assert len(messaging.delivered_notice_keys) == 256
    assert "k0" not in messaging.delivered_notice_keys
    assert "k299" in messaging.delivered_notice_keys


async def test_the_production_service_reports_to_the_shared_health_record(
    tmp_path: Path,
) -> None:
    """The wiring itself: a service with no reporter never writes the shared record."""
    from arcagent.extension.connection_health import StoreHealthReporter
    from arcagent.extension.source_catalog import SourceCatalog

    async def opener() -> Any:
        raise AssertionError("opened only when a report is written")

    connected_runtime.configure(agent_did="did:agent", workspace=tmp_path, arcstore_opener=opener)
    state = connected_runtime.state()
    state.source_catalog = SourceCatalog()
    capability = ConnectedData()
    await capability.setup(SimpleNamespace())
    try:
        service = capability.service
        assert service is not None
        assert isinstance(service._reporter, StoreHealthReporter)
        assert not hasattr(service, "_operator_notifier")
    finally:
        await capability.teardown()
        connected_runtime.reset()
