"""T-1345 — ``WorkflowControlPlane.fire_event`` (SPEC-088 COMP-007, REQ-605/606/607).

``fire_event(workflow_id, name, payload, *, idempotency_key, actor_did,
on_behalf_of) -> ControlPlaneResult`` finds the event in the signed bundle,
derives ``run_id = "evt-" + sha256(workflow_id, name, actor_did, key)[:24]`` and
starts the run through its OWN ``run()`` with initiator ``event`` and
``TriggerContext("event", name)`` (D-842, D-843).

The run store is the REAL ``WorkflowRunStore`` so the identity check that turns
"same key, different payload" into a refusal is the production one.

Issue codes (``not_found``, ``invalid_payload``, ``conflict``) are read as a
machine-readable value: an issue's ``code`` attribute when it has one, else its
``error`` or ``field`` must equal the code exactly. The route layer maps these to
HTTP statuses, so prose that merely mentions the word is not enough.
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend

from arcteam.workflow import WorkflowDefinition, parse_definition
from arcteam.workflow.control_plane import ControlPlaneResult, WorkflowControlPlane
from arcteam.workflow.runner_contracts import TriggerContext
from arcteam.workflow.stores import WorkflowRunStore

from .conftest import RecordingSink
from .test_runner_frontier import build

WORKFLOW_ID = "order-intake"
EVENT = "order_placed"
APP_DID = "did:arc:app:erp/0a0a0a0a"
OTHER_APP_DID = "did:arc:app:crm/0b0b0b0b"
PERSON = "did:arc:user:alice"

_EVENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"order_id": {"type": "string"}, "qty": {"type": "integer"}},
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


class Definitions:
    """Load-side of the definition store; an unknown id raises like the real one."""

    def __init__(self, bundle: RootedBundle) -> None:
        self._bundle = bundle

    def _get(self, workflow_id: str) -> RootedBundle:
        if workflow_id != self._bundle.definition.id:
            raise KeyError(workflow_id)
        return self._bundle

    def load(self, workflow_id: str) -> RootedBundle:
        return self._get(workflow_id)

    def load_for_run(self, workflow_id: str) -> RootedBundle:
        return self._get(workflow_id)

    def load_for_dispatch(self, workflow_id: str) -> RootedBundle:
        return self._get(workflow_id)


def _bundle(tmp_path: Path, *, signer_did: str | None = "did:arc:local:operator/0f0f0f0f") -> Any:
    root = tmp_path / WORKFLOW_ID
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "schemas" / "order_placed.json").write_text(json.dumps(_EVENT_SCHEMA))
    definition = parse_definition(
        {
            "workflow": {"id": WORKFLOW_ID, "owner": "@sales"},
            "event": [{"name": EVENT, "schema": "schemas/order_placed.json"}],
            "node": [{"id": "only", "kind": "agent", "agent": "@sales"}],
        }
    )
    return RootedBundle(definition, root, signer_did=signer_did)


def _plane(
    stores: Any, registry: Any, tmp_path: Path, **bundle_kwargs: Any
) -> tuple[WorkflowControlPlane, WorkflowRunStore, RecordingSink]:
    flow_tasks, _, tasks = stores
    runs = WorkflowRunStore(FakeBackend())
    sink = RecordingSink()
    bundle = _bundle(tmp_path, **bundle_kwargs)
    definitions = Definitions(bundle)
    runner = build(
        (flow_tasks, runs, tasks),
        registry,
        bundle.definition,
        definitions=definitions,
        audit_sink=sink,
    )
    plane = WorkflowControlPlane(
        definitions=definitions,  # type: ignore[arg-type]  # structural double
        parse=parse_definition,  # type: ignore[arg-type]  # not exercised here
        validate=lambda definition, **_: (),  # type: ignore[arg-type]  # not exercised
        runner=runner,
        runs=runs,  # type: ignore[arg-type]  # the real store satisfies the seam
        tier="personal",
        audit_sink=sink,
    )
    return plane, runs, sink


async def _fire(
    plane: WorkflowControlPlane,
    payload: dict[str, Any],
    *,
    name: str = EVENT,
    key: str = "key-1",
    actor_did: str = APP_DID,
    workflow_id: str = WORKFLOW_ID,
) -> ControlPlaneResult:
    return await plane.fire_event(  # type: ignore[attr-defined, no-any-return]  # T-1346
        workflow_id,
        name,
        payload,
        idempotency_key=key,
        actor_did=actor_did,
        on_behalf_of=PERSON,
    )


def _codes(result: ControlPlaneResult) -> set[str]:
    codes: set[str] = set()
    for issue in result.errors:
        for value in (getattr(issue, "code", None), issue.error, issue.field):
            if isinstance(value, str):
                codes.add(value)
    return codes


def _started(sink: RecordingSink) -> list[Any]:
    return [
        e for e in sink.events if e.action == "workflow.run.started" and e.outcome == "started"
    ]


# --- refusals ---------------------------------------------------------------------


async def test_unknown_event_is_not_found(stores: Any, registry: Any, tmp_path: Path) -> None:
    plane, runs, sink = _plane(stores, registry, tmp_path)

    result = await _fire(plane, {"order_id": "PO-1"}, name="never_declared")

    assert not result.ok
    assert "not_found" in _codes(result)
    assert result.run is None
    assert _started(sink) == []


async def test_unknown_workflow_is_refused_and_starts_nothing(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    plane, _, sink = _plane(stores, registry, tmp_path)

    result = await _fire(plane, {"order_id": "PO-1"}, workflow_id="no-such-flow")

    assert not result.ok
    assert result.run is None
    assert _started(sink) == []


async def test_invalid_payload_is_refused_with_field_issues(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    plane, _, sink = _plane(stores, registry, tmp_path)

    result = await _fire(plane, {"qty": "three"})

    assert not result.ok
    assert "invalid_payload" in _codes(result)
    fields = {issue.field for issue in result.errors}
    assert {"order_id", "qty"} <= fields, result.errors
    assert result.run is None
    assert _started(sink) == []


async def test_event_on_an_unsigned_bundle_is_refused_at_personal(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    plane, _, sink = _plane(stores, registry, tmp_path, signer_did=None)

    result = await _fire(plane, {"order_id": "PO-1"})

    assert not result.ok
    assert result.run is None
    assert _started(sink) == []


# --- dedupe by deterministic run id (D-842) --------------------------------------


async def test_same_key_and_payload_returns_the_same_run_without_a_second_start(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    plane, _, sink = _plane(stores, registry, tmp_path)

    first = await _fire(plane, {"order_id": "PO-1"})
    started_after_first = len(_started(sink))
    second = await _fire(plane, {"order_id": "PO-1"})

    assert first.ok and second.ok, (first.errors, second.errors)
    assert first.run is not None and second.run is not None
    assert first.run.run_id == second.run.run_id
    assert started_after_first >= 1
    # The runner and the control plane share one sink; the replay must add NO
    # started record from either of them.
    assert len(_started(sink)) == started_after_first, [(e.action, e.outcome) for e in sink.events]


async def test_same_key_with_a_different_payload_is_a_conflict(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    plane, runs, _ = _plane(stores, registry, tmp_path)

    first = await _fire(plane, {"order_id": "PO-1"})
    second = await _fire(plane, {"order_id": "PO-2"})

    assert first.ok
    assert not second.ok
    assert "conflict" in _codes(second)
    assert first.run is not None
    kept = await runs.get(first.run.run_id)
    assert kept is not None and kept.input["order_id"] == "PO-1"


async def test_two_principals_reusing_one_key_get_separate_runs(
    stores: Any, registry: Any, tmp_path: Path
) -> None:
    """Adversarial: a key is scoped to the principal, so one app cannot replay
    (or block with a conflict) another app's event by guessing its key."""
    plane, _, _ = _plane(stores, registry, tmp_path)

    mine = await _fire(plane, {"order_id": "PO-1"}, actor_did=APP_DID)
    theirs = await _fire(plane, {"order_id": "PO-9"}, actor_did=OTHER_APP_DID)

    assert mine.ok and theirs.ok, (mine.errors, theirs.errors)
    assert mine.run is not None and theirs.run is not None
    assert mine.run.run_id != theirs.run.run_id


# --- the run id and the delegation to run() ---------------------------------------


async def test_fire_event_starts_through_run_with_initiator_event_and_trigger(
    stores: Any, registry: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane, runs, _ = _plane(stores, registry, tmp_path)
    calls: list[dict[str, Any]] = []
    original = plane.run

    async def spy(workflow_id: str, **kwargs: Any) -> ControlPlaneResult:
        calls.append({"workflow_id": workflow_id, **kwargs})
        return await original(workflow_id, **kwargs)

    monkeypatch.setattr(plane, "run", spy)

    result = await _fire(plane, {"order_id": "PO-1"})

    assert result.ok, result.errors
    assert len(calls) == 1
    call = calls[0]
    assert call["workflow_id"] == WORKFLOW_ID
    assert call["initiator"] == "event"
    assert call["trigger"] == TriggerContext("event", EVENT)
    assert call["actor_did"] == APP_DID
    run_id = call["run_id"]
    assert run_id.startswith("evt-")
    assert len(run_id) == len("evt-") + 24
    assert set(run_id[len("evt-") :]) <= set(string.hexdigits.lower())
    assert call.get("trigger_digest"), "the event identity is bound into the run"
    assert result.run is not None and result.run.run_id == run_id
    stored = await runs.get(run_id)
    assert stored is not None
    assert stored.input["arc_trigger"] == {"kind": "event", "name": EVENT}


async def test_run_id_is_deterministic_and_varies_with_each_identity_part(
    stores: Any, registry: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane, _, _ = _plane(stores, registry, tmp_path)
    seen: list[str] = []
    original = plane.run

    async def spy(workflow_id: str, **kwargs: Any) -> ControlPlaneResult:
        seen.append(kwargs["run_id"])
        return await original(workflow_id, **kwargs)

    monkeypatch.setattr(plane, "run", spy)

    await _fire(plane, {"order_id": "PO-1"}, key="k-1", actor_did=APP_DID)
    await _fire(plane, {"order_id": "PO-1"}, key="k-1", actor_did=APP_DID)
    await _fire(plane, {"order_id": "PO-1"}, key="k-2", actor_did=APP_DID)
    await _fire(plane, {"order_id": "PO-1"}, key="k-1", actor_did=OTHER_APP_DID)

    assert len(seen) == 4
    assert seen[0] == seen[1], "same identity, same run id"
    assert len({seen[0], seen[2], seen[3]}) == 3, "key and principal both feed the id"
