"""Hotfix: work started before the fleet backend joins waits or degrades quietly.

DGX 9c280994: in the first second after start, the deferred sweep and the
trader_agent digest publishes ran against the fail-closed placeholder backend
and logged four ``FleetBackendUnavailableError`` tracebacks. Before the fleet is
joined the sweep skips its pass (the next tick sweeps), and a digest publish
waits for readiness and is retried once, logging one line (no traceback) only
when the fleet never comes up.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcteam.digest import DigestStore
from arcteam.storage import MemoryBackend
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.modules.messaging import _runtime, capabilities, sweep
from arcagent.modules.messaging.capabilities import (
    messaging_sweep_loop,
    publish_ingest_to_digest,
)

NATS = "nats://127.0.0.1:4222"
FILED = SimpleNamespace(
    data={
        "text": "NNL technical requirements\n\nthe reactor section covers cooling loops",
        "kind": "user",
    }
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def state(tmp_path: Path) -> Any:
    _runtime.configure(
        config=make_config_dict(entity_id="agent://trader", entity_name="trader", nats_url=NATS),
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        operator_signer=make_operator_signer(),
    )
    return _runtime.state()


def _join_fleet(st: Any) -> None:
    """What ensure_live_backend does once NATS answers, minus the network."""
    backend = MemoryBackend()
    st.digests = DigestStore(backend)
    st.live_backend = SimpleNamespace(available=True)
    st.live_subscription = object()
    st.live_backend_ready = True


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.exc_info]


async def test_sweep_before_the_fleet_joins_is_a_quiet_no_op(
    state: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[bool] = []

    async def spy(_st: Any) -> list[Any]:
        called.append(True)
        return []

    monkeypatch.setattr(sweep, "find_missed", spy)
    caplog.set_level(logging.DEBUG)

    await messaging_sweep_loop(SimpleNamespace(data={}))

    assert called == []
    assert _tracebacks(caplog) == []


async def test_sweep_transport_failure_logs_one_line_without_traceback(
    state: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _join_fleet(state)  # ready, but the placeholder svc still refuses
    caplog.set_level(logging.DEBUG)

    assert await sweep.run_once(state, lambda *_: None) == 0

    assert _tracebacks(caplog) == []
    assert any("fleet" in record.getMessage().lower() for record in caplog.records)


async def test_digest_publish_waits_for_the_fleet_then_lands_once(
    state: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await publish_ingest_to_digest(FILED)
    assert len(state.pending_publishes) == 1
    _join_fleet(state)
    await capabilities.drain_pending_publishes()

    digest = await state.digests.get(state.identity.did)
    assert digest is not None
    assert [entry.title for entry in digest.entries] == ["NNL technical requirements"]
    assert _tracebacks(caplog) == []


async def test_digest_publish_degrades_quietly_when_the_fleet_never_joins(
    state: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capabilities, "FLEET_READY_WAIT_SECONDS", 0.05)
    caplog.set_level(logging.DEBUG)

    await publish_ingest_to_digest(FILED)
    await asyncio.wait_for(capabilities.drain_pending_publishes(), timeout=2)

    assert _tracebacks(caplog) == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "digest" in warnings[0].getMessage()


async def test_failed_fleet_join_leaves_no_consumer_on_the_closed_connection(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DGX 2026-10-03: a startup timeout orphaned the first stream's consume loop.

    The backend was then closed under it and the loop retried the dead
    connection ~1000x/min forever. A failed join must stop every consumer before
    the connection it ran on is closed.
    """
    import arcteam
    from arcteam import composition
    from arcteam.messenger import MessagingService

    closed = asyncio.Event()

    class _ClosingBackend(MemoryBackend):
        async def close(self) -> None:
            closed.set()

    async def connect(_url: str) -> arcteam.StorageBackend:
        return _ClosingBackend()

    real_refresh = MessagingService.refresh_subscription

    async def open_one_then_time_out(self: Any, entity_id: str, subscription: Any) -> None:
        await real_refresh(self, entity_id, subscription)  # first stream's loop is running
        raise TimeoutError("nats timeout")

    monkeypatch.setattr(composition, "make_backend", connect)
    monkeypatch.setattr(MessagingService, "refresh_subscription", open_one_then_time_out)
    before = set(asyncio.all_tasks())

    async def receive(_message: Any) -> None:
        return None

    with pytest.raises(TimeoutError):
        await _runtime.ensure_live_backend(receive)

    assert closed.is_set()
    assert [t for t in asyncio.all_tasks() - before if not t.done()] == []
    assert not state.live_backend_ready
