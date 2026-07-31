"""Tests for COMP-007 IngestDriver — T-800.

Covers REQ-179 (one real agent turn per chunk, after ``startup()``, on a
distinct ``ingest:<session_idx>:<chunk_idx>`` session key), REQ-184 (the
background consolidation loop and harness-driven passes never coexist) and the
half of REQ-205 the driver owns: a report exists only for a fully fed
workspace, so the ``.ingest_complete`` marker can never be written mid-ingest.

The AGENT is stubbed — it is the collaborator, not the code under test, and a
real one costs an LLM call per chunk. Everything else is real: chunks come from
the real ``TurnChunker``, screening runs the real ``arcmemory.security``
filters through the real gate, and the loop-cancellation test drives a real
``CapabilityRegistry`` with a really-spawned ``asyncio.Task``. Nothing here
touches the network and nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from evaluations.longmemeval.ingest.chunker import TurnChunker
from evaluations.longmemeval.ingest.driver import (
    CONSOLIDATE_LOOP_NAME,
    BackgroundConsolidationError,
    IngestDriver,
    IngestTarget,
    session_key,
)
from evaluations.longmemeval.ingest.fidelity import GoldEvidenceFilteredError, SanitizeFidelityGate
from evaluations.longmemeval.ingest.types import Chunk, Session, Turn

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

CAP = 2000
SESSION_DATE = date(2023, 5, 20)

# Ordinary benchmark prose the live filters destroy: the second trips
# ``_INJECTION_RE``'s "you are now" branch, which deletes to end of line.
CLEAN_TURN = "I finally finished the PMP exam prep course."
INJECTION_TURN = "Congratulations! You are now a certified PM as of May 2023."


def _arcagent_satisfies_the_protocol(agent: ArcAgent) -> IngestTarget:
    """The real ``ArcAgent`` is a structural ``IngestTarget``.

    Proved by the ``mypy --strict`` gate rather than at runtime: the protocol is
    what the driver is typed against, and nothing else in this file passes a
    real agent, so without this the driver could drift from ``ArcAgent`` and
    every stub-based test would keep passing.
    """
    return agent


# ---------------------------------------------------------------------------
# Test doubles — the agent and, where the registry is not the thing under test,
# the registry.
# ---------------------------------------------------------------------------


class StubRegistry:
    """A capability registry that reports the consolidation loop absent."""

    def __init__(self) -> None:
        self.unregistered: list[tuple[str, str]] = []

    async def unregister(self, kind: str, name: str) -> None:
        self.unregistered.append((kind, name))

    async def get_task(self, name: str) -> Any:
        return None


class StubbornRegistry(StubRegistry):
    """A registry whose unregister does not take — the fail-closed case."""

    async def get_task(self, name: str) -> Any:
        return object()


class StubAgent:
    """Records the turns the driver sends, and refuses concurrent ones.

    ``run_collected`` yields to the event loop mid-call, so an overlapping
    second turn would be observable here. A stub that never awaited would let a
    concurrent driver pass this suite.
    """

    def __init__(
        self,
        registry: Any = None,
        *,
        fail_at: int | None = None,
        probe: Callable[[], int] | None = None,
    ) -> None:
        self._capability_registry = StubRegistry() if registry is None else registry
        self.calls: list[tuple[str, str]] = []
        self.in_flight = 0
        self._fail_at = fail_at
        self._probe = probe
        self.probes: list[int] = []

    async def run_collected(self, input_text: str, *, session_key: str) -> Any:
        if self._probe is not None:
            self.probes.append(self._probe())
        self.in_flight += 1
        assert self.in_flight == 1, "ingest turns must be serial within a question"
        await asyncio.sleep(0)
        if self._fail_at is not None and len(self.calls) == self._fail_at:
            self.in_flight -= 1
            raise RuntimeError("provider blew up mid-ingest")
        self.calls.append((session_key, input_text))
        self.in_flight -= 1
        return None


@pytest.fixture
def driver() -> IngestDriver:
    return IngestDriver(gate=SanitizeFidelityGate(max_event_chars=CAP))


def _chunks(*sessions: tuple[str, ...]) -> list[Chunk]:
    """Chunk each argument (a session's turn texts) through the real chunker."""
    chunker = TurnChunker(max_event_chars=CAP)
    out: list[Chunk] = []
    for session_idx, texts in enumerate(sessions):
        session = Session(
            conversation_id=f"c{session_idx}",
            source_date=SESSION_DATE,
            turns=[
                Turn(turn_id=f"s{session_idx}t{i}", role="user", text=text)
                for i, text in enumerate(texts)
            ],
        )
        out.extend(chunker.split(session, session_idx=session_idx))
    return out


# ---------------------------------------------------------------------------
# REQ-179 — one turn per chunk, in order, on its own session key
# ---------------------------------------------------------------------------


async def test_session_key_is_distinct_per_chunk(driver: IngestDriver) -> None:
    """One key per chunk is what removes quadratic growth and compaction."""
    chunks = _chunks((CLEAN_TURN, CLEAN_TURN + " Again."), (CLEAN_TURN,))
    chunks = [chunk.model_copy(update={"chunk_idx": i}) for i, chunk in enumerate(chunks)]
    agent = StubAgent()

    await driver.ingest(agent, chunks, gold_turn_ids=set())

    keys = [key for key, _ in agent.calls]
    assert len(keys) == len(chunks)
    assert len(set(keys)) == len(keys), f"session keys repeat across chunks: {keys}"


async def test_session_key_names_the_chunk_coordinate(driver: IngestDriver) -> None:
    """The key is ``ingest:<session_idx>:<chunk_idx>``, verbatim."""
    chunks = [
        Chunk(text="a", session_idx=0, chunk_idx=0, turn_ids=["t0"]),
        Chunk(text="b", session_idx=0, chunk_idx=1, turn_ids=["t1"]),
        Chunk(text="c", session_idx=7, chunk_idx=0, turn_ids=["t2"]),
    ]
    agent = StubAgent()

    await driver.ingest(agent, chunks, gold_turn_ids=set())

    assert [key for key, _ in agent.calls] == ["ingest:0:0", "ingest:0:1", "ingest:7:0"]
    assert session_key(chunks[2]) == "ingest:7:0"


async def test_chunks_are_fed_in_dataset_order(driver: IngestDriver) -> None:
    """Ingest order is what arcmemory's recency channel ranks by (REQ-178)."""
    chunks = _chunks((CLEAN_TURN,), ("Second session.",), ("Third session.",))
    agent = StubAgent()

    await driver.ingest(agent, chunks, gold_turn_ids=set())

    assert [text for _, text in agent.calls] == [chunk.text for chunk in chunks]


async def test_report_counts_and_hashes_what_was_fed(driver: IngestDriver) -> None:
    chunks = _chunks((CLEAN_TURN,), ("Second session.",))
    agent = StubAgent()

    report = await driver.ingest(agent, chunks, gold_turn_ids=set())

    assert report.chunks_fed == len(chunks)
    assert report.chunk_hashes == [
        hashlib.sha256(chunk.text.encode("utf-8")).hexdigest() for chunk in chunks
    ]
    assert report.warnings == []


async def test_run_collected_is_called_the_way_arcagent_declares_it() -> None:
    """``session_key`` is keyword-only on the real method; the driver passes it so."""
    from arcagent.core.agent import ArcAgent

    sig = inspect.signature(ArcAgent.run_collected)
    assert sig.parameters["input_text"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert sig.parameters["session_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["session_key"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# REQ-184 — the background consolidation loop never runs alongside the harness
# ---------------------------------------------------------------------------


async def test_background_consolidation_loop_is_cancelled_in_a_real_registry(
    driver: IngestDriver,
) -> None:
    """A really-spawned task under the loop's real name is dead before feeding.

    The registry, the entry, the metadata and the ``asyncio.Task`` are all real;
    only the loop BODY is a stand-in, so the test can observe iterations without
    a configured arcmemory runtime. The assertion that matters is the probe: the
    iteration count read at every chunk feed is identical, so no poll landed
    between two chunks. arcmemory has no lock, and a poll that did land would
    share a SQLite connection and a manifest with the harness's own pass.
    """
    from arcagent.capabilities.capability_registry import (
        BackgroundTaskEntry,
        CapabilityRegistry,
    )
    from arcagent.tools import BackgroundTaskMetadata

    iterations = 0

    async def spinning_loop(_ctx: Any) -> None:
        nonlocal iterations
        while True:
            iterations += 1
            await asyncio.sleep(0)

    registry = CapabilityRegistry()
    entry = BackgroundTaskEntry(
        meta=BackgroundTaskMetadata(name=CONSOLIDATE_LOOP_NAME, interval=300.0),
        fn=spinning_loop,
        source_path=Path("memory/capabilities.py"),
        scan_root="module:memory",
    )
    await registry.register_task(entry, spawn=True)
    await asyncio.sleep(0)
    assert iterations > 0, "the loop must really be running, or the test proves nothing"

    agent = StubAgent(registry, probe=lambda: iterations)
    chunks = _chunks((CLEAN_TURN,), ("Second session.",), ("Third session.",))
    await driver.ingest(agent, chunks, gold_turn_ids=set())

    assert len(agent.probes) == len(chunks)
    assert len(set(agent.probes)) == 1, f"the loop polled between chunks: {agent.probes}"
    assert entry.task is not None
    assert entry.task.done(), "the spawned task was never drained"
    assert await registry.get_task(CONSOLIDATE_LOOP_NAME) is None

    spun_at_end = iterations
    for _ in range(3):
        await asyncio.sleep(0)
    assert iterations == spun_at_end, "the loop is still alive after ingest returned"


def test_loop_name_matches_the_real_arcmemory_capability() -> None:
    """The name the driver unregisters is the one arcmemory registers.

    Without this, an arcmemory rename turns the cancellation into a silent
    no-op and every other test in this file still passes.
    """
    from arcagent.modules.memory.capabilities import memory_consolidate_loop
    from arcagent.tools import BackgroundTaskMetadata

    # The decorator stamps the metadata onto the function object, which mypy
    # types as a bare ``Callable``; read the stamp off ``__dict__`` to keep the
    # assertion typed rather than silenced.
    meta: BackgroundTaskMetadata = memory_consolidate_loop.__dict__["_arc_capability_meta"]
    assert meta.name == CONSOLIDATE_LOOP_NAME
    assert meta.kind == "background_task"


async def test_loop_is_stopped_before_the_first_chunk_is_fed(driver: IngestDriver) -> None:
    """Ordering matters: a poll during chunk 1 corrupts as surely as one during 40."""
    registry = StubRegistry()
    agent = StubAgent(registry)

    await driver.ingest(agent, _chunks((CLEAN_TURN,)), gold_turn_ids=set())

    assert registry.unregistered == [("background_task", CONSOLIDATE_LOOP_NAME)]


async def test_a_loop_that_survives_unregister_aborts_the_ingest(driver: IngestDriver) -> None:
    agent = StubAgent(StubbornRegistry())

    with pytest.raises(BackgroundConsolidationError, match="still registered"):
        await driver.ingest(agent, _chunks((CLEAN_TURN,)), gold_turn_ids=set())

    assert agent.calls == [], "nothing may be fed while the loop is unaccounted for"


async def test_an_unstarted_agent_aborts_the_ingest(driver: IngestDriver) -> None:
    """No registry means startup() never ran, so the loop cannot be proven off."""
    agent = StubAgent(registry=object())
    agent._capability_registry = None

    with pytest.raises(BackgroundConsolidationError, match="startup"):
        await driver.ingest(agent, _chunks((CLEAN_TURN,)), gold_turn_ids=set())

    assert agent.calls == []


# ---------------------------------------------------------------------------
# Serial-within-a-question, and the void paths (REQ-181, REQ-205)
# ---------------------------------------------------------------------------


async def test_turns_never_overlap(driver: IngestDriver) -> None:
    """The stub raises on any second in-flight turn; a clean run proves serial."""
    chunks = _chunks((CLEAN_TURN,), ("Second.",), ("Third.",), ("Fourth.",))
    agent = StubAgent()

    report = await driver.ingest(agent, chunks, gold_turn_ids=set())

    assert report.chunks_fed == 4
    assert agent.in_flight == 0


async def test_gold_evidence_damage_voids_before_any_spend(driver: IngestDriver) -> None:
    """Screening runs the whole question before the first LLM call."""
    chunks = _chunks((CLEAN_TURN,), (INJECTION_TURN,))
    agent = StubAgent()

    with pytest.raises(GoldEvidenceFilteredError, match="gold evidence"):
        await driver.ingest(agent, chunks, gold_turn_ids={"s1t0"})

    assert agent.calls == [], "a voided question must cost nothing"
    assert GoldEvidenceFilteredError.reason == "gold_evidence_filtered"


async def test_non_gold_damage_is_a_warning_not_a_void(driver: IngestDriver) -> None:
    chunks = _chunks((INJECTION_TURN,))
    agent = StubAgent()

    report = await driver.ingest(agent, chunks, gold_turn_ids={"s0t99"})

    assert report.chunks_fed == 1
    assert len(report.warnings) == 1
    assert "0:0" in report.warnings[0]
    assert "non-gold" in report.warnings[0]


async def test_a_failure_mid_ingest_yields_no_report(driver: IngestDriver) -> None:
    """REQ-205: the marker's writer never sees a value for a half-fed workspace."""
    chunks = _chunks((CLEAN_TURN,), ("Second.",), ("Third.",))
    agent = StubAgent(fail_at=1)

    with pytest.raises(RuntimeError, match="blew up"):
        await driver.ingest(agent, chunks, gold_turn_ids=set())

    assert len(agent.calls) == 1, "the failing turn and everything after it never landed"
