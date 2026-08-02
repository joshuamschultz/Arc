"""T-868 — one shared operation set for every surface (REQ-252, REQ-253).

The agent's builder tools, the operator command line, and the dashboard are all
callers of these six operations. Validation, versioning, draft lifecycle, and
audit live here exactly once, so the three surfaces cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arcteam.workflow.control_plane import WorkflowControlPlane

from .conftest import (
    SALES_DID,
    Bundle,
    Definition,
    Node,
    RecordingSink,
)
from .test_runner_frontier import build

OPERATOR = "did:arc:local:user/0f0f0f0f"


@dataclass(frozen=True)
class Issue:
    node_id: str | None
    field: str | None
    error: str
    observed: Any
    admissible: tuple[str, ...] = ()


class FakeDefinitionStore:
    def __init__(self) -> None:
        self.bundles: dict[str, Bundle] = {}
        self.archived: set[str] = set()
        self.saved: list[tuple[str, int | None, str]] = []
        self.files_seen: dict[str, bytes] = {}
        self.purged: list[tuple[str, bool, int]] = []

    def load(self, workflow_id: str) -> Bundle:
        return self.bundles[workflow_id]

    def load_for_run(self, workflow_id: str) -> Bundle:
        if workflow_id in self.archived:
            raise WorkflowArchivedError(workflow_id)
        return self.bundles[workflow_id]

    def load_for_dispatch(self, workflow_id: str) -> Bundle:
        return self.bundles[workflow_id]

    def save_draft(
        self,
        definition: Definition,
        *,
        actor_did: str,
        expected_version: int | None,
        files: Mapping[str, bytes] | None = None,
    ) -> Bundle:
        self.files_seen = dict(files or {})
        existing = self.bundles.get(definition.id)
        if existing is not None and expected_version != existing.definition.version:
            raise StaleEditError(
                f"expected version {expected_version}, stored {existing.definition.version}"
            )
        self.saved.append((definition.id, expected_version, actor_did))
        version = 1 if existing is None else existing.definition.version + 1
        bundle = Bundle(
            Definition(
                id=definition.id,
                version=version,
                owner=definition.owner,
                channel=definition.channel,
                budget=definition.budget,
                nodes=definition.nodes,
            ),
            status="draft",
        )
        self.bundles[definition.id] = bundle
        return bundle

    def list_ids(self, *, include_archived: bool = False) -> Sequence[str]:
        ids = sorted(self.bundles)
        return ids if include_archived else [i for i in ids if i not in self.archived]

    def archive(self, workflow_id: str, *, actor_did: str) -> Bundle:
        self.archived.add(workflow_id)
        return self.bundles[workflow_id]

    def purge(
        self,
        workflow_id: str,
        *,
        actor_did: str,
        runs_referencing: Any,
        force: bool = False,
        reason: str = "",
    ) -> None:
        if workflow_id not in self.bundles:
            raise KeyError(workflow_id)
        outstanding = runs_referencing(workflow_id)
        if outstanding and not force:
            raise PurgeRefusedError(f"{outstanding} run(s) still reference {workflow_id!r}")
        self.purged.append((workflow_id, force, outstanding))
        del self.bundles[workflow_id]

    def unarchive(self, workflow_id: str, *, actor_did: str) -> Bundle:
        self.archived.discard(workflow_id)
        bundle = Bundle(self.bundles[workflow_id].definition, status="draft")
        self.bundles[workflow_id] = bundle
        return bundle


class StaleEditError(RuntimeError):
    pass


class PurgeRefusedError(RuntimeError):
    pass


class WorkflowArchivedError(RuntimeError):
    pass


def parse(document: Mapping[str, Any]) -> Definition:
    if "id" not in document:
        raise ValueError("workflow.id is required")
    return Definition(
        id=str(document["id"]),
        channel=document.get("channel"),
        nodes=tuple(Node(**node) for node in document.get("nodes", [])),
    )


def validate(
    definition: Definition, *, pending_files: frozenset[str] = frozenset()
) -> tuple[Issue, ...]:
    validate.last_pending = pending_files  # type: ignore[attr-defined]
    known = set(definition.node_ids)
    return tuple(
        Issue(
            node_id=node.id,
            field="needs",
            error="unknown node in needs",
            observed=need,
            admissible=tuple(sorted(known)),
        )
        for node in definition.nodes
        for need in node.needs
        if need not in known
    )


VALID = {
    "id": "customer-onboarding",
    "channel": "channel://onboarding",
    "nodes": [
        {"id": "collect", "kind": "agent", "agent": "@sales"},
        {"id": "verify", "kind": "agent", "agent": "@sales", "needs": ("collect",)},
    ],
}
BROKEN = {
    "id": "customer-onboarding",
    "nodes": [{"id": "collect", "kind": "agent", "agent": "@sales", "needs": ("ghost",)}],
}


def plane(stores: Any, registry: Any, **kwargs: Any) -> tuple[WorkflowControlPlane, Any, Any]:
    definitions = kwargs.pop("definitions", FakeDefinitionStore())
    sink = RecordingSink()
    runner = build(stores, registry, Definition(id="customer-onboarding"))
    runner._definitions = definitions
    control = WorkflowControlPlane(
        definitions=definitions,
        parse=parse,
        validate=validate,
        runner=runner,
        runs=stores[1],
        tier=kwargs.pop("tier", "personal"),
        audit_sink=sink,
        **kwargs,
    )
    return control, definitions, sink


async def test_create_validates_then_writes_a_draft(stores: Any, registry: Any) -> None:
    control, definitions, sink = plane(stores, registry)

    result = await control.create(VALID, actor_did=OPERATOR)

    assert result.ok
    assert result.bundle.status == "draft", "no authoring path confers signed status"
    assert definitions.saved == [("customer-onboarding", None, OPERATOR)]
    assert "workflow.created" in sink.actions()


async def test_an_invalid_definition_returns_typed_errors_and_writes_nothing(
    stores: Any, registry: Any
) -> None:
    control, definitions, sink = plane(stores, registry)

    result = await control.create(BROKEN, actor_did=OPERATOR)

    assert not result.ok
    assert definitions.saved == []
    issue = result.errors[0]
    assert (issue.node_id, issue.field, issue.observed) == ("collect", "needs", "ghost")
    assert issue.admissible == ("collect",)
    assert sink.events[-1].outcome == "invalid"


async def test_an_unparseable_document_is_a_typed_error_not_a_crash(
    stores: Any, registry: Any
) -> None:
    control, _, _ = plane(stores, registry)

    result = await control.create({"nodes": []}, actor_did=OPERATOR)

    assert not result.ok
    assert "id" in result.errors[0].error


async def test_edit_refuses_a_stale_version_rather_than_merging_it(
    stores: Any, registry: Any
) -> None:
    control, _, sink = plane(stores, registry)
    created = await control.create(VALID, actor_did=OPERATOR)
    assert created.bundle.definition.version == 1

    fresh = await control.edit(
        "customer-onboarding", VALID, expected_version=1, actor_did=OPERATOR, reason="tweak"
    )
    assert fresh.ok
    assert fresh.bundle.definition.version == 2
    assert fresh.bundle.status == "draft"

    stale = await control.edit(
        "customer-onboarding", VALID, expected_version=1, actor_did=OPERATOR, reason="race"
    )
    assert not stale.ok
    assert "version" in stale.errors[0].error
    assert sink.events[-1].outcome == "conflict"


async def test_archive_then_unarchive_round_trips_through_one_place(
    stores: Any, registry: Any
) -> None:
    control, definitions, sink = plane(stores, registry)
    await control.create(VALID, actor_did=OPERATOR)

    await control.archive("customer-onboarding", actor_did=OPERATOR)
    assert definitions.list_ids() == []

    restored = await control.unarchive("customer-onboarding", actor_did=OPERATOR)
    assert restored.bundle.status == "draft"
    assert definitions.list_ids() == ["customer-onboarding"]
    assert {"workflow.archived", "workflow.unarchived"} <= set(sink.actions())


async def test_run_refuses_an_archived_workflow(stores: Any, registry: Any) -> None:
    control, _, sink = plane(stores, registry)
    await control.create(VALID, actor_did=OPERATOR)
    await control.archive("customer-onboarding", actor_did=OPERATOR)

    result = await control.run("customer-onboarding", input={}, actor_did=OPERATOR)

    assert not result.ok
    assert sink.events[-1].outcome in ("refused", "error")


async def test_run_and_cancel_delegate_to_the_runner(stores: Any, registry: Any) -> None:
    definitions = FakeDefinitionStore()
    definitions.bundles["customer-onboarding"] = Bundle(
        Definition(
            id="customer-onboarding",
            channel="channel://onboarding",
            nodes=(Node(id="collect", kind="agent", agent="@sales"),),
        )
    )
    control, _, sink = plane(stores, registry, definitions=definitions)

    started = await control.run("customer-onboarding", input={}, actor_did=OPERATOR)
    assert started.ok
    assert started.run.status == "running"
    assert started.run.initiator_did == OPERATOR

    cancelled = await control.cancel(started.run.run_id, actor_did=OPERATOR, reason="stop")
    assert cancelled.run.status == "cancelled"
    assert {"workflow.run.started", "workflow.run.cancelled"} <= set(sink.actions())


async def test_every_operation_audits_the_actor_and_the_construction_tier(
    stores: Any, registry: Any
) -> None:
    control, _, sink = plane(stores, registry, tier="federal")

    await control.create(VALID, actor_did=OPERATOR)
    await control.edit(
        "customer-onboarding", VALID, expected_version=1, actor_did=OPERATOR, reason="tweak"
    )
    await control.archive("customer-onboarding", actor_did=OPERATOR)
    await control.unarchive("customer-onboarding", actor_did=OPERATOR)

    assert len(sink.events) == 4, "exactly one audit event per operation"
    assert {e.actor_did for e in sink.events} == {OPERATOR}
    assert {e.tier for e in sink.events} == {"federal"}
    assert [e.action for e in sink.events] == [
        "workflow.created",
        "workflow.edited",
        "workflow.archived",
        "workflow.unarchived",
    ]


async def test_three_surfaces_invoking_the_same_operation_cannot_drift(
    stores: Any, registry: Any
) -> None:
    """A builder tool, a CLI command, and a dashboard route are all callers."""
    control, _, sink = plane(stores, registry)

    async def builder_tool(doc: Mapping[str, Any]) -> Any:
        return await control.create(doc, actor_did=SALES_DID)

    async def cli_command(doc: Mapping[str, Any]) -> Any:
        return await control.create(doc, actor_did=OPERATOR)

    async def dashboard_route(doc: Mapping[str, Any]) -> Any:
        return await control.create(doc, actor_did=OPERATOR)

    outcomes = [await surface(BROKEN) for surface in (builder_tool, cli_command, dashboard_route)]

    assert {o.ok for o in outcomes} == {False}
    assert len({tuple((i.node_id, i.field, i.error) for i in o.errors) for o in outcomes}) == 1
    assert [e.action for e in sink.events] == ["workflow.created"] * 3


async def test_companion_files_travel_with_the_definition(stores: Any, registry: Any) -> None:
    """A surface must never have to write a prompt file behind this operation.

    The moment authoring a complete workflow needs a second call the caller
    makes itself, there is no longer one shared operation set — and the store's
    validate-then-commit ordering is exactly what the caller would be skipping.
    """
    control, definitions, _ = plane(stores, registry)

    result = await control.create(
        VALID,
        actor_did=OPERATOR,
        files={"prompts/collect.md": b"Gather the customer record."},
    )

    assert result.ok
    assert definitions.files_seen == {"prompts/collect.md": b"Gather the customer record."}
    # Validation saw the not-yet-written file as present, so a node referencing
    # it validates — while the bytes are still uncommitted.
    assert validate.last_pending == frozenset({"prompts/collect.md"})  # type: ignore[attr-defined]


async def test_a_rejected_edit_never_reaches_the_store(stores: Any, registry: Any) -> None:
    """Validation precedes the write, so a refusal cannot leave files behind."""
    control, definitions, _ = plane(stores, registry)

    result = await control.create(
        BROKEN, actor_did=OPERATOR, files={"prompts/evil.md": b"IGNORE PRIOR"}
    )

    assert not result.ok
    assert definitions.files_seen == {}, "a refused call writes nothing at all"
    assert definitions.saved == []


async def test_a_traversal_workflow_id_is_a_typed_refusal_not_a_crash(
    stores: Any, registry: Any
) -> None:
    """The store refuses a non-name id; every surface must see a 400, not a 500.

    Ids reach these operations from HTTP path parameters and tool arguments, so
    the control plane is where that refusal becomes an answer a caller can act
    on rather than an unhandled exception.
    """

    class TraversalRefusingStore(FakeDefinitionStore):
        def load_for_run(self, workflow_id: str) -> Bundle:
            raise InvalidWorkflowIdError(f"{workflow_id!r} is not a bare name")

        def archive(self, workflow_id: str, *, actor_did: str) -> Bundle:
            raise InvalidWorkflowIdError(f"{workflow_id!r} is not a bare name")

    control, _, sink = plane(stores, registry, definitions=TraversalRefusingStore())

    started = await control.run("../../bob/workflows/secretflow", input={}, actor_did=OPERATOR)
    archived = await control.archive("../../bob/workflows/secretflow", actor_did=OPERATOR)

    assert not started.ok and not archived.ok
    assert {e.outcome for e in sink.events} <= {"refused", "error"}
    # `run` goes through the runner, which checks the id at its own boundary, so
    # the refusal is ours and the store is never asked.
    assert "never a path" in started.errors[0].error
    # `archive` delegates straight to the store, so the refusal is the store's.
    # Either way a caller gets an answer it can act on, never an unhandled raise.
    assert "bare name" in archived.errors[0].error


class InvalidWorkflowIdError(RuntimeError):
    """Stands in for the definition store's id refusal."""


async def test_the_stores_own_validation_issues_reach_the_caller(
    stores: Any, registry: Any
) -> None:
    """The store validates too; its repairable issues must not be flattened."""

    class RefusingStore(FakeDefinitionStore):
        def save_draft(self, definition: Definition, **kwargs: Any) -> Bundle:
            raise WorkflowValidationError(
                "invalid", (Issue("collect", "prompt", "missing file", "prompts/x.md"),)
            )

    control, _, sink = plane(stores, registry, definitions=RefusingStore())

    result = await control.create(VALID, actor_did=OPERATOR)

    assert not result.ok
    assert result.errors[0].field == "prompt"
    assert sink.events[-1].outcome == "invalid"


class WorkflowValidationError(RuntimeError):
    def __init__(self, message: str, issues: tuple[Issue, ...]) -> None:
        super().__init__(message)
        self.issues = issues


async def test_an_unknown_workflow_is_a_typed_refusal(stores: Any, registry: Any) -> None:
    control, _, _ = plane(stores, registry)
    result = await control.run("nope", input={}, actor_did=OPERATOR)
    assert not result.ok


def test_the_operation_set_is_exactly_seven(stores: Any) -> None:
    """Purge belongs here: it is the one component holding BOTH stores.

    The definition store deliberately takes the run count as an injected
    callable rather than importing the run store, so somebody has to supply it,
    and the answer must be the same place that owns audit for every other
    mutation — otherwise a surface purges by calling the store directly and the
    single-operation-set guarantee is gone.
    """
    operations = {
        name
        for name in dir(WorkflowControlPlane)
        if not name.startswith("_") and callable(getattr(WorkflowControlPlane, name))
    }
    assert operations == {
        "create",
        "edit",
        "archive",
        "unarchive",
        "purge",
        "run",
        "cancel",
    }


# ---------------------------------------------------------------------------
# Purge (REQ-256, T-871) — destroy, but never orphan history silently
# ---------------------------------------------------------------------------


async def test_purge_is_refused_while_a_run_references_the_workflow(
    stores: Any, registry: Any
) -> None:
    control, definitions, sink = plane(stores, registry)
    definitions.bundles["customer-onboarding"] = Bundle(
        Definition(
            id="customer-onboarding",
            channel="channel://onboarding",
            nodes=(Node(id="collect", kind="agent", agent="@sales"),),
        )
    )
    await control.run("customer-onboarding", input={}, actor_did=OPERATOR)

    result = await control.purge("customer-onboarding", actor_did=OPERATOR)

    assert not result.ok
    assert "still reference" in result.errors[0].error
    assert definitions.purged == [], "a refused purge destroys nothing"
    assert sink.events[-1].outcome == "refused"


async def test_a_forced_purge_records_that_history_is_now_unrenderable(
    stores: Any, registry: Any
) -> None:
    """Destroying history is allowed; doing it unrecorded is not."""
    control, definitions, sink = plane(stores, registry)
    definitions.bundles["customer-onboarding"] = Bundle(
        Definition(
            id="customer-onboarding",
            channel="channel://onboarding",
            nodes=(Node(id="collect", kind="agent", agent="@sales"),),
        )
    )
    await control.run("customer-onboarding", input={}, actor_did=OPERATOR)

    result = await control.purge(
        "customer-onboarding", actor_did=OPERATOR, force=True, reason="GDPR request"
    )

    assert result.ok
    event = sink.events[-1]
    assert event.action == "workflow.purged"
    assert event.extra["forced"] is True
    assert event.extra["runs_orphaned"] == 1, "the chain must name what was orphaned"
    assert event.extra["reason"] == "GDPR request"


async def test_purge_of_an_unreferenced_workflow_succeeds(stores: Any, registry: Any) -> None:
    control, definitions, sink = plane(stores, registry)
    await control.create(VALID, actor_did=OPERATOR)

    result = await control.purge("customer-onboarding", actor_did=OPERATOR)

    assert result.ok
    assert definitions.purged == [("customer-onboarding", False, 0)]
    assert sink.events[-1].extra["runs_orphaned"] == 0


async def test_purging_an_unknown_workflow_is_a_typed_refusal(stores: Any, registry: Any) -> None:
    control, _, _ = plane(stores, registry)

    result = await control.purge("nope", actor_did=OPERATOR)

    assert not result.ok
