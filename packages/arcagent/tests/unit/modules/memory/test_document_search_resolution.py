"""P21 (J1 F1/F11/F12): the ``document_search`` tool resolves names, cites, recalls.

The model sees display names ("gmail", "Team wiki"), never the sha256 source
ids the document pools are keyed by. The tool must map one to the other, fan
out when no source is named, render provenance, and recall connected documents
proactively without repeating a document the memory recall already holds.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.modules.connected_data import _runtime as connected_runtime
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import document_search, inject_recall

_DID = "did:arc:test-agent"
_WIKI_ID = "a" * 64
_MAIL_ID = "b" * 64


class _Hit:
    def __init__(self, pointer: str, text: str, **extra: str) -> None:
        self.pointer = pointer
        self.text = text
        self.source_id = _WIKI_ID
        self.score = 1.0
        self.classification = "unclassified"
        self.title = extra.get("title", "")
        self.source_kind = extra.get("source_kind", "")
        self.url = extra.get("url", "")
        self.updated_at = extra.get("updated_at", "")


class _Brain:
    def __init__(self, hits: list[_Hit] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._hits = hits if hits is not None else [_Hit("memory/connected/x/1.md", "found it")]

    async def document_search(self, query: str, **kwargs: Any) -> list[_Hit]:
        self.calls.append({"query": query, **kwargs})
        return self._hits

    async def retrieve(self, *args: Any, **kwargs: Any) -> str:
        return ""


def _status(connection_id: str, kind: str, name: str, source_id: str) -> Any:
    return SimpleNamespace(
        connection_id=connection_id,
        source_id=source_id,
        status="synced",
        description=SimpleNamespace(source_kind=kind, display_name=name),
    )


class _Service:
    async def list_sources(self) -> list[Any]:
        return [
            _status("c-wiki", "confluence", "Team wiki", _WIKI_ID),
            _status("c-mail", "gmail", "Josh mail", _MAIL_ID),
        ]

    async def guide_context(self, *, source_ids: Any = None, run_key: str) -> str:
        """No operator guides written for these sources."""
        return ""


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    _runtime.reset()
    monkeypatch.setattr(connected_runtime, "state", lambda: SimpleNamespace(service=_Service()))
    yield
    _runtime.reset()


def _install(brain: Any) -> None:
    from arcagent.modules.memory.config import MemoryConfig

    _runtime.bind(
        _runtime._State(
            config=MemoryConfig(),
            brain=brain,
            workspace=Path("."),
            telemetry=None,
            bus=None,
            agent_did=_DID,
            active=True,
        )
    )


async def test_no_source_searches_every_pool() -> None:
    brain = _Brain()
    _install(brain)

    out = await document_search("roadmap")

    assert brain.calls[0].get("source_id") is None
    assert brain.calls[0].get("source_ids") is None
    assert "found it" in out


@pytest.mark.parametrize("name", ["Team wiki", "team wiki", "confluence"])
async def test_a_display_name_or_kind_resolves_to_the_source_id(name: str) -> None:
    brain = _Brain()
    _install(brain)

    await document_search("roadmap", source=name)

    assert brain.calls[0]["source_ids"] == [_WIKI_ID]


async def test_a_raw_source_id_passes_through() -> None:
    brain = _Brain()
    _install(brain)

    await document_search("roadmap", source=_MAIL_ID)

    assert brain.calls[0]["source_ids"] == [_MAIL_ID]


async def test_an_unknown_name_says_what_is_available_and_searches_nothing() -> None:
    brain = _Brain()
    _install(brain)

    out = await document_search("roadmap", source="sharepoint")

    assert brain.calls == []
    assert "Team wiki" in out
    assert "gmail" in out


async def test_hits_render_title_kind_link_and_updated_time() -> None:
    hit = _Hit(
        "memory/connected/x/1.md",
        "the plan",
        title="Q3 Plan",
        source_kind="confluence",
        url="https://wiki.example.com/p/1",
        updated_at="2026-09-30T12:00:00Z",
    )
    _install(_Brain([hit]))

    out = await document_search("plan")

    for expected in ("Q3 Plan", "confluence", "https://wiki.example.com/p/1", "2026-09-30"):
        assert expected in out


async def test_proactive_recall_surfaces_connected_documents_once() -> None:
    brain = _Brain(
        [
            _Hit("memory/connected/x/1.md", "the plan", title="Q3 Plan"),
            _Hit("memory/connected/x/2.md", "the other", title="Other"),
        ]
    )
    _install(brain)
    ctx = SimpleNamespace(data={"query": "what is the plan", "sections": {}})

    await inject_recall(ctx)
    await inject_recall(ctx)

    assert len(brain.calls) == 1, "the same query in one turn must not search twice"
    recall = ctx.data["sections"]["recall"]
    assert "Q3 Plan" in recall
    assert recall.count("memory/connected/x/1.md") == 1


async def test_proactive_recall_is_bounded() -> None:
    hits = [_Hit(f"memory/connected/x/{n}.md", f"doc {n}", title=f"T{n}") for n in range(20)]
    brain = _Brain(hits)
    _install(brain)
    ctx = SimpleNamespace(data={"query": "anything", "sections": {}})

    await inject_recall(ctx)

    assert brain.calls[0]["top_k"] <= 5
    assert ctx.data["sections"]["recall"].count("memory/connected/x/") <= 5
