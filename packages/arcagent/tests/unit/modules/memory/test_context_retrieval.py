"""``ContextRetrieval``: the turn's one pre-model retrieval returns candidates.

It replaced three bus hooks (assembly-time recall, the user-turn proactive
moments and the pre-respond insight). Each source is gated, best-effort and
traced; the agent core owns ranking, caps and rendering.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.modules.connected_data import _runtime as connected_runtime
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import ContextRetrieval
from arcagent.modules.memory.config import MemoryConfig

_DID = "did:arc:test-agent"


def _card(
    source: str, content: str, *, score: float = 0.8, classification: str = "unclassified"
) -> Any:
    return SimpleNamespace(
        source=source, kind="episode", score=score, classification=classification, content=content
    )


def _hit(pointer: str, text: str, *, source_id: str = "a" * 64, score: float = 0.9) -> Any:
    return SimpleNamespace(
        pointer=pointer,
        text=text,
        source_id=source_id,
        score=score,
        classification="unclassified",
        title=pointer.rsplit("/", 1)[-1],
        source_kind="",
        url="",
        updated_at="",
    )


class _Brain:
    """Records the Brain calls the retrieval makes; every result is canned."""

    def __init__(
        self,
        *,
        cards: list[Any] | None = None,
        hits: list[Any] | None = None,
        allow: bool = True,
        recall_error: Exception | None = None,
    ) -> None:
        self.recalls: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []
        self.authorized: list[str] = []
        self._cards = cards or []
        self._hits = hits or []
        self._allow = allow
        self._recall_error = recall_error

    async def authorize(self, operation: str, *, caller_did: str = "") -> bool:
        self.authorized.append(operation)
        return self._allow

    async def recall(self, query: str, **kwargs: Any) -> list[Any]:
        self.recalls.append({"query": query, **kwargs})
        if self._recall_error is not None:
            raise self._recall_error
        return self._cards

    async def document_search(self, query: str, **kwargs: Any) -> list[Any]:
        self.searches.append({"query": query, **kwargs})
        return self._hits


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Any:
    _runtime.reset()

    def _no_connected_data() -> Any:
        raise RuntimeError("connected_data not bound")

    monkeypatch.setattr(connected_runtime, "state", _no_connected_data)
    yield
    _runtime.reset()


def _install(brain: Any, *, workspace: Path = Path(".")) -> _runtime._State:
    state = _runtime._State(
        config=MemoryConfig(),
        brain=brain,
        workspace=workspace,
        telemetry=None,
        bus=None,
        agent_did=_DID,
        active=not isinstance(brain, NullBrain),
    )
    _runtime.bind(state)
    return state


async def _retrieve(query: str = "who owns payments") -> dict[str, Any]:
    return dict(await ContextRetrieval().retrieve(query, memory_top_k=4, docs_top_k=3))


def _by_kind(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [c for c in result["candidates"] if c["source_kind"] == kind]


def _step(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in result["steps"] if s["name"] == name)


async def test_memory_candidates_are_gated_and_never_index_the_corpus() -> None:
    brain = _Brain(cards=[_card("episode:1", "Ada owns the payments service")])
    _install(brain)

    result = await _retrieve()

    (candidate,) = _by_kind(result, "memory")
    assert candidate["text"] == "Ada owns the payments service"
    assert candidate["source"] == "episode:1"
    assert brain.recalls[0]["index"] is False, "a turn embeds only its query"
    assert brain.recalls[0]["clearance"] == "unclassified"
    assert brain.recalls[0]["top_k"] == 4
    assert brain.authorized == ["memory.search"]


async def test_an_acl_veto_blocks_the_memory_step_before_the_brain() -> None:
    brain = _Brain(cards=[_card("episode:1", "secret")], allow=False)
    _install(brain)

    result = await _retrieve()

    assert brain.recalls == []
    assert _by_kind(result, "memory") == []
    assert _step(result, "memory")["status"] == "ok"


async def test_approved_profile_facts_surface_and_pending_ones_do_not(tmp_path: Path) -> None:
    pytest.importorskip("arcmemory")
    from arcmemory.profile import ProfileFactKind, ProfileReviewStore
    from arcmemory.types import Provenance

    reviews = ProfileReviewStore(tmp_path, agent_did=_DID)
    approved = await reviews.submit(
        profile_id=_DID,
        field="role",
        value="payments lead",
        kind=ProfileFactKind.STATIC,
        provenance=Provenance(source="crm", external_id="1"),
    )
    await reviews.approve(approved.fact_id)
    await reviews.submit(
        profile_id=_DID,
        field="secret_plan",
        value="unreviewed guess",
        kind=ProfileFactKind.INFERRED,
        provenance=Provenance(source="mail", external_id="2"),
    )
    _install(_Brain(), workspace=tmp_path)

    result = await _retrieve()

    texts = [c["text"] for c in _by_kind(result, "profile")]
    assert texts == ["role: payments lead"]


async def test_connected_documents_surface_as_connection_candidates() -> None:
    brain = _Brain(hits=[_hit("memory/connected/x/1.md", "the Q3 plan", score=0.7)])
    _install(brain)

    result = await _retrieve("what is the plan")

    (candidate,) = _by_kind(result, "connection")
    assert candidate["path"] == "memory/connected/x/1.md"
    assert "the Q3 plan" in candidate["text"]
    assert candidate["score"] == pytest.approx(0.7)
    assert brain.searches[0]["top_k"] == 3
    assert brain.searches[0]["caller_did"] == _DID


async def test_shared_store_hits_merge_into_the_connection_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        async def shared_document_search(self, query: str, **kwargs: Any) -> list[Any]:
            assert kwargs["caller_did"] == _DID
            return [_hit("shared/wiki/9.md", "shared runbook", source_id="b" * 64, score=0.95)]

    monkeypatch.setattr(connected_runtime, "state", lambda: SimpleNamespace(service=_Service()))
    _install(_Brain(hits=[_hit("memory/connected/x/1.md", "own doc", score=0.5)]))

    result = await _retrieve()

    paths = [c["path"] for c in _by_kind(result, "connection")]
    assert paths == ["shared/wiki/9.md", "memory/connected/x/1.md"]


async def test_the_proactive_buffer_is_drained_into_candidates_once() -> None:
    state = _install(_Brain())
    state.proactive_buffer.append("<memory-result>staged by a loop moment</memory-result>")

    first = await _retrieve()
    second = await _retrieve()

    assert [c["text"] for c in _by_kind(first, "memory")] == [
        "<memory-result>staged by a loop moment</memory-result>"
    ]
    assert _by_kind(second, "memory") == []
    assert state.proactive_buffer == []


async def test_an_inactive_brain_returns_nothing() -> None:
    _install(NullBrain())

    assert await _retrieve() == {"candidates": [], "steps": []}


async def test_a_blank_query_returns_nothing() -> None:
    brain = _Brain(cards=[_card("episode:1", "x")])
    _install(brain)

    result = dict(await ContextRetrieval().retrieve("  ", memory_top_k=4, docs_top_k=3))

    assert result == {"candidates": [], "steps": []}
    assert brain.recalls == []


async def test_a_failing_step_is_marked_error_and_the_others_still_return() -> None:
    brain = _Brain(
        recall_error=RuntimeError("index unavailable"),
        hits=[_hit("memory/connected/x/1.md", "still found")],
    )
    _install(brain)

    result = await _retrieve()

    assert _step(result, "memory")["status"] == "error"
    assert _step(result, "connections")["status"] == "ok"
    assert [c["path"] for c in _by_kind(result, "connection")] == ["memory/connected/x/1.md"]
