"""Per-folder OKF ``index.md``: every folder indexed, incrementally, off the turn path."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from arcokf import validate

import arcmemory.collection_index as collection_index
from arcmemory.collection_index import (
    OkfIndexMaintainer,
    memory_maintainer,
    refresh_memory_document,
)
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.daily import DailyNotesStore
from arcmemory.stores.events import EventStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import DaySummary, Insight, Procedure, Step


@pytest.fixture
def memory(workspace: Path, db: MemoryDB) -> Path:
    return workspace / "memory"


@pytest.fixture
def entities(workspace: Path, db: MemoryDB) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db, MemoryConfig()), "scope")


@pytest.fixture(autouse=True)
def _no_background_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests drain explicitly; the debounce task must never race them."""
    monkeypatch.setattr(collection_index, "DEBOUNCE_S", 3600.0)


def _entity(store: SemanticStore, slug: str, **kwargs: str) -> None:
    store.write_fact(slug, "role", f"{slug} role", name=slug.title(), **kwargs)


def _mtimes(memory: Path) -> dict[str, int]:
    return {
        path.relative_to(memory).as_posix(): path.stat().st_mtime_ns
        for path in memory.rglob("index.md")
    }


def test_each_memory_subfolder_has_index_after_write(
    workspace: Path, memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice", entity_type="person")
    InsightStore(workspace).write(Insight(id="i1", statement="s", trigger="when x"))
    ProceduralStore(workspace).write(
        Procedure(slug="p1", title="Deploy", when_to_use="to ship", steps=[Step(text="run")])
    )
    EventStore(workspace).upsert("meeting", "Kickoff meeting", date="2026-10-01")
    DailyNotesStore(workspace).write(DaySummary(day="2026-10-01", timeline=["met"]))

    memory_maintainer(memory).drain_sync()

    for folder in ("entities", "insights", "procedures", "events", "daily-log"):
        assert (memory / folder / "index.md").is_file(), folder
        assert memory_maintainer(memory).verify(memory / folder), folder
    root = (memory / "index.md").read_text(encoding="utf-8")
    assert root.startswith('---\nokf_version: "0.2"\n---\n')
    assert "[entities/](entities/index.md) - 1 doc" in root
    assert memory_maintainer(memory).verify()


def test_one_write_rewrites_only_its_folder_and_root_when_counts_change(
    workspace: Path, memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice")
    InsightStore(workspace).write(Insight(id="i1", statement="s", trigger="t"))
    maintainer = memory_maintainer(memory)
    maintainer.drain_sync()
    before = _mtimes(memory)

    _entity(entities, "bob")  # new document: entities count 1 -> 2
    maintainer.drain_sync()
    after = _mtimes(memory)
    assert after["entities/index.md"] != before["entities/index.md"]
    assert after["index.md"] != before["index.md"]  # the root's summary line changed
    assert after["insights/index.md"] == before["insights/index.md"]

    entities.write_fact("bob", "city", "Denver")  # same count, edited document
    maintainer.drain_sync()
    again = _mtimes(memory)
    assert again["index.md"] == after["index.md"]  # count unchanged: root untouched
    assert again["insights/index.md"] == after["insights/index.md"]


def test_unchanged_folders_are_never_rewritten(memory: Path, entities: SemanticStore) -> None:
    _entity(entities, "alice")
    maintainer = memory_maintainer(memory)
    maintainer.drain_sync()
    before = _mtimes(memory)
    refresh_memory_document(memory / "entities" / "alice.md")
    maintainer.drain_sync()
    assert _mtimes(memory) == before


@pytest.mark.asyncio
async def test_write_path_does_no_index_io(
    memory: Path, entities: SemanticStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    to_thread_calls = 0
    real_to_thread = asyncio.to_thread

    async def spy(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal to_thread_calls
        to_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    regenerated: list[str] = []
    real_regenerate = OkfIndexMaintainer._regenerate
    monkeypatch.setattr(asyncio, "to_thread", spy)
    monkeypatch.setattr(
        OkfIndexMaintainer,
        "_regenerate",
        lambda self, rel, **kw: (regenerated.append(rel), real_regenerate(self, rel, **kw))[1],
    )

    for i in range(1000):
        entities.write_fact(f"person-{i}", "role", "engineer")

    assert to_thread_calls == 0
    assert regenerated == []  # nothing rendered on the turn path
    assert not (memory / "entities" / "index.md").exists()
    # The journal holds ONE line for the folder, however many writes hit it.
    assert (memory / ".dirty").read_text(encoding="utf-8").splitlines() == ["entities"]


@pytest.mark.asyncio
async def test_debounced_background_regeneration_bounds_writes(
    memory: Path, entities: SemanticStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collection_index, "DEBOUNCE_S", 0.05)
    maintainer = OkfIndexMaintainer(memory, nested=frozenset({"connected"}), debounce_s=0.05)
    monkeypatch.setitem(collection_index._REGISTRY, str(memory.absolute()), maintainer)
    regenerated: list[str] = []
    real = OkfIndexMaintainer._regenerate
    monkeypatch.setattr(
        OkfIndexMaintainer,
        "_regenerate",
        lambda s, rel, **kw: (regenerated.append(rel), real(s, rel, **kw))[1],
    )

    for i in range(200):
        _entity(entities, f"person-{i}")
    await asyncio.sleep(0.4)

    assert regenerated.count("entities") <= 3  # a burst collapses into few passes
    assert maintainer.verify(memory / "entities", deep=True)
    index = (memory / "entities" / "index.md").read_text(encoding="utf-8")
    assert index.count("\n* [") == 200


def test_crash_between_write_and_index_replays_journal(
    memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice")
    assert (memory / ".dirty").is_file()
    assert not (memory / "entities" / "index.md").exists()

    # "Restart": a brand-new maintainer adopts the journal and heals.
    fresh = OkfIndexMaintainer(memory, nested=frozenset({"connected"}))
    fresh.drain_sync()

    assert fresh.verify(memory / "entities", deep=True)
    assert fresh.verify()
    assert not (memory / ".dirty").exists()


def test_edit_behind_our_back_fails_closed_then_sync_repairs(
    memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice")
    maintainer = memory_maintainer(memory)
    maintainer.drain_sync()
    index = memory / "entities" / "index.md"
    index.write_text(index.read_text(encoding="utf-8") + "* [evil](evil.md)\n", encoding="utf-8")
    assert not maintainer.verify(memory / "entities")

    maintainer.sync_all()

    assert maintainer.verify(memory / "entities", deep=True)
    assert "evil" not in index.read_text(encoding="utf-8")


def test_document_edited_behind_our_back_fails_deep_and_sync_repairs(
    memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice")
    maintainer = memory_maintainer(memory)
    maintainer.drain_sync()
    card = memory / "entities" / "alice.md"
    card.write_text(card.read_text(encoding="utf-8") + "\nsneaky edit\n", encoding="utf-8")
    assert not maintainer.verify(memory / "entities", deep=True)

    maintainer.sync_all()

    assert maintainer.verify(memory / "entities", deep=True)


def test_sync_all_leaves_healthy_folders_alone_and_heals_a_legacy_index(
    memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice")
    memory.mkdir(exist_ok=True)
    (memory / "index.md").write_text("# Collection Index\n\nlegacy flat index\n", encoding="utf-8")
    maintainer = memory_maintainer(memory)

    assert maintainer.sync_all() >= 2  # entities + the replaced legacy root
    assert maintainer.verify()
    before = _mtimes(memory)
    assert maintainer.sync_all() == 0
    assert _mtimes(memory) == before


def test_format_matches_spec(memory: Path, entities: SemanticStore) -> None:
    _entity(entities, "alice", entity_type="person")
    InsightStore(memory.parent).write(Insight(id="i1", statement="a thesis", trigger="when x"))
    memory_maintainer(memory).drain_sync()

    root = (memory / "index.md").read_text(encoding="utf-8")
    folder = (memory / "entities" / "index.md").read_text(encoding="utf-8")
    assert validate(root, path="index.md", bundle_root=True).valid
    assert validate(folder, path="entities/index.md").valid
    assert not folder.startswith("---")  # only the root carries frontmatter
    assert root.count("---\n") == 2 and "okf_version" in root.split("---\n")[1]
    assert "# Folders\n* [entities/](entities/index.md) - 1 doc" in root
    assert folder.startswith("# Entity\n* [Alice](alice.md) - ")
    assert "sha256" not in root + folder
    assert (memory / "entities" / ".index.digest").is_file()
    insights = (memory / "insights" / "index.md").read_text(encoding="utf-8")
    assert insights.startswith("# Insight\n* [i1](i1.md) - ")


def test_classified_docs_are_listed_with_label(memory: Path, entities: SemanticStore) -> None:
    _entity(entities, "plain")
    _entity(entities, "plan", classification="secret")
    memory_maintainer(memory).drain_sync()

    index = (memory / "entities" / "index.md").read_text(encoding="utf-8")
    assert "[Plan](plan.md)" in index
    assert "(classification: secret)" in index
    assert "[Plain](plain.md)" in index


def test_cards_carry_real_type_title_and_description(
    workspace: Path, memory: Path, entities: SemanticStore
) -> None:
    from arcokf import parse

    _entity(entities, "alice", entity_type="person")
    InsightStore(workspace).write(Insight(id="i1", statement="s", trigger="when x"))
    ProceduralStore(workspace).write(
        Procedure(slug="p1", title="Deploy", when_to_use="to ship", steps=[Step(text="run")])
    )
    EventStore(workspace).upsert("meeting", "Kickoff meeting", date="2026-10-01")
    DailyNotesStore(workspace).write(DaySummary(day="2026-10-01", timeline=["met"]))

    kinds = {
        "entities/alice.md": "Entity",
        "insights/i1.md": "Insight",
        "procedures/p1.md": "Procedure",
        "events/meeting.md": "Event",
        "daily-log/2026-10-01.md": "DailyLog",
    }
    for rel, kind in kinds.items():
        meta = parse((memory / rel).read_text(encoding="utf-8")).metadata
        assert meta["type"] == kind, rel
        assert meta["title"], rel
        assert meta["description"], rel
        assert meta["generated"]["by"] == "process:arcmemory", rel


def test_ingesting_n_objects_does_linear_index_work(
    memory: Path, entities: SemanticStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """index.md was once rebuilt per object and pinned the loop: it must stay O(N)."""
    reads = 0
    real = collection_index.folder_entry

    def counting(path: Path):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return real(path)

    monkeypatch.setattr(collection_index, "folder_entry", counting)
    maintainer = memory_maintainer(memory)
    count = 300
    for batch in range(count // 10):
        for i in range(10):
            _entity(entities, f"person-{batch * 10 + i}")
        maintainer.drain_sync()  # worst case: a drain after every batch of ten

    # A quadratic maintainer re-reads every prior document on every drain
    # (~ N^2 / 20 = 4,500 reads); the stat-keyed cache reads each document once.
    assert reads <= count + 10
    assert maintainer.verify(memory / "entities", deep=True)


def test_hidden_and_operational_folders_are_never_indexed(
    memory: Path, entities: SemanticStore
) -> None:
    _entity(entities, "alice")
    (memory / ".pruned").mkdir()
    (memory / ".pruned" / "old.md").write_text("---\ntype: Entity\n---\nx\n", encoding="utf-8")
    memory_maintainer(memory).sync_all()
    assert not (memory / ".pruned" / "index.md").exists()
    assert ".pruned" not in (memory / "index.md").read_text(encoding="utf-8")


def test_symlinked_documents_are_not_listed(
    memory: Path, entities: SemanticStore, tmp_path: Path
) -> None:
    _entity(entities, "alice")
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: Entity\ntitle: Outside\n---\nsecret\n", encoding="utf-8")
    os.symlink(outside, memory / "entities" / "link.md")
    memory_maintainer(memory).sync_all()
    assert "Outside" not in (memory / "entities" / "index.md").read_text(encoding="utf-8")


async def test_refresh_index_backfills_a_workspace_that_never_had_folder_indexes(
    workspace: Path, memory: Path
) -> None:
    """The operator backfill: cards written before per-folder indexes get theirs."""
    from arcmemory.brain import ArcMemoryBrain
    from arcmemory.mdfile import atomic_write_text, render_document

    card = memory / "entities" / "legacy.md"
    atomic_write_text(
        card, render_document({"entity_type": "person", "name": "Legacy"}, "# Legacy")
    )
    (memory / "index.md").write_text("# Collection Index\n\nold flat index\n", encoding="utf-8")
    brain = ArcMemoryBrain(workspace, agent_did="did:arc:test-agent")

    await brain.refresh_index()

    maintainer = memory_maintainer(memory)
    assert maintainer.verify(memory / "entities", deep=True)
    assert maintainer.verify(memory, deep=True)
    assert "[Legacy](legacy.md)" in (memory / "entities" / "index.md").read_text(encoding="utf-8")
