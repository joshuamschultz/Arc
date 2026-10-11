"""T-1341 — validator additions for events, human_input, continue and escalate_to.

SPEC-088 COMP-006 (REQ-604, REQ-620, REQ-622). Every refusal must come back as a
``ValidationIssue`` naming the node (``None`` for a workflow-level problem), the
field, and the admissible alternatives, because a repair pass is driven by the
alternatives (REQ-222).

Roster contract used here: ``KnownReferences.roles`` is the registry's declared
roles (existing), and ``KnownReferences.users`` is the set of DIDs whose registry
entity type is ``user``. An ``escalate_to`` DID outside ``users`` (an agent, a
stranger) is refused; an empty ``users`` roster refuses every DID (fail closed),
and ``users=None`` (no roster read) defers the DID check to run start.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcteam.workflow import (
    KnownReferences,
    ValidationIssue,
    parse_definition,
    validate_definition,
)
from arcteam.workflow.validator import check_gate_roles

from .conftest import minimal_document

USER_DID = "did:arc:telegram:4242"
AGENT_DID = "did:arc:local:agent/1111aaaa"

_OBJECT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
}


# --- helpers -----------------------------------------------------------------


def _write_schema(root: Path, ref: str, schema: dict[str, Any]) -> None:
    target = root / ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(schema), encoding="utf-8")


def _issues(
    document: dict[str, Any], *, bundle_root: Path | None = None, **kwargs: Any
) -> tuple[ValidationIssue, ...]:
    return validate_definition(parse_definition(document), bundle_root=bundle_root, **kwargs)


def _with_node(node: dict[str, Any]) -> dict[str, Any]:
    document = minimal_document()
    document["node"].append({"needs": ["b"], **node})
    return document


def _human_input(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "ask_due",
        "kind": "human_input",
        "prompt": "Confirm the promised date",
        "output_schema": "schemas/answer.json",
    }
    node.update(overrides)
    return node


def _gate(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": "approve", "kind": "gate", "gate": "human:approve"}
    node.update(overrides)
    return node


def _escalating(escalate_to: str) -> dict[str, Any]:
    return _with_node(_gate(timeout_s=3600, on_timeout="escalate", escalate_to=escalate_to))


def _only(
    issues: tuple[ValidationIssue, ...], node_id: str | None, *fields: str
) -> ValidationIssue:
    matching = [i for i in issues if i.node_id == node_id and i.field in fields]
    assert len(matching) == 1, f"expected one issue on {node_id}.{fields}, got {issues!r}"
    return matching[0]


# --- events: the schema file must exist ---------------------------------------


def test_event_whose_schema_file_is_missing_is_refused(tmp_path: Path) -> None:
    document = minimal_document()
    document["event"] = [{"name": "order_placed", "schema": "schemas/order_placed.json"}]

    issues = _issues(document, bundle_root=tmp_path)

    event_issues = [i for i in issues if i.field.startswith("event") and "schema" in i.field]
    assert len(event_issues) == 1, issues
    issue = event_issues[0]
    assert issue.node_id is None
    assert issue.observed == "schemas/order_placed.json"
    assert issue.admissible


def test_event_whose_schema_file_exists_validates(tmp_path: Path) -> None:
    _write_schema(tmp_path, "schemas/order_placed.json", _OBJECT_SCHEMA)
    document = minimal_document()
    document["event"] = [{"name": "order_placed", "schema": "schemas/order_placed.json"}]

    assert _issues(document, bundle_root=tmp_path) == ()


def test_event_schema_escaping_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_schema(tmp_path, "outside.json", _OBJECT_SCHEMA)
    document = minimal_document()
    document["event"] = [{"name": "order_placed", "schema": "../outside.json"}]

    issues = _issues(document, bundle_root=bundle)

    assert any(i.field.startswith("event") and i.node_id is None for i in issues), issues


# --- arc_trigger is reserved ---------------------------------------------------


def test_input_schema_declaring_arc_trigger_is_refused(tmp_path: Path) -> None:
    _write_schema(
        tmp_path,
        "schemas/input.json",
        {**_OBJECT_SCHEMA, "properties": {"arc_trigger": {"type": "object"}}},
    )
    document = minimal_document()
    document["input"] = {"schema": "schemas/input.json"}

    issues = _issues(document, bundle_root=tmp_path)

    issue = _only(issues, None, "input.schema")
    assert "arc_trigger" in issue.error
    assert issue.admissible


def test_input_schema_without_arc_trigger_validates(tmp_path: Path) -> None:
    _write_schema(
        tmp_path,
        "schemas/input.json",
        {**_OBJECT_SCHEMA, "properties": {"customer": {"type": "string"}}},
    )
    document = minimal_document()
    document["input"] = {"schema": "schemas/input.json"}

    assert _issues(document, bundle_root=tmp_path) == ()


def test_event_schema_declaring_arc_trigger_is_refused(tmp_path: Path) -> None:
    """Adversarial (beyond the listed criteria): an event run's stored input also
    carries ``arc_trigger``, so an event schema may not claim the key either."""
    _write_schema(
        tmp_path,
        "schemas/order_placed.json",
        {**_OBJECT_SCHEMA, "properties": {"arc_trigger": {"type": "string"}}},
    )
    document = minimal_document()
    document["event"] = [{"name": "order_placed", "schema": "schemas/order_placed.json"}]

    issues = _issues(document, bundle_root=tmp_path)

    flagged = [i for i in issues if i.field.startswith("event") and "arc_trigger" in i.error]
    assert len(flagged) == 1, issues
    assert flagged[0].admissible


# --- human_input needs an output schema ----------------------------------------


def test_human_input_without_output_schema_is_refused() -> None:
    node = _human_input()
    del node["output_schema"]

    issues = _issues(_with_node(node))

    issue = _only(issues, "ask_due", "output_schema")
    assert issue.admissible


def test_human_input_with_output_schema_validates(tmp_path: Path) -> None:
    _write_schema(tmp_path, "schemas/answer.json", _OBJECT_SCHEMA)

    assert _issues(_with_node(_human_input()), bundle_root=tmp_path) == ()


# --- on_timeout = continue: {} must validate -----------------------------------


def test_continue_on_a_schema_that_requires_a_field_is_refused(tmp_path: Path) -> None:
    _write_schema(
        tmp_path,
        "schemas/answer.json",
        {
            **_OBJECT_SCHEMA,
            "properties": {"due_date": {"type": "string"}},
            "required": ["due_date"],
        },
    )
    node = _human_input(timeout_s=3600, on_timeout="continue")

    issues = _issues(_with_node(node), bundle_root=tmp_path)

    issue = _only(issues, "ask_due", "on_timeout", "output_schema")
    assert issue.admissible


def test_continue_on_a_gate_whose_payload_schema_requires_a_field_is_refused(
    tmp_path: Path,
) -> None:
    _write_schema(
        tmp_path,
        "schemas/approval.json",
        {**_OBJECT_SCHEMA, "properties": {"po": {"type": "string"}}, "required": ["po"]},
    )
    node = _gate(timeout_s=60, on_timeout="continue", output_schema="schemas/approval.json")

    issues = _issues(_with_node(node), bundle_root=tmp_path)

    issue = _only(issues, "approve", "on_timeout", "output_schema")
    assert issue.admissible


def test_continue_on_a_schema_that_accepts_empty_validates(tmp_path: Path) -> None:
    _write_schema(
        tmp_path,
        "schemas/answer.json",
        {**_OBJECT_SCHEMA, "properties": {"due_date": {"type": "string"}}},
    )
    node = _human_input(timeout_s=3600, on_timeout="continue")

    assert _issues(_with_node(node), bundle_root=tmp_path) == ()


# --- escalate_to: a user, or a role a user holds --------------------------------


def test_escalate_to_an_agent_did_is_refused() -> None:
    known = KnownReferences(
        agents=frozenset({"@sales"}),
        users=frozenset({USER_DID}),
        roles=frozenset({"supervisor"}),
    )

    issues = _issues(_escalating(AGENT_DID), known=known)

    issue = _only(issues, "approve", "escalate_to")
    assert issue.observed == AGENT_DID
    assert USER_DID in issue.admissible


def test_escalate_to_a_registered_user_did_validates() -> None:
    known = KnownReferences(
        agents=frozenset({"@sales"}),
        users=frozenset({USER_DID}),
        roles=frozenset({"supervisor"}),
    )

    assert _issues(_escalating(USER_DID), known=known) == ()


def test_escalate_to_a_role_no_user_holds_is_refused() -> None:
    known = KnownReferences(agents=frozenset({"@sales"}), roles=frozenset({"supervisor"}))

    issues = _issues(_escalating("role:night_shift"), known=known)

    issue = _only(issues, "approve", "escalate_to")
    assert issue.observed == "role:night_shift"
    assert issue.admissible == ("role:supervisor",)


def test_escalate_to_a_held_role_validates() -> None:
    known = KnownReferences(agents=frozenset({"@sales"}), roles=frozenset({"supervisor"}))

    assert _issues(_escalating("role:supervisor"), known=known) == ()


def test_escalate_to_on_human_input_naming_an_unheld_role_is_refused(tmp_path: Path) -> None:
    _write_schema(tmp_path, "schemas/answer.json", _OBJECT_SCHEMA)
    known = KnownReferences(agents=frozenset({"@sales"}), roles=frozenset({"supervisor"}))
    node = _human_input(timeout_s=60, on_timeout="escalate", escalate_to="role:ghost")

    issues = _issues(_with_node(node), known=known, bundle_root=tmp_path)

    issue = _only(issues, "ask_due", "escalate_to")
    assert issue.admissible == ("role:supervisor",)


def test_run_start_role_check_covers_escalate_to() -> None:
    """``check_gate_roles`` is what start_run calls with the LIVE registry roles;
    an escalation role nobody holds would strand the run at its deadline."""
    definition = parse_definition(_escalating("role:night_shift"))

    issues = check_gate_roles(definition, frozenset({"supervisor"}))

    issue = _only(issues, "approve", "escalate_to")
    assert issue.observed == "role:night_shift"
    assert issue.admissible == ("role:supervisor",)
