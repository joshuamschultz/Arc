"""T-1343 — start_run validates input for every trigger kind and records arc_trigger.

SPEC-088 COMP-008 (REQ-607, REQ-609), D-843. ``start_run`` takes a required
``trigger: TriggerContext``; it picks the governing schema (the fired event's
schema, else the workflow input schema), validates the input through SchemaCheck
BEFORE the Run row exists, then stores ``arc_trigger = {"kind", "name"}`` on the
run input.

The definition is the REAL parsed ``WorkflowDefinition`` and the bundle carries a
real ``root`` with real schema files, so the runner reads schemas exactly as it
does from a verified ``WorkflowBundle``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from arcteam.workflow import WorkflowDefinition, parse_definition
from arcteam.workflow.runner import (
    UnsignedWorkflowRefusedError,
    WorkflowRunError,
    WorkflowRunner,
)
from arcteam.workflow.runner_contracts import TriggerContext

from .conftest import FlowRunStore, RecordingSink
from .test_runner_frontier import FakeDefinitions, build

WORKFLOW_ID = "order-intake"
OPERATOR = "did:arc:local:user/0f0f0f0f"
SCHEDULER = "did:arc:local:agent/1111aaaa"

_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"customer": {"type": "string"}},
    "required": ["customer"],
}
_EVENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
}


@dataclass(frozen=True)
class RootedBundle:
    """A bundle double with the ``root`` a real ``WorkflowBundle`` carries."""

    definition: WorkflowDefinition
    root: Path
    status: str = "signed"
    signer_did: str | None = "did:arc:local:operator/0f0f0f0f"
    content_hash: str = "sha256:order-intake"

    @property
    def is_verified(self) -> bool:
        return self.signer_did is not None


class SpyRunStore(FlowRunStore):
    """Records every ``create_run`` so a test can prove refusal came first."""

    def __init__(self, backend: Any) -> None:
        super().__init__(backend)
        self.create_calls: list[Mapping[str, Any]] = []

    async def create_run(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return await super().create_run(**kwargs)


def _bundle_root(tmp_path: Path) -> Path:
    root = tmp_path / WORKFLOW_ID
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "input.json").write_text(json.dumps(_INPUT_SCHEMA))
    (root / "schemas" / "order_placed.json").write_text(json.dumps(_EVENT_SCHEMA))
    return root


def _definition(*, with_input: bool = True) -> WorkflowDefinition:
    document: dict[str, Any] = {
        "workflow": {"id": WORKFLOW_ID, "owner": "@sales"},
        "event": [{"name": "order_placed", "schema": "schemas/order_placed.json"}],
        "node": [{"id": "only", "kind": "agent", "agent": "@sales"}],
    }
    if with_input:
        document["input"] = {"schema": "schemas/input.json"}
    return parse_definition(document)


def _runner(
    stores: Any,
    registry: Any,
    tmp_path: Path,
    *,
    signer_did: str | None = "did:arc:local:operator/0f0f0f0f",
    with_input: bool = True,
    sink: RecordingSink | None = None,
) -> tuple[WorkflowRunner, SpyRunStore]:
    flow_tasks, runs, tasks = stores
    spy = SpyRunStore(runs._backend)
    definition = _definition(with_input=with_input)
    bundle = RootedBundle(definition, _bundle_root(tmp_path), signer_did=signer_did)
    runner = build(
        (flow_tasks, spy, tasks),
        registry,
        definition,  # type: ignore[arg-type]  # build() is typed for the conftest double
        definitions=FakeDefinitions(bundle),  # type: ignore[arg-type]  # same double seam
        audit_sink=sink or RecordingSink(),
    )
    return runner, spy


# --- the signature: trigger is required, no legacy default ----------------------


def test_start_run_requires_a_trigger_context() -> None:
    parameter = inspect.signature(WorkflowRunner.start_run).parameters.get("trigger")

    assert parameter is not None, "start_run must take trigger: TriggerContext"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, "no legacy default path"


# --- manual: refused before the Run row exists ---------------------------------


async def test_manual_input_violating_the_input_schema_is_refused_before_create_run(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    sink = RecordingSink()
    runner, spy = _runner(stores, registry, tmp_path, sink=sink)

    with pytest.raises(WorkflowRunError, match="customer"):
        await runner.start_run(
            WORKFLOW_ID,
            input={"customer": 42},
            initiator="operator",
            initiator_did=OPERATOR,
            trigger=TriggerContext("manual"),
        )

    assert spy.create_calls == []
    assert await spy.active_runs() == []
    refused = [
        e for e in sink.events if e.action.startswith("workflow.run.") and e.outcome != "started"
    ]
    assert refused, "a refused start is an audited operation"


async def test_manual_input_missing_a_required_field_is_refused_before_create_run(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path)

    with pytest.raises(WorkflowRunError, match="customer"):
        await runner.start_run(
            WORKFLOW_ID,
            input={},
            initiator="operator",
            initiator_did=OPERATOR,
            trigger=TriggerContext("manual"),
        )

    assert spy.create_calls == []


async def test_oversized_input_is_refused_before_create_run(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path)

    with pytest.raises(WorkflowRunError):
        await runner.start_run(
            WORKFLOW_ID,
            input={"customer": "x" * (65 * 1024)},
            initiator="operator",
            initiator_did=OPERATOR,
            trigger=TriggerContext("manual"),
        )

    assert spy.create_calls == []


async def test_valid_manual_input_is_stored_with_arc_trigger(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path)

    run = await runner.start_run(
        WORKFLOW_ID,
        input={"customer": "acme"},
        initiator="operator",
        initiator_did=OPERATOR,
        trigger=TriggerContext("manual"),
    )

    stored = await spy.get(run.run_id)
    assert stored is not None
    assert stored.input == {"customer": "acme", "arc_trigger": {"kind": "manual", "name": None}}


async def test_caller_supplied_arc_trigger_cannot_spoof_the_trigger(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    """Adversarial: a manual caller claims the run came from an event."""
    runner, spy = _runner(stores, registry, tmp_path)
    forged = {"customer": "acme", "arc_trigger": {"kind": "event", "name": "order_placed"}}

    try:
        run = await runner.start_run(
            WORKFLOW_ID,
            input=forged,
            initiator="operator",
            initiator_did=OPERATOR,
            trigger=TriggerContext("manual"),
        )
    except WorkflowRunError:
        assert spy.create_calls == []
        return
    stored = await spy.get(run.run_id)
    assert stored is not None
    assert stored.input["arc_trigger"] == {"kind": "manual", "name": None}


# --- cron: arc_trigger recorded --------------------------------------------------


async def test_cron_run_records_arc_trigger_kind_cron(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path)

    run = await runner.start_run(
        WORKFLOW_ID,
        input={"customer": "acme"},
        initiator="scheduler",
        initiator_did=SCHEDULER,
        run_id="schedule_0123456789abcdef",
        trigger_digest="digest-of-the-signed-occurrence",
        trigger=TriggerContext("cron"),
    )

    stored = await spy.get(run.run_id)
    assert stored is not None
    assert stored.input["arc_trigger"] == {"kind": "cron", "name": None}
    assert stored.input["customer"] == "acme"


async def test_cron_run_with_invalid_input_is_refused_before_create_run(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    """D-843: every trigger kind is validated, not just the event route."""
    runner, spy = _runner(stores, registry, tmp_path)

    with pytest.raises(WorkflowRunError):
        await runner.start_run(
            WORKFLOW_ID,
            input={},
            initiator="scheduler",
            initiator_did=SCHEDULER,
            run_id="schedule_0123456789abcdef",
            trigger=TriggerContext("cron"),
        )

    assert spy.create_calls == []


# --- event: the event's schema governs, not the input schema --------------------


async def test_event_run_validates_against_the_event_schema_not_the_input_schema(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path)

    run = await runner.start_run(
        WORKFLOW_ID,
        # Valid for the event schema, INVALID for the input schema (no customer).
        input={"order_id": "PO-1"},
        initiator="event",
        initiator_did=OPERATOR,
        run_id="evt-0123456789abcdef01234567",
        trigger=TriggerContext("event", "order_placed"),
    )

    stored = await spy.get(run.run_id)
    assert stored is not None
    assert stored.input == {
        "order_id": "PO-1",
        "arc_trigger": {"kind": "event", "name": "order_placed"},
    }


async def test_event_payload_valid_only_for_the_input_schema_is_refused(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path)

    with pytest.raises(WorkflowRunError, match="order_id"):
        await runner.start_run(
            WORKFLOW_ID,
            input={"customer": "acme"},
            initiator="event",
            initiator_did=OPERATOR,
            run_id="evt-0123456789abcdef01234567",
            trigger=TriggerContext("event", "order_placed"),
        )

    assert spy.create_calls == []


async def test_event_run_naming_an_undeclared_event_is_refused(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    """Adversarial: an event the signed definition never declared has no governing
    schema; falling back to the input schema would let any name start a run."""
    runner, spy = _runner(stores, registry, tmp_path)

    with pytest.raises(WorkflowRunError):
        await runner.start_run(
            WORKFLOW_ID,
            input={"customer": "acme"},
            initiator="event",
            initiator_did=OPERATOR,
            run_id="evt-0123456789abcdef01234567",
            trigger=TriggerContext("event", "never_declared"),
        )

    assert spy.create_calls == []


async def test_manual_run_on_a_workflow_without_input_schema_records_arc_trigger(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    runner, spy = _runner(stores, registry, tmp_path, with_input=False)

    run = await runner.start_run(
        WORKFLOW_ID,
        input={"anything": 1},
        initiator="operator",
        initiator_did=OPERATOR,
        trigger=TriggerContext("manual"),
    )

    stored = await spy.get(run.run_id)
    assert stored is not None
    assert stored.input == {"anything": 1, "arc_trigger": {"kind": "manual", "name": None}}


# --- event on an unsigned bundle --------------------------------------------------


async def test_event_initiated_run_of_an_unsigned_bundle_is_refused_at_personal(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    sink = RecordingSink()
    runner, spy = _runner(stores, registry, tmp_path, signer_did=None, sink=sink)

    with pytest.raises(UnsignedWorkflowRefusedError):
        await runner.start_run(
            WORKFLOW_ID,
            input={"order_id": "PO-1"},
            initiator="event",
            initiator_did=OPERATOR,
            run_id="evt-0123456789abcdef01234567",
            trigger=TriggerContext("event", "order_placed"),
        )

    assert spy.create_calls == []
    denied = [e for e in sink.events if e.outcome == "denied"]
    assert [e.action for e in denied] == ["workflow.run.started"]
    assert denied[0].extra["initiator"] == "event"
