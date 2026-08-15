"""Procedures must be reachable by the agent, and their use count must mean use.

Procedures are the most operationally valuable thing arcmemory holds — they are
how the operator wants work done — and on a live agent they were unreachable and
unmeasured. The listing and read tools existed, but only inside the consolidation
ReAct loop; the running agent's only memory tool was ``memory_search``, so a
procedure competed as plain text against 85 entity cards and the agent answered
from a skill instead.

``use_count`` compounded it. It incremented on WRITE — every re-distillation of
the same procedure — so a live store of 36 procedures read 28 at 1 and 8 at 2,
and none of it was evidence of use, because nothing recorded use at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.stores.procedural import ProceduralStore
from arcmemory.types import Procedure


@pytest.fixture
def store(tmp_path: Path) -> ProceduralStore:
    return ProceduralStore(tmp_path)


def test_listing_names_every_procedure_and_its_trigger(store: ProceduralStore) -> None:
    """The index the agent scans: enough to choose one, small enough to always afford.

    Thirty-six full procedures will not fit in a turn's context, which is why the
    agent needs a listing that carries only the trigger — the field that decides
    whether a procedure is relevant at all.
    """
    store.upsert("deploy", "Deploy the fleet", when_to_use="Shipping to production", steps=["a"])
    store.upsert("onboard", "Onboard a client", when_to_use="A new logo signs", steps=["b"])

    listed = store.list_summaries()

    assert {entry.slug for entry in listed} == {"deploy", "onboard"}
    deploy = next(entry for entry in listed if entry.slug == "deploy")
    assert deploy.title == "Deploy the fleet"
    assert deploy.when_to_use == "Shipping to production"


def test_reading_a_procedure_counts_as_using_it(store: ProceduralStore) -> None:
    """Use is what ``use_count`` must measure — otherwise it measures nothing."""
    store.upsert("deploy", "Deploy the fleet", steps=["a"])
    assert store.read("deploy").use_count == 0, "writing a procedure is not using it"

    store.increment_use("deploy")
    store.increment_use("deploy")

    assert store.read("deploy").use_count == 2


def test_rewriting_a_procedure_is_a_revision_not_a_use(store: ProceduralStore) -> None:
    """The old counter conflated the two, so a much-refined procedure looked much-used.

    Both numbers are worth having and they answer different questions: how settled
    is this playbook, and how often is it actually reached for.
    """
    store.upsert("deploy", "Deploy the fleet", steps=["a"])
    store.upsert("deploy", "Deploy the fleet", steps=["a", "b"])
    store.upsert("deploy", "Deploy the fleet", steps=["a", "b", "c"])

    card = store.read("deploy")
    assert card.revisions == 3
    assert card.use_count == 0, "three rewrites are not three uses"


def test_using_a_procedure_does_not_disturb_its_steps(store: ProceduralStore) -> None:
    """Recording a use must not be a chance to lose the content."""
    store.upsert("deploy", "Deploy the fleet", when_to_use="Shipping", steps=["a", "b"])

    store.increment_use("deploy")

    card = store.read("deploy")
    assert card.step_texts == ["a", "b"]
    assert card.when_to_use == "Shipping"
    assert card.title == "Deploy the fleet"


def test_using_an_absent_procedure_is_a_no_op(store: ProceduralStore) -> None:
    """A stale slug must not mint an empty card that then shows up in the listing."""
    assert store.increment_use("nope") == 0
    assert store.list_summaries() == []


def test_an_existing_card_without_the_counter_still_loads(tmp_path: Path) -> None:
    """Cards written before revisions existed must keep working, and keep their steps.

    There are 36 of these on a live box. A parser that required the new field would
    read every one of them as absent — the operator's accumulated playbooks, gone.
    """
    cards = tmp_path / "memory" / "procedures"
    cards.mkdir(parents=True)
    (cards / "legacy.md").write_text(
        "---\nslug: legacy\ntitle: Legacy\nwhen_to_use: Old card\nuse_count: 4\n"
        "classification: unclassified\n---\n\n# Legacy\n\n## Steps\n1. first\n2. second\n",
        encoding="utf-8",
    )

    card = ProceduralStore(tmp_path).read("legacy")

    assert card is not None
    assert card.step_texts == ["first", "second"]
    assert card.use_count == 4
    assert card.revisions == 0


def test_a_procedure_round_trips_through_disk(store: ProceduralStore) -> None:
    """Both counters must survive the markdown, which is the source of truth."""
    store.write(Procedure(slug="p", title="P", steps=["one"], use_count=7, revisions=3))

    card = store.read("p")
    assert card.use_count == 7
    assert card.revisions == 3
