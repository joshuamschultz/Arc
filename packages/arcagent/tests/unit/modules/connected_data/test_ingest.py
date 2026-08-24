"""The ingest adapter's seam to the optional memory module.

A connected-data sync runs on a background task, not inside a turn. Everything
here is about that being true and the Brain still being reachable for the right
agent — and only the right agent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.connected_data import KnowledgeHome, MappingPlan
from arcagent.extension.source import SourceDescription
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter

_DID = "did:arc:local:executor/abc"
_OTHER = "did:arc:local:executor/zzz"


def _adapter(tmp_path: Path, did: str = _DID) -> ArcMemoryIngestAdapter:
    return ArcMemoryIngestAdapter(tmp_path, did, approval_store=None)


def _source() -> SourceDescription:
    return SourceDescription(connection_id="shop", source_kind="sqlite", account_id="acct")


def _plan(*homes: KnowledgeHome) -> MappingPlan:
    return MappingPlan(mapping_id="m", homes=homes, revision="r", content_hash="h")


class _Brain:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    async def register_datastore(self, source_id: str, port: Any, *, caller_did: str) -> None:
        self.registered.append((source_id, caller_did))


class _Port:
    async def datastore_port(self) -> str:
        return "the port"


def _fake_runtime(brain: _Brain, *, registered_for: str = _DID) -> SimpleNamespace:
    """A memory runtime that answers only by DID, as a background task must."""

    def state_for(agent_did: str) -> Any:
        if agent_did != registered_for:
            raise RuntimeError(f"no memory state registered for {agent_did}")
        return SimpleNamespace(brain=brain, agent_did=agent_did, active=True)

    def state() -> Any:
        raise AssertionError("a background sync has no bound turn to resolve from")

    return SimpleNamespace(state=state, state_for=state_for)


async def test_a_datastore_attaches_from_a_sync_with_no_bound_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``state()`` resolves by the DID bound for the RUNNING TURN, and a sync is
    not a turn — it runs on a background task that is not a descendant of the one
    that bound it. So approving a datastore mapping in the dashboard failed the
    moment the sync tried to attach the database, with "MemoryIsolationError: no
    agent DID bound for the running turn". Seen on the live box.
    """
    brain = _Brain()
    monkeypatch.setitem(
        __import__("sys").modules, "arcagent.modules.memory._runtime", _fake_runtime(brain)
    )

    await _adapter(tmp_path).register_datastore(_source(), _Port(), _plan(KnowledgeHome.DATASTORE))

    assert brain.registered == [(brain.registered[0][0], _DID)]


async def test_it_attaches_for_its_own_agent_and_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming the DID must stay as strict as the ambient binding was: a sync for
    one agent must never resolve another agent's Brain."""
    brain = _Brain()
    monkeypatch.setitem(
        __import__("sys").modules,
        "arcagent.modules.memory._runtime",
        _fake_runtime(brain, registered_for=_OTHER),
    )

    with pytest.raises(RuntimeError, match=_DID):
        await _adapter(tmp_path).register_datastore(
            _source(), _Port(), _plan(KnowledgeHome.DATASTORE)
        )

    assert brain.registered == []


async def test_a_mapping_without_the_datastore_home_attaches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document mapping must not quietly wire a live database to the Brain."""
    brain = _Brain()
    monkeypatch.setitem(
        __import__("sys").modules, "arcagent.modules.memory._runtime", _fake_runtime(brain)
    )

    await _adapter(tmp_path).register_datastore(_source(), _Port(), _plan(KnowledgeHome.DOCUMENT))

    assert brain.registered == []
