"""T-023 — procedural + insight cards round-trip to/from markdown."""

from __future__ import annotations

from pathlib import Path

from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.types import Confidence, Insight, Procedure


def test_procedure_round_trip_with_use_count(workspace: Path) -> None:
    store = ProceduralStore(workspace)
    store.write(Procedure(slug="deploy", title="Deploy", steps=["build", "ship"], use_count=1))
    assert store.increment_use("deploy") == 2

    loaded = store.read("deploy")
    assert loaded is not None
    assert loaded.steps == ["build", "ship"]
    assert loaded.use_count == 2


def test_procedure_slugs_lists_sorted_ids(workspace: Path) -> None:
    store = ProceduralStore(workspace)
    store.write(Procedure(slug="zulu", title="Zulu", steps=["a"]))
    store.write(Procedure(slug="alpha", title="Alpha", steps=["b"]))
    assert store.slugs() == ["alpha", "zulu"]


def test_procedure_slugs_empty_when_dir_absent(workspace: Path) -> None:
    assert ProceduralStore(workspace).slugs() == []


def test_insight_round_trip_carries_trigger_cues_instances(workspace: Path) -> None:
    store = InsightStore(workspace)
    ins = Insight(
        id="producers-unwired",
        statement="a claimed property whose producer is never traced is a silent no-op",
        trigger="predicate exists but producer never traced",
        cues=["claims-property", "predicate-without-producer"],
        instances=["event:1", "event:2"],
        confidence=0.8,
        salience=0.5,
        status=Confidence.KNOWN,
        hits=3,
    )
    store.write(ins)
    loaded = store.read("producers-unwired")

    assert loaded == ins
    assert store.all_ids() == ["producers-unwired"]
