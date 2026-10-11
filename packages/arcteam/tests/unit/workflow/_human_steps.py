"""Shared harness for the SPEC-088 human-step tests (T-1347, T-1349, T-1351).

Gate and human_input nodes are the REAL ``GateNode`` / ``HumanInputNode``
models, so the field names the runner reads are the ones a signed bundle
carries; agent and tool nodes are the conftest doubles. The stores are the
real ``WorkflowRunStore`` / ``WorkflowTaskStore`` over a real arcstore
``FakeBackend``, so ``update_if`` and dotted ``where`` keys behave exactly as
in production. The only fakes are the clock, the registry and the notifier.

Runs are started the way existing runner tests start them, with the required
``trigger=TriggerContext("manual")`` (T-1344) passed by :func:`start`, the one
call site in this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arcstore.tasks import Task, TaskStore

from arcteam.workflow import GateNode, HumanInputNode
from arcteam.workflow.control_plane import WorkflowControlPlane
from arcteam.workflow.runner import WorkflowRunner
from arcteam.workflow.runner_contracts import TriggerContext
from arcteam.workflow.stores import WorkflowRunStore, WorkflowTaskStore

from .conftest import (
    RUNNER_DID,
    SALES_DID,
    Bundle,
    Definition,
    Node,
    RecordingNotifier,
    RecordingSink,
    Registry,
    complete_node,
    evaluate,
    resolve_args,
    task_id,
)

WORKFLOW_ID = "orders"

ALICE = "did:arc:local:user/aaaa1111"  # holds role planner
BOB = "did:arc:local:user/bbbb2222"  # holds role planner
CAROL = "did:arc:local:user/cccc3333"  # the declared escalation target
MALLORY = "did:arc:local:user/dddd4444"  # holds nothing
APP = "did:arc:app:orders-portal"

PLANNER = frozenset({"planner"})

APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"qty": {"type": "integer", "minimum": 1}},
    "required": ["qty"],
    "additionalProperties": False,
}
PROMISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"promised": {"type": "string", "minLength": 1}},
    "required": ["promised"],
}
OPEN_SCHEMA: dict[str, Any] = {"type": "object"}

SCHEMAS: dict[str, dict[str, Any]] = {
    "schemas/approval.json": APPROVAL_SCHEMA,
    "schemas/promise.json": PROMISE_SCHEMA,
    "schemas/open.json": OPEN_SCHEMA,
}

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
TIMEOUT_S = 60

ENVELOPE_KEYS = frozenset(
    {"decision", "notes", "payload", "decided_by", "on_behalf_of", "decided_at", "timed_out"}
)


class Clock:
    """A clock the test moves by hand. Shared by every runner of one test."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class BundleDefinitions:
    """Definition store double that also exposes ``root`` like the real store.

    The runner derives a node's bundle directory from ``root / workflow_id``
    (``_bundle_root``); the schema files live there, so stamping
    ``output_schema_resolved`` reads the verified bundle, never the row.
    """

    def __init__(self, bundle: Bundle, root: Path) -> None:
        self._bundle = bundle
        self.root = root

    def load(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_run(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_dispatch(self, workflow_id: str) -> Bundle:
        return self._bundle


def write_bundle(root: Path) -> None:
    """Write every schema file under ``root/<workflow_id>/``."""
    for ref, schema in SCHEMAS.items():
        target = root / WORKFLOW_ID / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(schema), encoding="utf-8")


def team() -> Registry:
    return Registry(
        {"sales": SALES_DID},
        roles={ALICE: ("planner",), BOB: ("planner",), CAROL: ("supervisor",)},
    )


def approval_flow(
    *,
    output_schema: str | None = "schemas/approval.json",
    approvers: tuple[str, ...] = ("role:planner",),
    timeout_s: int | None = None,
    on_timeout: str = "fail",
    escalate_to: str | None = None,
    channel: str | None = None,
    order_args: Mapping[str, Any] | None = None,
) -> Definition:
    """draft -> approve (gate) -> order (tool; by default reads the approved qty).

    ``order_args`` replaces the tool args, for flows whose gate payload has no
    ``qty`` (an empty ``continue`` payload makes ``payload.qty`` unresolvable).
    """
    gate_fields: dict[str, Any] = {
        "id": "approve",
        "kind": "gate",
        "gate": "Review the plan",
        "needs": ("draft",),
        "approvers": approvers,
        "output_schema": output_schema,
        "timeout_s": timeout_s,
        "on_timeout": on_timeout,
        "escalate_to": escalate_to,
    }
    return Definition(
        id=WORKFLOW_ID,
        owner="@sales",
        channel=channel,
        nodes=(
            Node(id="draft", kind="agent", agent="@sales"),
            GateNode.model_validate(gate_fields),  # type: ignore[arg-type]  # real model in a double's tuple
            Node(
                id="order",
                kind="tool",
                agent="@sales",
                tool="place_order",
                needs=("approve",),
                args=dict(order_args or {"qty": "$nodes.approve.output.payload.qty"}),
            ),
        ),
    )


def input_flow(
    *,
    output_schema: str = "schemas/promise.json",
    timeout_s: int | None = None,
    on_timeout: str = "fail",
    escalate_to: str | None = None,
) -> Definition:
    """draft -> ask_due (human_input) -> confirm."""
    input_fields: dict[str, Any] = {
        "id": "ask_due",
        "kind": "human_input",
        "prompt": "Confirm the promised date",
        "needs": ("draft",),
        "approvers": ("role:planner",),
        "output_schema": output_schema,
        "timeout_s": timeout_s,
        "on_timeout": on_timeout,
        "escalate_to": escalate_to,
    }
    return Definition(
        id=WORKFLOW_ID,
        owner="@sales",
        nodes=(
            Node(id="draft", kind="agent", agent="@sales"),
            HumanInputNode.model_validate(input_fields),  # type: ignore[arg-type]  # real model in a double's tuple
            Node(id="confirm", kind="agent", agent="@sales", needs=("ask_due",)),
        ),
    )


def single_node_flow() -> Definition:
    """One agent node: the shortest run that can finish or fail."""
    return Definition(
        id=WORKFLOW_ID, owner="@sales", nodes=(Node(id="draft", kind="agent", agent="@sales"),)
    )


@dataclass
class Harness:
    """One deployment's runner, control plane and stores over a shared backend."""

    backend: Any
    definition: Definition
    root: Path
    clock: Clock
    sink: RecordingSink = field(default_factory=RecordingSink)
    notifier: RecordingNotifier = field(default_factory=RecordingNotifier)
    registry: Registry = field(default_factory=team)
    tasks_override: WorkflowTaskStore | None = None
    runner_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        write_bundle(self.root)
        self.flow_tasks = self.tasks_override or WorkflowTaskStore(
            self.backend, actor_did=RUNNER_DID
        )
        self.runs = WorkflowRunStore(self.backend)
        self.tasks = TaskStore(self.backend)
        definitions = BundleDefinitions(Bundle(self.definition), self.root)
        self.runner = WorkflowRunner(
            tasks=self.flow_tasks,
            runs=self.runs,
            definitions=definitions,
            owners=self.registry,
            roles=self.registry,
            runner_did=RUNNER_DID,
            tier="personal",
            evaluate=evaluate,
            resolve_args=resolve_args,
            audit_sink=self.sink,
            operator_notifier=self.notifier,
            clock=self.clock,
            **dict(self.runner_kwargs),
        )
        self.plane = WorkflowControlPlane(
            definitions=definitions,
            parse=lambda document: document,
            validate=lambda definition, **_: (),
            runner=self.runner,
            runs=self.runs,
            tier="personal",
            audit_sink=self.sink,
        )

    async def start(self, run_input: Mapping[str, Any] | None = None) -> str:
        """Start a run as existing tests do, as a manual trigger."""
        run = await self.runner.start_run(
            WORKFLOW_ID,
            input=dict(run_input or {}),
            initiator="operator",
            initiator_did=ALICE,
            trigger=TriggerContext("manual"),
        )
        return run.run_id

    async def reach(self, node_id: str, run_input: Mapping[str, Any] | None = None) -> str:
        """Start a run, finish ``draft``, and advance until ``node_id`` waits."""
        run_id = await self.start(run_input)
        await complete_node(self.tasks, task_id(run_id, "draft", 0), SALES_DID, {"draft": "v1"})
        await self.runner.advance(run_id)
        return run_id

    async def row(self, run_id: str, node_id: str, iteration: int = 0) -> Task:
        found = await self.flow_tasks.get(task_id(run_id, node_id, iteration))
        assert found is not None, f"{node_id}#{iteration} was never materialized"
        return found

    async def rows(self, run_id: str) -> dict[str, Task]:
        return {r.id: r for r in await self.flow_tasks.query_by_flow_run(run_id)}

    async def run_status(self, run_id: str) -> str:
        run = await self.runs.get(run_id)
        assert run is not None
        return str(run.status)


def issue_codes(result: Any) -> set[str]:
    """Every machine-readable code a refusal carries, wherever the plane puts it.

    The control plane has no issue-code field today; COMP-007 names the codes
    (``invalid_payload``, ``not_found`` ...) the route maps to 422/404. This
    accepts the code on the result, on an issue, or as the issue's ``error``.
    """
    codes: set[str] = set()
    for holder in (result, *result.errors):
        for attr in ("code", "error"):
            value = getattr(holder, attr, None)
            if isinstance(value, str):
                codes.add(value)
    return codes


def issue_fields(result: Any) -> list[str]:
    return [str(getattr(issue, "field", "") or "") for issue in result.errors]


def actions(sink: RecordingSink, action: str) -> list[Any]:
    return [event for event in sink.events if event.action == action]
