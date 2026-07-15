"""Run-control watcher — applies operator cancel requests to live tracked runs.

Proves the per-agent ``@background_task`` seam: a pending ``cancellations`` row is
matched to a live ``RunHandle`` (by run_id or session_key), the handle is cancelled
carrying the operator DID, the request is resolved ``applied``, and an
operator-attributed audit event fires. A request naming no live run stays pending.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from arcstore.backends.sqlite import SqliteBackend
from arcstore.cancellations import CancelRequest, CancelStore

from arcagent.modules.runcontrol import _runtime
from arcagent.modules.runcontrol.capabilities import _watch_tick

_OPERATOR = "did:arc:test:human/operator"


class _FakeState:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


class _FakeHandle:
    """Records the attributed cancel the watcher issues."""

    def __init__(self, run_id: str) -> None:
        self.state = _FakeState(run_id)
        self.cancelled_with: tuple[str, str | None] | None = None

    async def cancel(self, caller_did: str, reason: str | None = None) -> None:
        self.cancelled_with = (caller_did, reason)


class _FakeAgent:
    def __init__(self, active: dict[str, Any]) -> None:
        self._active_runs = active


class _FakeTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[CancelStore]:
    be = SqliteBackend(tmp_path / "store.db")
    await be.start()
    yield CancelStore(be)
    await be.stop()


def _configure(store: CancelStore, agent: Any, telemetry: Any) -> _runtime._State:
    from arctrust import AgentIdentity

    _runtime.configure(config={}, identity=AgentIdentity.generate(org="local", agent_type="agent"))
    st = _runtime.state()
    st.store = store
    st.agent = agent
    st.telemetry = telemetry
    return st


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    yield
    _runtime.reset()


class TestWatcher:
    async def test_matches_run_id_and_cancels_with_attribution(self, store: CancelStore) -> None:
        handle = _FakeHandle("run-abc")
        telemetry = _FakeTelemetry()
        _configure(store, _FakeAgent({"sess-1": handle}), telemetry)
        await store.create(
            CancelRequest(id="c1", run_id="run-abc", requested_by=_OPERATOR, reason="too long")
        )

        await _watch_tick()

        # Handle stopped, carrying the operator DID + reason (ASI09/ASI10).
        assert handle.cancelled_with == (_OPERATOR, "too long")
        # Request resolved applied, race-safely.
        req = await store.get("c1")
        assert req is not None and req.status == "applied"
        # Operator-attributed audit at the point of application.
        assert telemetry.events == [
            (
                "run.cancel.applied",
                {
                    "caller_did": _OPERATOR,
                    "run_id": "run-abc",
                    "session_key": "sess-1",
                    "reason": "too long",
                },
            )
        ]

    async def test_matches_session_key(self, store: CancelStore) -> None:
        handle = _FakeHandle("run-xyz")
        _configure(store, _FakeAgent({"cli:main": handle}), _FakeTelemetry())
        await store.create(CancelRequest(id="c1", session_key="cli:main", requested_by=_OPERATOR))

        await _watch_tick()

        assert handle.cancelled_with == (_OPERATOR, None)
        req = await store.get("c1")
        assert req is not None and req.status == "applied"

    async def test_no_matching_run_stays_pending(self, store: CancelStore) -> None:
        handle = _FakeHandle("run-abc")
        _configure(store, _FakeAgent({"sess-1": handle}), _FakeTelemetry())
        await store.create(CancelRequest(id="c1", run_id="run-GONE", requested_by=_OPERATOR))

        await _watch_tick()

        # No live run named run-GONE: the handle is untouched and the request is
        # left pending (the run may not have started, or it is a GAP-A run).
        assert handle.cancelled_with is None
        req = await store.get("c1")
        assert req is not None and req.status == "pending"

    async def test_no_agent_bound_is_a_noop(self, store: CancelStore) -> None:
        _configure(store, None, _FakeTelemetry())
        await store.create(CancelRequest(id="c1", run_id="run-abc", requested_by=_OPERATOR))

        await _watch_tick()  # agent:ready has not fired — must not raise

        req = await store.get("c1")
        assert req is not None and req.status == "pending"
