"""What an agent publishes about itself, and when (ADR-032).

Two properties decide whether the router can work at all:

* **Written at ingest, not at query time.** The router must rank the room
  without waking anyone, and it can only do that over an index that already
  exists when the question arrives.
* **Pointers, never contents.** The digest is the one artifact that crosses the
  memory privacy boundary. If bodies crossed it, a teammate could read the
  document instead of asking the agent that owns it, and every agent's private
  memory would be public by accident.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import publish_ingest_to_digest

SECRET = "the reactor runs at 400 degrees and the access code is hunter2"


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def state(tmp_path: Path) -> Any:
    _runtime.configure(
        config=make_config_dict(entity_id="agent://ops", entity_name="ops"),
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        operator_signer=make_operator_signer(),
    )
    return _runtime.state()


def _captured(text: str, kind: str = "user") -> Any:
    return SimpleNamespace(data={"text": text, "kind": kind})


async def _published(state: Any) -> list[Any]:
    digest = await state.digests.get(state.identity.did)
    return list(digest.entries) if digest is not None else []


class TestIngestPublishesAPointer:
    async def test_filing_something_makes_this_agent_findable(self, state: Any) -> None:
        await publish_ingest_to_digest(_captured(f"NNL technical requirements\n\n{SECRET}"))

        assert [entry.title for entry in await _published(state)] == ["NNL technical requirements"]

    async def test_the_rare_identifier_survives_into_the_index(self, state: Any) -> None:
        """The token the router matches on has to be in what gets published."""
        await publish_ingest_to_digest(_captured(f"NNL technical requirements\n\n{SECRET}"))

        assert "NNL" in (await _published(state))[0].entities

    async def test_the_contents_never_cross_the_boundary(self, state: Any) -> None:
        await publish_ingest_to_digest(_captured(f"NNL technical requirements\n\n{SECRET}"))

        published = (await _published(state))[0].model_dump_json()
        assert "hunter2" not in published
        assert "400 degrees" not in published

    async def test_refiling_updates_the_pointer_rather_than_adding_one(self, state: Any) -> None:
        for _ in range(3):
            await publish_ingest_to_digest(_captured(f"NNL technical requirements\n\n{SECRET}"))

        assert len(await _published(state)) == 1


class TestWhatIsNotWorthPublishing:
    @pytest.mark.parametrize("kind", ["tool", "respond"])
    async def test_working_noise_is_not_an_artifact(self, state: Any, kind: str) -> None:
        """A digest indexing tool spew ranks its owner for having been busy."""
        await publish_ingest_to_digest(_captured("tool:read -> " + SECRET, kind))

        assert await _published(state) == []

    async def test_a_remark_is_not_an_artifact(self, state: Any) -> None:
        await publish_ingest_to_digest(_captured("thanks!"))

        assert await _published(state) == []

    async def test_an_empty_payload_publishes_nothing(self, state: Any) -> None:
        await publish_ingest_to_digest(SimpleNamespace(data={}))

        assert await _published(state) == []


class TestTheWiringBetweenTheTwoModules:
    """A correct publisher with dead wiring is the bug shape this repo keeps finding.

    Memory owns private storage and must not know a team exists; messaging owns
    the team and must not reach into private memory. They meet on the bus, and
    both ends of that meeting are asserted here rather than assumed.
    """

    async def test_memory_announces_an_ingest_on_the_bus(self, tmp_path: Path) -> None:
        from arcagent.brain import NullBrain
        from arcagent.modules.memory import _runtime as memory_runtime
        from arcagent.modules.memory import capabilities as memory
        from arcagent.modules.memory.config import MemoryConfig

        emitted: list[tuple[str, dict[str, Any]]] = []

        class _Bus:
            async def emit(self, event: str, data: dict[str, Any]) -> None:
                emitted.append((event, data))

        state = memory_runtime._State(
            config=MemoryConfig(),
            brain=NullBrain(),
            workspace=tmp_path,
            telemetry=None,
            bus=_Bus(),
            agent_did="did:arc:ops",
            active=True,
        )
        memory_runtime.bind(state)
        try:
            await memory._capture(state, "NNL technical requirements", kind="user")
        finally:
            memory_runtime.reset()

        assert emitted[0][0] == "memory:captured"
        assert emitted[0][1]["kind"] == "user"

    def test_messaging_subscribes_to_that_announcement(self) -> None:
        assert publish_ingest_to_digest._arc_capability_meta.event == "memory:captured"


class TestItNeverBreaksTheWorkItIndexes:
    async def test_a_broken_store_does_not_raise(self, state: Any) -> None:
        class _Broken:
            async def add_entry(self, *_a: Any, **_kw: Any) -> None:
                raise RuntimeError("store down")

        state.digests = _Broken()

        await publish_ingest_to_digest(_captured("NNL technical requirements " + SECRET))

    async def test_an_unconfigured_store_does_not_raise(self, state: Any) -> None:
        state.digests = None

        await publish_ingest_to_digest(_captured("NNL technical requirements " + SECRET))
