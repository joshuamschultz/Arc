"""A step restated across sessions is more settled than one mentioned once.

A procedure is not uniformly trustworthy. Some steps are the operator's firm
practice, restated every time the topic comes up; others were said once in
passing and may have been a one-off. Stored as bare strings they are
indistinguishable, so an agent following the card cannot tell which steps are the
method and which are a guess — and neither can the operator reading it.

Corroboration is already how this system separates a guess from a known thing for
insights (``1 - e^(-gamma*hits)``). Steps use the same curve, so three mentions
reach the same "known" bar everywhere in memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.stores.procedural import ProceduralStore
from arcmemory.types import Step


@pytest.fixture
def store(tmp_path: Path) -> ProceduralStore:
    return ProceduralStore(tmp_path)


def test_a_step_starts_at_one_mention(store: ProceduralStore) -> None:
    """First sighting is a real signal, but a weak one — it must not read as settled."""
    store.upsert("deploy", "Deploy", steps=["run the smoke tests"])

    step = store.read("deploy").steps[0]
    assert step.hits == 1
    assert 0.4 < step.confidence < 0.5, "one mention must not look like an established step"


def test_restating_a_step_raises_its_confidence(store: ProceduralStore) -> None:
    """The whole point: the more it is discussed, the more it is the method."""
    store.upsert("deploy", "Deploy", steps=["run the smoke tests"])
    first = store.read("deploy").steps[0].confidence

    store.upsert("deploy", "Deploy", steps=["run the smoke tests"])
    store.upsert("deploy", "Deploy", steps=["run the smoke tests"])

    step = store.read("deploy").steps[0]
    assert step.hits == 3
    assert step.confidence > first
    # The curve reaches .799 at three hits — "~0.8", as the gamma default documents.
    assert step.confidence >= 0.79, "three corroborations should approach the known bar"


def test_a_step_the_session_did_not_mention_keeps_its_standing(
    store: ProceduralStore,
) -> None:
    """Silence is not disagreement — omission already never deletes a step here.

    If not mentioning a step decayed it, a session that refined one part of a
    procedure would quietly erode every part it did not touch.
    """
    store.upsert("deploy", "Deploy", steps=["a", "b"])
    store.upsert("deploy", "Deploy", steps=["a"])
    store.upsert("deploy", "Deploy", steps=["a"])

    card = store.read("deploy")
    by_text = {step.text: step for step in card.steps}
    assert by_text["b"].hits == 1, "an unmentioned step was penalised"
    assert by_text["a"].hits == 3


def test_rewording_a_step_carries_its_corroboration_forward(
    store: ProceduralStore,
) -> None:
    """A step re-stated in different words is the same step, not a new one.

    Matching is already whitespace/case-insensitive for merging; confidence has to
    ride the same match or every rewording would reset the evidence to zero.
    """
    store.upsert("deploy", "Deploy", steps=["Run The Smoke Tests"])
    store.upsert("deploy", "Deploy", steps=["run the smoke   tests"])

    card = store.read("deploy")
    assert len(card.steps) == 1
    assert card.steps[0].hits == 2


def test_confidence_survives_the_markdown(store: ProceduralStore) -> None:
    """Markdown is the source of truth, so the counter has to live in the file."""
    store.upsert("deploy", "Deploy", steps=["only step"])
    store.upsert("deploy", "Deploy", steps=["only step"])

    raw = store.path_for("deploy").read_text(encoding="utf-8")
    assert "only step" in raw
    assert ProceduralStore(store._dir.parent.parent).read("deploy").steps[0].hits == 2


def test_a_card_written_before_step_confidence_still_loads(tmp_path: Path) -> None:
    """36 of these exist on a live box; a strict parser would erase every one.

    A plain numbered step carries no counter, and one mention is the honest reading
    of a step that was recorded at least once.
    """
    cards = tmp_path / "memory" / "procedures"
    cards.mkdir(parents=True)
    (cards / "legacy.md").write_text(
        "---\nslug: legacy\ntitle: Legacy\nuse_count: 4\nclassification: unclassified\n---\n\n"
        "# Legacy\n\n## Steps\n1. first thing\n2. second thing\n",
        encoding="utf-8",
    )

    card = ProceduralStore(tmp_path).read("legacy")

    assert [step.text for step in card.steps] == ["first thing", "second thing"]
    assert all(step.hits == 1 for step in card.steps)


def test_a_step_renders_as_its_text_for_every_reader(store: ProceduralStore) -> None:
    """Every surface that prints a step must keep printing the step, not a repr."""
    step = Step(text="run the smoke tests", hits=2)
    assert f"{step}" == "run the smoke tests"
    assert "run the smoke tests" in f"1. {step}"
