"""T-870/T-871 — archival and the purge guard (COMP-022, REQ-255/REQ-256).

Deleting a workflow deletes the ability to explain what already happened. So
deletion is archival: hide it, disable its trigger, refuse new runs, and keep
the bundle, every retained version, and all history so past runs stay
renderable and the audit chain stays whole.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from arctrust import generate_keypair

from arcteam.workflow import (
    DefinitionStore,
    PurgeRefusedError,
    UnsignedWorkflowError,
    WorkflowArchivedError,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    parse_definition,
    sign_definition,
)

DOCUMENT: dict[str, Any] = {
    "workflow": {"id": "retired", "owner": "@sales", "description": "v1"},
    "trigger": {"type": "cron", "expression": "0 9 * * MON"},
    "node": [{"id": "a", "kind": "agent", "agent": "@sales"}],
}
ACTOR = "did:arc:ui:operator"


@pytest.fixture
def store(tmp_path: Path) -> DefinitionStore:
    store = DefinitionStore(tmp_path / "workflows")
    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )
    store.save_draft(
        parse_definition({**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "v2"}}),
        actor_did="did:arc:agent:sales",
        expected_version=1,
    )
    return store


def _no_runs(workflow_id: str) -> int:
    return 0


def _two_runs(workflow_id: str) -> int:
    return 2


# --- archive hides without erasing ------------------------------------------


def test_archiving_hides_the_workflow_from_the_active_list(store: DefinitionStore) -> None:
    assert store.list_ids() == ("retired",)

    store.archive("retired", actor_did=ACTOR, reason="superseded")

    assert store.list_ids() == ()
    assert store.list_ids(include_archived=True) == ("retired",)


def test_archiving_disables_the_trigger(store: DefinitionStore) -> None:
    assert store.load("retired").effective_trigger is not None

    bundle = store.archive("retired", actor_did=ACTOR)

    assert bundle.status == "archived"
    assert bundle.effective_trigger is None
    assert bundle.definition.trigger is not None


def test_archiving_refuses_new_runs(store: DefinitionStore) -> None:
    store.archive("retired", actor_did=ACTOR)

    with pytest.raises(WorkflowArchivedError):
        store.load_for_run("retired")


def test_archiving_refuses_further_edits(store: DefinitionStore) -> None:
    store.archive("retired", actor_did=ACTOR)

    with pytest.raises(WorkflowArchivedError):
        store.save_draft(
            parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=2
        )


def test_archiving_retains_the_bundle_and_every_prior_version(store: DefinitionStore) -> None:
    bundle = store.archive("retired", actor_did=ACTOR)

    assert (bundle.root / "workflow.toml").is_file()
    assert store.versions("retired") == (1,)
    assert store.load_version("retired", 1).description == "v1"
    assert store.load("retired").definition.version == 2


def test_an_archived_workflow_still_loads_so_past_runs_render(store: DefinitionStore) -> None:
    store.archive("retired", actor_did=ACTOR)

    bundle = store.load("retired")

    assert bundle.definition.id == "retired"
    assert bundle.content_hash


def test_archiving_is_audited_with_the_actor_and_reason(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = DefinitionStore(tmp_path / "workflows", audit=lambda e, p: events.append((e, p)))
    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )
    events.clear()

    store.archive("retired", actor_did=ACTOR, reason="replaced by v2 pipeline")

    assert events[0][0] == "workflow.archived"
    assert events[0][1]["actor_did"] == ACTOR
    assert events[0][1]["reason"] == "replaced by v2 pipeline"


# --- unarchive restores as a draft ------------------------------------------


def test_unarchive_restores_the_workflow_as_a_draft(store: DefinitionStore) -> None:
    store.archive("retired", actor_did=ACTOR)

    bundle = store.unarchive("retired", actor_did=ACTOR)

    assert bundle.status == "draft"
    assert store.list_ids() == ("retired",)
    assert store.load_for_run("retired").status == "draft"


def test_unarchiving_a_previously_signed_workflow_returns_it_as_a_draft(
    tmp_path: Path,
) -> None:
    """A definition that left the active set is never silently re-armed."""
    keypair = generate_keypair()
    store = DefinitionStore(tmp_path / "workflows", operator_public_key=keypair.public_key)
    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )
    sign_definition(
        store, "retired", signer_did="did:arc:operator:x", private_key=keypair.private_key
    )
    assert store.load("retired").status == "signed"
    store.archive("retired", actor_did=ACTOR)

    assert store.unarchive("retired", actor_did=ACTOR).status == "draft"


def test_unarchiving_is_audited(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = DefinitionStore(tmp_path / "workflows", audit=lambda e, p: events.append((e, p)))
    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )
    store.archive("retired", actor_did=ACTOR)
    events.clear()

    store.unarchive("retired", actor_did=ACTOR)

    assert [event for event, _ in events] == ["workflow.unarchived"]


# --- purge is guarded --------------------------------------------------------


def test_a_purge_is_refused_while_any_run_references_the_definition(
    store: DefinitionStore,
) -> None:
    with pytest.raises(PurgeRefusedError):
        store.purge("retired", actor_did=ACTOR, runs_referencing=_two_runs)

    assert store.exists("retired")


def test_a_purge_with_no_referencing_runs_removes_the_bundle(store: DefinitionStore) -> None:
    store.purge("retired", actor_did=ACTOR, runs_referencing=_no_runs)

    assert not store.exists("retired")
    assert store.list_ids(include_archived=True) == ()


def test_a_forced_purge_records_that_history_is_unrenderable(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = DefinitionStore(tmp_path / "workflows", audit=lambda e, p: events.append((e, p)))
    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )
    events.clear()

    store.purge("retired", actor_did=ACTOR, runs_referencing=_two_runs, force=True)

    assert events[0][0] == "workflow.purged"
    assert events[0][1]["history_unrenderable"] is True
    assert events[0][1]["orphaned_runs"] == 2
    assert events[0][1]["forced"] is True
    assert not store.exists("retired")


def test_a_forced_purge_proceeds_when_the_caller_owns_audit(
    store: DefinitionStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The store cannot know whether its caller records the event.

    Requiring a hook on the store looked like it enforced "never destroy
    history unrecorded", but it cannot: the control plane owns audit for every
    mutation and emits the event itself, without wiring this hook. Neither
    production construction site passes one, so the requirement made a declared
    capability (REQ-256) impossible everywhere rather than safe anywhere. It was
    also inconsistent with this store's own model — every other lifecycle event
    flows through the same optional hook with no such demand.

    The enforceable guard is the run-reference check, which stays.
    """
    with caplog.at_level(logging.WARNING, logger="arcteam.workflow.store"):
        store.purge("retired", actor_did=ACTOR, runs_referencing=_two_runs, force=True)

    assert not store.exists("retired")
    assert "no audit hook" in caplog.text


def test_a_purge_is_still_refused_by_the_run_guard_without_force(
    store: DefinitionStore,
) -> None:
    """The check the store CAN enforce is unchanged."""
    with pytest.raises(PurgeRefusedError):
        store.purge("retired", actor_did=ACTOR, runs_referencing=_two_runs)

    assert store.exists("retired")


def test_purging_an_absent_workflow_raises(store: DefinitionStore) -> None:
    with pytest.raises(WorkflowNotFoundError):
        store.purge("ghost", actor_did=ACTOR, runs_referencing=_no_runs)


def test_archiving_an_absent_workflow_raises(store: DefinitionStore) -> None:
    with pytest.raises(WorkflowNotFoundError):
        store.archive("ghost", actor_did=ACTOR)


# --- archive vs. in-flight runs ---------------------------------------------


def test_archiving_does_not_break_dispatch_for_an_already_admitted_run(
    store: DefinitionStore,
) -> None:
    """Archiving refuses NEW runs; it does not terminate live ones.

    The admission question ("may a run start?") and the integrity question
    ("are these still the bytes that were signed?") are different gates. A run
    already in flight has passed admission, so its per-node dispatch must keep
    resolving the definition while still being integrity-checked.
    """
    store.archive("retired", actor_did=ACTOR)

    bundle = store.load_for_dispatch("retired")

    assert bundle.status == "archived"
    assert bundle.definition.id == "retired"


def test_dispatch_still_fails_closed_on_drift_under_a_signature(tmp_path: Path) -> None:
    """Relaxing the archive check must not relax the integrity check."""
    keypair = generate_keypair()
    store = DefinitionStore(tmp_path / "workflows", operator_public_key=keypair.public_key)
    bundle = store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )
    sign_definition(
        store, "retired", signer_did="did:arc:operator:x", private_key=keypair.private_key
    )
    store.archive("retired", actor_did=ACTOR)

    text = (bundle.root / "workflow.toml").read_text().replace('"@sales"', '"@attacker"')
    (bundle.root / "workflow.toml").write_text(text)

    with pytest.raises(WorkflowIntegrityError):
        store.load_for_dispatch("retired")


def test_dispatch_still_refuses_an_unsigned_definition_above_personal(tmp_path: Path) -> None:
    """Relaxing the archive check must not relax the tier gate either."""
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=keypair.public_key
    )
    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
    )

    with pytest.raises(UnsignedWorkflowError):
        store.load_for_dispatch("retired")


def test_load_for_run_is_the_admission_gate_and_still_refuses_an_archived_workflow(
    store: DefinitionStore,
) -> None:
    store.archive("retired", actor_did=ACTOR)

    with pytest.raises(WorkflowArchivedError):
        store.load_for_run("retired")
