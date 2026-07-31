"""COMP-008 / REQ-182, REQ-184 — the consolidation waiter drives and confirms a pass.

Three properties are under test and nothing else:

1. The waiter awaits the public ``consolidate_poll_once()`` and confirms the pass
   against arcmemory's two on-disk markers — ``memory/.consolidate-last-run``
   advanced, ``memory/.consolidate-manifest.json`` absent — rather than trusting
   the poll's return value. Every way that confirmation can fail raises
   ``ConsolidationStalledError``.
2. A manifest already present *before* a pass fires is refused, because that is
   what an interleaved background pass looks like from outside and arcmemory has
   no lock to make two passes safe (REQ-184).
3. WARNING records on the ``arcmemory.consolidate`` logger are captured for the
   whole run, including when an ancestor logger is configured above WARNING —
   that logger is the only channel carrying ``dedup_skipped`` once the arcmemory
   audit sink is null in a live agent.

No network, no LLM, no real ``ArcAgent``. ``consolidate_poll_once`` is replaced by
a fake that stamps the markers the way ``arcmemory.consolidate.Consolidator`` does,
so the assertions are about the waiter's contract rather than arcmemory's internals.
The memory runtime, however, is the real one: a real ``_State`` is bound through the
real ``activate_runtime_bindings`` path, so an unbound-DID regression would surface
here as the fail-closed error it is in production. Every write lands under
``tmp_path``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from arcagent.brain import NullBrain
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.config import MemoryConfig

from evaluations.ingest.consolidation import (
    CONSOLIDATE_LOGGER_NAME,
    LAST_RUN_NAME,
    MANIFEST_NAME,
    ConsolidationStalledError,
    ConsolidationWaiter,
)

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

AGENT_DID = "did:arc:test-consolidation-waiter"


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """The ``memory/`` subdirectory arcmemory writes its markers into."""
    path = tmp_path / "memory"
    path.mkdir()
    return path


@pytest.fixture
def unbound_state(tmp_path: Path) -> Iterator[_runtime._State]:
    """A real memory ``_State``, deliberately left unregistered and unbound.

    Nothing here binds it: the waiter's own ``activate_runtime_bindings`` call is
    the only thing that can, so dropping that call makes every test below fail
    closed with ``MemoryIsolationError`` instead of quietly still passing.
    """
    state = _runtime._State(
        config=MemoryConfig(),
        brain=NullBrain(),
        workspace=tmp_path,
        telemetry=None,
        bus=None,
        agent_did=AGENT_DID,
        active=True,
        events_since_consolidate=7,
    )
    yield state
    _runtime.reset()


@pytest.fixture
def agent(unbound_state: _runtime._State) -> ArcAgent:
    """The smallest thing ``activate_runtime_bindings`` accepts: a bindings list.

    Passing a stub rather than patching the binding call keeps the real
    rebind-into-this-task path under test — that path is what stops memory's
    ``state()`` from failing closed when the waiter runs outside a turn's task.
    """
    stub = SimpleNamespace(_runtime_bindings=[(_runtime.bind, unbound_state)])
    return cast("ArcAgent", stub)


@pytest.fixture
def waiter(agent: ArcAgent, tmp_path: Path) -> Iterator[ConsolidationWaiter]:
    with ConsolidationWaiter(agent=agent, workspace=tmp_path) as active:
        yield active


def _stamp(memory_dir: Path, when: datetime) -> None:
    (memory_dir / LAST_RUN_NAME).write_text(when.isoformat(), encoding="utf-8")


def _fake_poll(
    monkeypatch: pytest.MonkeyPatch, behavior: Callable[[], bool]
) -> Callable[[], list[int]]:
    """Replace ``consolidate_poll_once`` with ``behavior``; return a call counter."""
    calls: list[int] = []

    async def _poll() -> bool:
        calls.append(1)
        return behavior()

    monkeypatch.setattr("evaluations.ingest.consolidation.consolidate_poll_once", _poll)
    return lambda: calls


# --------------------------------------------------------------- confirmation


async def test_wait_returns_the_observed_window_when_the_stamp_advances(
    waiter: ConsolidationWaiter, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = datetime(2024, 1, 1, tzinfo=UTC)
    after = before + timedelta(minutes=5)
    _stamp(memory_dir, before)

    def _land() -> bool:
        _stamp(memory_dir, after)
        return True

    _fake_poll(monkeypatch, _land)

    result = await waiter.wait()

    assert result.fired is True
    assert result.last_run_before == before
    assert result.last_run_after == after
    # Read from the live memory runtime before the poll zeroes it.
    assert result.window_events == 7


async def test_wait_accepts_the_first_ever_pass_with_no_prior_stamp(
    waiter: ConsolidationWaiter, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after = datetime(2024, 1, 1, tzinfo=UTC)

    def _land() -> bool:
        _stamp(memory_dir, after)
        return True

    _fake_poll(monkeypatch, _land)

    result = await waiter.wait()

    assert result.last_run_before is None
    assert result.last_run_after == after


# --------------------------------------------------------------------- stalls


async def test_wait_raises_when_the_stamp_never_advances(
    waiter: ConsolidationWaiter, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stamp(memory_dir, datetime(2024, 1, 1, tzinfo=UTC))
    _fake_poll(monkeypatch, lambda: True)

    with pytest.raises(ConsolidationStalledError, match=LAST_RUN_NAME):
        await waiter.wait()


async def test_wait_raises_when_the_poll_declines_to_run(
    waiter: ConsolidationWaiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fired=False`` with nothing on disk is a dead capture path, not a quiet turn."""
    _fake_poll(monkeypatch, lambda: False)

    with pytest.raises(ConsolidationStalledError, match="fired=False"):
        await waiter.wait()


async def test_wait_raises_when_the_manifest_survives_the_pass(
    waiter: ConsolidationWaiter, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _die_mid_write() -> bool:
        (memory_dir / MANIFEST_NAME).write_text('{"status": "in_progress"}', encoding="utf-8")
        _stamp(memory_dir, datetime(2024, 1, 1, tzinfo=UTC))
        return True

    _fake_poll(monkeypatch, _die_mid_write)

    with pytest.raises(ConsolidationStalledError, match="did not complete"):
        await waiter.wait()


async def test_wait_refuses_to_fire_when_a_manifest_is_already_present(
    waiter: ConsolidationWaiter, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-184: a manifest at a session boundary means a second pass is in flight."""
    (memory_dir / MANIFEST_NAME).write_text('{"status": "in_progress"}', encoding="utf-8")
    calls = _fake_poll(monkeypatch, lambda: True)

    with pytest.raises(ConsolidationStalledError, match="REQ-184"):
        await waiter.wait()

    assert calls() == []


async def test_wait_raises_when_the_stamp_is_corrupt_after_the_pass(
    waiter: ConsolidationWaiter, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _write_junk() -> bool:
        (memory_dir / LAST_RUN_NAME).write_text("not-a-timestamp", encoding="utf-8")
        return True

    _fake_poll(monkeypatch, _write_junk)

    with pytest.raises(ConsolidationStalledError):
        await waiter.wait()


async def test_wait_outside_the_context_manager_is_refused(
    agent: ArcAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running a pass unwatched would discard the only surviving degrade channel."""
    calls = _fake_poll(monkeypatch, lambda: True)
    detached = ConsolidationWaiter(agent=agent, workspace=tmp_path)

    with pytest.raises(RuntimeError, match="context manager"):
        await detached.wait()

    assert calls() == []


# ------------------------------------------------------- degrade-warning channel


def test_warnings_on_the_arcmemory_logger_are_captured_for_the_whole_run(
    agent: ArcAgent, tmp_path: Path
) -> None:
    logger = logging.getLogger(CONSOLIDATE_LOGGER_NAME)

    with ConsolidationWaiter(agent=agent, workspace=tmp_path) as active:
        logger.warning("entity de-dup skipped: %s", "no-embedder")
        logger.info("a routine pass detail")
        captured = list(active.warnings)

    assert len(captured) == 1
    assert "no-embedder" in captured[0]
    assert "WARNING" in captured[0]


def test_the_handler_is_detached_and_the_level_restored_on_exit(
    agent: ArcAgent, tmp_path: Path
) -> None:
    logger = logging.getLogger(CONSOLIDATE_LOGGER_NAME)
    logger.setLevel(logging.CRITICAL)
    try:
        with ConsolidationWaiter(agent=agent, workspace=tmp_path) as active:
            handlers_during = list(logger.handlers)
        logger.warning("emitted after the run is over")

        assert active.warnings == ()
        assert any(h not in logger.handlers for h in handlers_during)
        assert logger.level == logging.CRITICAL
    finally:
        logger.setLevel(logging.NOTSET)


def test_an_ancestor_level_above_warning_does_not_swallow_the_degrade_channel(
    agent: ArcAgent, tmp_path: Path
) -> None:
    """The whole point of the handler is lost if the logger filters first."""
    logging.getLogger("arcmemory").setLevel(logging.CRITICAL)
    try:
        with ConsolidationWaiter(agent=agent, workspace=tmp_path) as active:
            logging.getLogger(CONSOLIDATE_LOGGER_NAME).warning("dedup_skipped: no-confirmer")
            captured = list(active.warnings)
    finally:
        logging.getLogger("arcmemory").setLevel(logging.NOTSET)

    assert len(captured) == 1
    assert "no-confirmer" in captured[0]
