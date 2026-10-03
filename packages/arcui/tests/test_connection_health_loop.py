"""The deployment's probe loop and notice delivery (P18-1, 9.6).

The loop is the only thing that keeps a connection's health record true between
operator clicks, so each test drives it as the lifespan does: a real monitor over a
real in-memory arcstore. The clock and the jitter source are injected so the schedule
is asserted, not hoped for.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import arcagent
from arcstore.backends.memory import FakeBackend
from arctrust import causal
from packages.arcui.tests.connection_fleet import INSTANCE, Fleet

from arcui.connection_health import ConnectionHealthMonitor

START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class _Connections:
    """The slice of ``Connections`` a monitor drives, recording every probe."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.checked: list[tuple[str, str]] = []
        self.registry = self

    async def ensure_health_records(self) -> None:
        return None

    async def check_health(self, instance: str, *, checked_by: str, **_: Any) -> None:
        self.checked.append((instance, checked_by))

    def get(self, instance: str) -> Any:
        raise AssertionError("not used")


async def _seed(backend: FakeBackend, **statuses: str) -> None:
    store = arcagent.ConnectionStateStore(backend)
    for name, status in statuses.items():
        await store.create(
            arcagent.ConnectionRecord.model_validate(
                {"connection": name, "status": status, "last_success_at": START.isoformat()}
            ),
            actor_did="did:arc:test",
        )


def _monitor(
    backend: FakeBackend,
    connections: _Connections,
    clock: _Clock,
    agents: Any = None,
    fallback: Any = None,
    ui_base: str = "",
) -> ConnectionHealthMonitor:
    async def opener() -> FakeBackend:
        return backend

    return ConnectionHealthMonitor(
        lambda: connections,  # type: ignore[arg-type,return-value] # reason: test double
        store_opener=opener,
        agents_resolver=agents or (lambda instance: []),
        fallback_agents=fallback or (lambda: []),
        ui_base=lambda: ui_base,
        clock=clock,
        rng=lambda: 0.5,
        initial_delay_seconds=0,
    )


async def test_loop_probes_each_connection_with_jitter_and_tightens_on_needs_you() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _seed(backend, gmail="healthy", jira="needs_you")
    connections = _Connections(("gmail", "jira"))
    monitor = _monitor(backend, connections, clock)

    await monitor.tick()
    assert sorted(name for name, _ in connections.checked) == ["gmail", "jira"]
    assert {actor for _, actor in connections.checked} == {arcagent.PROBE_DID}

    store = arcagent.ConnectionStateStore(backend)
    healthy = await store.get("gmail")
    dead = await store.get("jira")
    assert healthy is not None and dead is not None
    assert healthy.next_check_at is not None and dead.next_check_at is not None
    # rng 0.5: healthy = 30 min + 2.5 min jitter; needs_you = 5 min + 30 s.
    assert datetime.fromisoformat(healthy.next_check_at) == START + timedelta(
        minutes=32, seconds=30
    )
    assert datetime.fromisoformat(dead.next_check_at) == START + timedelta(minutes=5, seconds=30)

    connections.checked.clear()
    clock.now = START + timedelta(minutes=6)
    await monitor.tick()
    assert [name for name, _ in connections.checked] == ["jira"], "only the needs_you one is due"

    connections.checked.clear()
    clock.now = START + timedelta(minutes=33)
    await monitor.tick()
    assert sorted(name for name, _ in connections.checked) == ["gmail", "jira"]


async def test_a_connection_that_just_broke_is_rescheduled_at_the_tight_interval() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _seed(backend, gmail="healthy")

    class _Breaking(_Connections):
        async def check_health(self, instance: str, **kwargs: Any) -> None:
            await super().check_health(instance, **kwargs)
            authority = arcagent.ConnectionHealthAuthority(arcagent.ConnectionStateStore(backend))
            await authority.record(
                instance,
                arcagent.HealthSignal(
                    ok=False,
                    source="probe",
                    checked_by=arcagent.PROBE_DID,
                    reason_code="invalid_grant",
                ),
                now=clock(),
            )

    monitor = _monitor(backend, _Breaking(("gmail",)), clock)

    await monitor.tick()

    record = await arcagent.ConnectionStateStore(backend).get("gmail")
    assert record is not None and record.status == "needs_you"
    assert record.next_check_at is not None
    assert datetime.fromisoformat(record.next_check_at) == START + timedelta(minutes=5, seconds=30)


async def test_loop_is_a_detached_scheduler_root(tmp_path: Any, monkeypatch: Any) -> None:
    """A probe is attributed to the probe loop, never to a page view in flight."""
    fleet = Fleet(tmp_path, monkeypatch)
    fleet.install()
    backend_record = await arcagent.ConnectionStateStore(fleet.backend).get(INSTANCE)
    assert backend_record is not None
    fleet.sink.events.clear()
    from arcui.connection_health import build_connection_health_monitor

    app = fleet.client.app
    monitor = build_connection_health_monitor(app, initial_delay_seconds=0)
    assert monitor is not None
    # Force the record due, then run the loop for real: a detached root, as the lifespan does.
    await arcagent.ConnectionStateStore(fleet.backend).schedule_check(
        INSTANCE, "2000-01-01T00:00:00+00:00", actor_did="did:arc:test"
    )
    with causal.bind(causal.root("ui_session", "did:arc:ui:session:page-view")):
        monitor.start()
    try:

        async def probed() -> bool:
            return any(e.action == "connection.health.checked" for e in fleet.sink.events)

        for _ in range(200):
            if await probed():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the loop never probed the due connection")
    finally:
        await monitor.stop()

    checked = [e for e in fleet.sink.events if e.action == "connection.health.checked"]
    assert checked
    for event in checked:
        assert event.causal is not None
        assert event.causal.initiator == "connector_probe"
        assert event.actor_did == arcagent.PROBE_DID


class _Agent:
    def __init__(self, channel: str | None) -> None:
        self.channel = channel
        self.calls: list[tuple[str, str]] = []

    async def notify_operator(self, text: str, *, idempotency_key: str) -> str | None:
        self.calls.append((text, idempotency_key))
        return self.channel


async def test_notice_relayed_through_first_granted_agent_that_delivers() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _seed(backend, gmail="healthy")
    authority = arcagent.ConnectionHealthAuthority(arcagent.ConnectionStateStore(backend))
    await authority.record(
        "gmail",
        arcagent.HealthSignal(
            ok=False, source="probe", checked_by=arcagent.PROBE_DID, reason_code="invalid_grant"
        ),
        now=START,
    )
    first, second, third = _Agent(None), _Agent("telegram"), _Agent("slack")
    monitor = _monitor(
        backend, _Connections(("gmail",)), clock, agents=lambda instance: [first, second, third]
    )

    await monitor.tick()

    assert len(first.calls) == len(second.calls) == 1
    assert third.calls == [], "delivery stops at the first agent that reached the operator"
    text, key = second.calls[0]
    assert key == "connection-health:gmail:1"
    assert text.startswith("Connection 'gmail' needs you:")
    record = await arcagent.ConnectionStateStore(backend).get("gmail")
    assert record is not None and record.last_notice is not None
    assert (record.last_notice.delivered, record.last_notice.channel) == (True, "telegram")
    assert record.notified_seq == 1


async def test_no_agent_able_to_deliver_gives_up_after_three_tries_and_says_so() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _seed(backend, gmail="healthy")
    await arcagent.ConnectionHealthAuthority(arcagent.ConnectionStateStore(backend)).record(
        "gmail",
        arcagent.HealthSignal(
            ok=False, source="probe", checked_by=arcagent.PROBE_DID, reason_code="invalid_grant"
        ),
        now=START,
    )
    mute = _Agent(None)
    monitor = _monitor(backend, _Connections(("gmail",)), clock, agents=lambda instance: [mute])

    for minutes in (0, 2, 8, 20):
        clock.now = START + timedelta(minutes=minutes)
        await monitor.tick()

    assert len(mute.calls) == 3
    record = await arcagent.ConnectionStateStore(backend).get("gmail")
    assert record is not None and record.last_notice is not None
    assert record.last_notice.delivered is False


async def _broken_gmail(backend: FakeBackend) -> None:
    await _seed(backend, gmail="healthy")
    await arcagent.ConnectionHealthAuthority(arcagent.ConnectionStateStore(backend)).record(
        "gmail",
        arcagent.HealthSignal(
            ok=False, source="probe", checked_by=arcagent.PROBE_DID, reason_code="invalid_grant"
        ),
        now=START,
    )


async def test_notice_falls_back_to_any_embedded_agent_then_undeliverable() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _broken_gmail(backend)
    ungranted = _Agent("slack")
    monitor = _monitor(
        backend, _Connections(("gmail",)), clock, agents=lambda i: [], fallback=lambda: [ungranted]
    )

    await monitor.tick()

    record = await arcagent.ConnectionStateStore(backend).get("gmail")
    assert record is not None and record.last_notice is not None
    assert len(ungranted.calls) == 1, "an agent with no grant still carries the notice"
    assert (record.last_notice.delivered, record.last_notice.channel) == (True, "slack")

    nobody_backend = FakeBackend()
    await _broken_gmail(nobody_backend)
    nobody = _monitor(nobody_backend, _Connections(("gmail",)), clock)
    for minutes in (0, 2, 8, 20):
        clock.now = START + timedelta(minutes=minutes)
        await nobody.tick()

    record = await arcagent.ConnectionStateStore(nobody_backend).get("gmail")
    assert record is not None and record.last_notice is not None
    assert (record.last_notice.delivered, record.last_notice.channel) == (False, "undeliverable")


async def test_notice_link_uses_public_base_url_when_set() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _broken_gmail(backend)
    agent = _Agent("telegram")
    monitor = _monitor(
        backend,
        _Connections(("gmail",)),
        clock,
        agents=lambda i: [agent],
        ui_base="https://arc.example.com/",
    )

    await monitor.tick()

    assert agent.calls[0][0].endswith("https://arc.example.com/connections?focus=gmail")


async def test_notice_carries_no_link_when_public_base_url_is_unset() -> None:
    backend, clock = FakeBackend(), _Clock()
    await _broken_gmail(backend)
    agent = _Agent("telegram")
    monitor = _monitor(backend, _Connections(("gmail",)), clock, agents=lambda i: [agent])

    await monitor.tick()

    assert "http" not in agent.calls[0][0]


async def test_built_monitor_reads_the_public_address_again_for_every_notice() -> None:
    """Changing the address in Settings changes the next notice's link, with no restart."""
    from types import SimpleNamespace

    from arcui.connection_health import build_connection_health_monitor

    class _Address:
        value: str | None = "https://old.example.com"

        def current(self) -> str | None:
            return self.value

    address = _Address()
    state = SimpleNamespace(arcstore_backend=FakeBackend(), public_address=address)
    monitor = build_connection_health_monitor(SimpleNamespace(state=state))
    assert monitor is not None and monitor._ui_base() == "https://old.example.com"

    address.value = "https://new.example.com"
    assert monitor._ui_base() == "https://new.example.com"

    address.value = None
    assert monitor._ui_base() == ""
