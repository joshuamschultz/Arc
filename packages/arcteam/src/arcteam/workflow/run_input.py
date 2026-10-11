"""Run-input admission: the governing schema and the ``arc_trigger`` stamp (SPEC-088 D-843).

``WorkflowRunner.start_run`` is the one choke point every run passes through —
operator, schedule, agent, chat and event alike — so every run input is judged
here, before the Run row exists:

* The GOVERNING schema is the fired event's schema for an event run, else the
  workflow's input schema when one is declared. An event the signed definition
  does not declare has no schema and is refused; falling back to the input
  schema would let any event name start a run.
* The input is always bounded (size and depth) through SchemaCheck, even when
  no schema is declared.
* ``arc_trigger`` is reserved. A caller that supplies it is refused rather than
  silently overwritten, so a forged trigger is visible in the audit trail.

Kept out of ``runner.py`` to hold that file's size; it is pure and reads
schemas only from the verified bundle root.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import ValidationIssue
from .runner_contracts import BundleSpec, Initiator, TriggerSpec
from .schema_check import SchemaRefusedError, check, load_schema

ARC_TRIGGER_KEY = "arc_trigger"
"""The reserved run-input key the runner writes the trigger under."""


def admission_issues(
    bundle: BundleSpec,
    run_input: Mapping[str, Any],
    *,
    initiator: Initiator,
    trigger: TriggerSpec,
) -> tuple[ValidationIssue, ...]:
    """Every reason ``run_input`` may not start a run of ``bundle``; empty when admissible."""
    pairing = _trigger_pairing_issue(initiator, trigger)
    if pairing is not None:
        return (pairing,)
    if ARC_TRIGGER_KEY in run_input:
        return (
            ValidationIssue(
                field=ARC_TRIGGER_KEY,
                error=f"{ARC_TRIGGER_KEY!r} is reserved; the runner records the trigger itself",
            ),
        )
    try:
        schema = _governing_schema(bundle, trigger)
    except SchemaRefusedError as exc:
        return (ValidationIssue(field="schema", error=str(exc)),)
    if isinstance(schema, ValidationIssue):
        return (schema,)
    return check(dict(run_input), schema)


def stamped_input(run_input: Mapping[str, Any], trigger: TriggerSpec) -> dict[str, Any]:
    """The input as stored: the caller's fields plus the trigger that started the run."""
    return {**run_input, ARC_TRIGGER_KEY: {"kind": trigger.kind, "name": trigger.name}}


def _trigger_pairing_issue(initiator: Initiator, trigger: TriggerSpec) -> ValidationIssue | None:
    """An event trigger only with the event initiator, and always with a name.

    Without this an operator-initiated start could claim an event trigger (or an
    event start claim a manual one) and land under the wrong trust rule.
    """
    if (initiator == "event") != (trigger.kind == "event"):
        return ValidationIssue(
            field="trigger.kind",
            error=f"a {initiator}-initiated run cannot carry a {trigger.kind!r} trigger",
            observed=trigger.kind,
        )
    if (trigger.kind == "event") != (trigger.name is not None):
        return ValidationIssue(
            field="trigger.name",
            error="an event trigger names its event; no other trigger does",
            observed=trigger.name,
        )
    return None


def _governing_schema(
    bundle: BundleSpec, trigger: TriggerSpec
) -> dict[str, Any] | ValidationIssue:
    """The schema this run's input must satisfy; ``{}`` (bounds only) when none is declared.

    An event the definition does not declare is returned as the refusal issue.
    """
    definition = bundle.definition
    if trigger.kind == "event":
        declared = {event.name: event.schema_ref for event in definition.events}
        ref = declared.get(trigger.name or "")
        if ref is None:
            return ValidationIssue(
                field="trigger.name",
                error=f"workflow {definition.id!r} declares no event {trigger.name!r}",
                observed=trigger.name,
                admissible=tuple(sorted(declared)),
            )
        return load_schema(bundle.root, ref)
    if definition.input_spec is None:
        return {}
    return load_schema(bundle.root, definition.input_spec.schema_ref)


__all__ = ["ARC_TRIGGER_KEY", "admission_issues", "stamped_input"]
