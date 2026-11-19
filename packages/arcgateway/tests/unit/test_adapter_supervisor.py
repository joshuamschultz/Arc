"""One platform adapter's transient failure cannot stop the embedded startup.

The arcui lifespan used to ``await adapter.connect()`` for each platform in
turn, so any adapter that raised on a network blip aborted the whole process.
``AdapterSupervisor`` isolates each adapter: a transient (network / timeout)
failure is reported and retried in the background, a misconfiguration still
raises.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from arcgateway.adapters.supervisor import AdapterSupervisor, is_transient_error


class _FakeAdapter:
    def __init__(self, name: str, agent_did: str, failures: list[Exception]) -> None:
        self.name = name
        self.agent_did = agent_did
        self._failures = list(failures)
        self.connect_calls = 0
        self.connected = False

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._failures:
            raise self._failures.pop(0)
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


class _ReportingAdapter(_FakeAdapter):
    def health(self) -> dict[str, str | None]:
        return {"state": "degraded", "last_error": "reported by adapter"}


def _slack_dns_failure() -> RuntimeError:
    try:
        raise socket.gaierror(-3, "Temporary failure in name resolution")
    except socket.gaierror as cause:
        try:
            raise RuntimeError("Slack Socket Mode connection failed") from cause
        except RuntimeError as exc:
            return exc


def test_transient_errors_are_found_through_the_cause_chain() -> None:
    assert is_transient_error(_slack_dns_failure())
    assert is_transient_error(TimeoutError("read timed out"))
    assert is_transient_error(ConnectionRefusedError())
    assert not is_transient_error(RuntimeError("invalid_auth"))
    assert not is_transient_error(ValueError("bad config"))


@pytest.mark.asyncio
async def test_transient_failure_does_not_stop_other_adapters() -> None:
    broken = _FakeAdapter("slack", "did:arc:agent:a", [_slack_dns_failure()])
    healthy = _FakeAdapter("telegram", "did:arc:agent:b", [])
    supervisor = AdapterSupervisor((broken, healthy), poll_interval_seconds=0.01)
    try:
        await supervisor.start()

        assert healthy.connected
        health = supervisor.health(broken)
        assert health["state"] == "degraded"
        assert "Slack Socket Mode connection failed" in str(health["last_error"])
        assert supervisor.health(healthy) == {"state": "connected", "last_error": None}
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_transient_failure_reconnects_in_background() -> None:
    flaky = _FakeAdapter("slack", "did:arc:agent:a", [_slack_dns_failure()])
    supervisor = AdapterSupervisor((flaky,), poll_interval_seconds=0.01)
    try:
        await supervisor.start()

        async def reconnected() -> None:
            while not flaky.connected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(reconnected(), timeout=2.0)
        assert supervisor.health(flaky)["state"] == "connected"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_misconfiguration_still_raises() -> None:
    bad = _FakeAdapter("slack", "did:arc:agent:a", [RuntimeError("invalid_auth")])
    supervisor = AdapterSupervisor((bad,))

    with pytest.raises(RuntimeError, match="invalid_auth"):
        await supervisor.start()
    await supervisor.stop()


@pytest.mark.asyncio
async def test_adapter_reported_health_wins_when_connect_returned() -> None:
    adapter = _ReportingAdapter("telegram", "did:arc:agent:a", [])
    supervisor = AdapterSupervisor((adapter,))
    await supervisor.start()

    assert supervisor.health(adapter) == {
        "state": "degraded",
        "last_error": "reported by adapter",
    }
    await supervisor.stop()


@pytest.mark.asyncio
async def test_same_platform_adapters_are_tracked_per_agent() -> None:
    first = _FakeAdapter("slack", "did:arc:agent:a", [_slack_dns_failure()])
    second = _FakeAdapter("slack", "did:arc:agent:b", [])
    supervisor = AdapterSupervisor((first, second), poll_interval_seconds=60.0)
    try:
        await supervisor.start()

        assert supervisor.health(first)["state"] == "degraded"
        assert supervisor.health(second)["state"] == "connected"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_stop_cancels_the_background_reconnect() -> None:
    down = _FakeAdapter("slack", "did:arc:agent:a", [_slack_dns_failure()] * 1000)
    supervisor = AdapterSupervisor((down,), poll_interval_seconds=0.01)
    await supervisor.start()
    await supervisor.stop()
    calls = down.connect_calls
    await asyncio.sleep(0.05)

    assert down.connect_calls == calls
