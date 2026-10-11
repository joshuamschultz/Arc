"""T-1337 — EventSpec, HumanInputNode, on_timeout / escalate_to, TriggerContext.

SPEC-088 COMP-005 (REQ-604, REQ-607, REQ-615, REQ-618). Every test here drives
the public parse entry point (``parse_definition``) the way a ``workflow.toml``
reaches it, so the assertions hold for whatever internal shape the model takes.

The two golden tests at the bottom are signature regressions: the canonical
document and content hash of workflows that declare none of the new keys were
captured from the code BEFORE the SPEC-088 model additions. If the new defaults
(``events = ()``, ``on_timeout = "fail"``, ``escalate_to = None``) leak into the
canonical projection, every signed workflow in the fleet drops to draft.
"""

from __future__ import annotations

import copy
import json
import tomllib
import typing
from typing import Any

import pytest

from arcteam.workflow import WorkflowParseError, parse_definition
from arcteam.workflow.serialize import content_hash, dump_toml

from .conftest import EXAMPLE_DOCUMENT, minimal_document

# --- helpers -----------------------------------------------------------------


def _with_events(*events: dict[str, Any]) -> dict[str, Any]:
    document = minimal_document()
    document["event"] = list(events)
    return document


def _event(name: str, schema: str = "schemas/e.json") -> dict[str, Any]:
    return {"name": name, "schema": schema}


def _with_node(node: dict[str, Any]) -> dict[str, Any]:
    document = minimal_document()
    document["node"].append({"needs": ["b"], **node})
    return document


def _human_input(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "ask_due",
        "kind": "human_input",
        "prompt": "Confirm the promised date",
        "approvers": ["role:planner"],
    }
    node.update(overrides)
    return node


def _gate(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "approve",
        "kind": "gate",
        "gate": "Review the plan",
        "approvers": ["role:planner"],
    }
    node.update(overrides)
    return node


def _refused(document: dict[str, Any]) -> tuple[Any, ...]:
    with pytest.raises(WorkflowParseError) as caught:
        parse_definition(document)
    return caught.value.issues


_NOT_THE_RULE = ("Extra inputs are not permitted", "does not match any of the expected tags")


def _refused_by_rule(document: dict[str, Any]) -> tuple[Any, ...]:
    """Refused, and refused by the SPEC-088 rule under test.

    Before the model knows ``on_timeout`` / ``escalate_to`` / ``human_input``,
    pydantic refuses them as unknown keys or an unknown kind tag. That refusal
    is the wrong reason; it must not count as evidence the rule exists.
    """
    issues = _refused(document)
    wrong = [i for i in issues if any(text in i.error for text in _NOT_THE_RULE)]
    assert not wrong, f"refused for the wrong reason: {[i.error for i in wrong]}"
    return issues


def _canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


# --- [[event]] (REQ-604) -------------------------------------------------------


def test_event_table_parses_into_definition_events_with_schema_ref() -> None:
    definition = parse_definition(_with_events(_event("rush_order", "schemas/rush_order.json")))

    assert len(definition.events) == 1
    event = definition.events[0]
    assert event.name == "rush_order"
    assert event.schema_ref == "schemas/rush_order.json"


def test_definition_without_event_tables_has_empty_events() -> None:
    definition = parse_definition(minimal_document())

    assert definition.events == ()


def test_event_name_of_64_chars_is_accepted() -> None:
    name = "a" + "b" * 63

    definition = parse_definition(_with_events(_event(name)))

    assert definition.events[0].name == name


@pytest.mark.parametrize(
    "bad_name",
    [
        "Rush_order",  # uppercase
        "1rush",  # leading digit
        "_rush",  # leading underscore
        "rush-order",  # hyphen
        "rush order",  # space
        "",  # empty
        "a" + "b" * 64,  # 65 chars
        "rush/../x",  # path-ish
    ],
)
def test_event_with_bad_name_is_refused(bad_name: str) -> None:
    issues = _refused(_with_events(_event(bad_name)))

    assert issues, "a bad event name must produce at least one issue"


def test_duplicate_event_names_are_refused() -> None:
    issues = _refused(_with_events(_event("rush_order"), _event("rush_order", "schemas/b.json")))

    assert issues


def test_thirty_two_events_are_accepted() -> None:
    events = [_event(f"event_{index}") for index in range(32)]

    definition = parse_definition(_with_events(*events))

    assert len(definition.events) == 32


def test_thirty_three_events_are_refused() -> None:
    events = [_event(f"event_{index}") for index in range(33)]

    issues = _refused(_with_events(*events))

    assert issues


def test_event_without_schema_is_refused() -> None:
    issues = _refused(_with_events({"name": "rush_order"}))

    assert issues


def test_event_with_unknown_key_is_refused() -> None:
    issues = _refused(_with_events({**_event("rush_order"), "handler": "scripts/x.py"}))

    assert issues


def test_declared_events_are_bound_by_the_canonical_document() -> None:
    """REQ-604: events are covered by the bundle signature."""
    definition = parse_definition(_with_events(_event("rush_order", "schemas/rush_order.json")))

    canonical = definition.canonical_document()

    assert canonical["event"] == [{"name": "rush_order", "schema": "schemas/rush_order.json"}]


def test_changing_an_event_schema_ref_changes_the_content_hash() -> None:
    before = parse_definition(_with_events(_event("rush_order", "schemas/a.json")))
    after = parse_definition(_with_events(_event("rush_order", "schemas/b.json")))

    assert content_hash(before, {}) != content_hash(after, {})


def test_events_round_trip_through_toml_emission() -> None:
    """A declared event must survive write-to-disk, or the signed bundle loses it."""
    definition = parse_definition(_with_events(_event("rush_order", "schemas/rush_order.json")))

    reparsed = parse_definition(tomllib.loads(dump_toml(definition.to_document())))

    assert reparsed.events == definition.events


# --- human_input node (REQ-615) -----------------------------------------------


def test_human_input_node_without_output_schema_parses() -> None:
    """The model accepts it; the validator (COMP-006) refuses it later."""
    definition = parse_definition(_with_node(_human_input()))

    node = definition.node_by_id("ask_due")
    assert node.kind == "human_input"
    assert node.prompt == "Confirm the promised date"
    assert node.approvers == ("role:planner",)
    assert node.output_schema is None


def test_human_input_node_with_4000_char_prompt_is_accepted() -> None:
    definition = parse_definition(_with_node(_human_input(prompt="p" * 4000)))

    assert len(definition.node_by_id("ask_due").prompt) == 4000


@pytest.mark.parametrize("prompt", ["", "p" * 4001])
def test_human_input_node_with_out_of_range_prompt_is_refused(prompt: str) -> None:
    issues = _refused(_with_node(_human_input(prompt=prompt)))

    assert any(issue.node_id == "ask_due" and issue.field == "prompt" for issue in issues)


def test_human_input_node_without_prompt_is_refused() -> None:
    node = _human_input()
    del node["prompt"]

    issues = _refused(_with_node(node))

    assert any(issue.node_id == "ask_due" and issue.field == "prompt" for issue in issues)


@pytest.mark.parametrize(
    "approvers",
    [
        ["planner"],  # neither DID nor role:
        ["role:"],  # empty role name
        ["role:planner", "role:planner"],  # duplicate
        ["did:arc"],  # truncated DID
    ],
)
def test_human_input_node_applies_the_gate_approver_rules(approvers: list[str]) -> None:
    gate_issues = _refused(_with_node(_gate(approvers=approvers)))
    human_issues = _refused(_with_node(_human_input(approvers=approvers)))

    assert gate_issues
    assert any(issue.node_id == "ask_due" and issue.field == "approvers" for issue in human_issues)


def test_human_input_is_an_admissible_node_kind_in_repair_hints() -> None:
    issues = _refused(_with_node({"id": "x", "kind": "not_a_kind"}))

    kind_issue = next(issue for issue in issues if issue.node_id == "x")
    assert "human_input" in kind_issue.admissible


# --- on_timeout / escalate_to (REQ-618) ---------------------------------------


@pytest.mark.parametrize("builder", [_gate, _human_input])
def test_on_timeout_defaults_to_fail_and_escalate_to_to_none(builder: Any) -> None:
    node_id = builder()["id"]

    node = parse_definition(_with_node(builder())).node_by_id(node_id)

    assert node.on_timeout == "fail"
    assert node.escalate_to is None


@pytest.mark.parametrize("builder", [_gate, _human_input])
@pytest.mark.parametrize("on_timeout", ["fail", "continue"])
def test_on_timeout_with_timeout_s_is_accepted(builder: Any, on_timeout: str) -> None:
    node_id = builder()["id"]

    node = parse_definition(_with_node(builder(timeout_s=600, on_timeout=on_timeout))).node_by_id(
        node_id
    )

    assert node.on_timeout == on_timeout
    assert node.timeout_s == 600


@pytest.mark.parametrize("builder", [_gate, _human_input])
@pytest.mark.parametrize("escalate_to", ["role:supervisor", "did:arc:user:alice"])
def test_escalate_with_did_or_role_escalate_to_is_accepted(builder: Any, escalate_to: str) -> None:
    node_id = builder()["id"]

    node = parse_definition(
        _with_node(builder(timeout_s=600, on_timeout="escalate", escalate_to=escalate_to))
    ).node_by_id(node_id)

    assert node.on_timeout == "escalate"
    assert node.escalate_to == escalate_to


@pytest.mark.parametrize("builder", [_gate, _human_input])
def test_escalate_without_escalate_to_is_refused_by_rule(builder: Any) -> None:
    issues = _refused_by_rule(_with_node(builder(timeout_s=600, on_timeout="escalate")))

    assert issues


@pytest.mark.parametrize("builder", [_gate, _human_input])
@pytest.mark.parametrize("on_timeout", ["fail", "continue"])
def test_escalate_to_without_escalate_on_timeout_is_refused_by_rule(
    builder: Any, on_timeout: str
) -> None:
    """``escalate_to`` is required IFF ``on_timeout == 'escalate'``."""
    issues = _refused_by_rule(
        _with_node(builder(timeout_s=600, on_timeout=on_timeout, escalate_to="role:supervisor"))
    )

    assert issues


@pytest.mark.parametrize("builder", [_gate, _human_input])
@pytest.mark.parametrize(
    ("on_timeout", "extra"),
    [("continue", {}), ("escalate", {"escalate_to": "role:supervisor"})],
)
def test_non_default_on_timeout_without_timeout_s_is_refused_by_rule(
    builder: Any, on_timeout: str, extra: dict[str, Any]
) -> None:
    issues = _refused_by_rule(_with_node(builder(on_timeout=on_timeout, **extra)))

    assert issues


@pytest.mark.parametrize("builder", [_gate, _human_input])
def test_unknown_on_timeout_value_is_refused_by_rule(builder: Any) -> None:
    issues = _refused_by_rule(_with_node(builder(timeout_s=600, on_timeout="ignore")))

    assert issues


@pytest.mark.parametrize("builder", [_gate, _human_input])
@pytest.mark.parametrize("escalate_to", ["supervisor", "role:", "@sales", "did:arc", ""])
def test_escalate_to_that_is_neither_did_nor_role_is_refused_by_rule(
    builder: Any, escalate_to: str
) -> None:
    issues = _refused_by_rule(
        _with_node(builder(timeout_s=600, on_timeout="escalate", escalate_to=escalate_to))
    )

    assert issues


@pytest.mark.parametrize(
    "node",
    [
        {"id": "x", "kind": "agent", "agent": "@sales"},
        {"id": "x", "kind": "tool", "tool": "crm_lookup"},
    ],
)
def test_non_waiting_node_kinds_refuse_on_timeout(node: dict[str, Any]) -> None:
    issues = _refused(_with_node({**node, "timeout_s": 60, "on_timeout": "continue"}))

    assert issues


# --- Initiator and TriggerContext (REQ-607, REQ-609) --------------------------


def test_initiator_literal_includes_event() -> None:
    from arcteam.workflow.runner_contracts import Initiator

    assert "event" in typing.get_args(Initiator)


def test_initiator_literal_keeps_existing_values() -> None:
    from arcteam.workflow.runner_contracts import Initiator

    assert {"operator", "agent", "scheduler", "chat"} <= set(typing.get_args(Initiator))


def test_trigger_context_positional_manual_has_no_name() -> None:
    from arcteam.workflow.runner_contracts import TriggerContext

    context = TriggerContext("manual")

    assert context.kind == "manual"
    assert context.name is None


@pytest.mark.parametrize(
    ("kind", "name"),
    [("manual", None), ("cron", None), ("interval", None), ("event", "rush_order")],
)
def test_trigger_context_carries_kind_and_name(kind: str, name: str | None) -> None:
    from arcteam.workflow.runner_contracts import TriggerContext

    context = TriggerContext(kind=kind, name=name)  # type: ignore[arg-type]  # parametrized str

    assert (context.kind, context.name) == (kind, name)


def test_trigger_context_kind_literal_is_exactly_the_four_trigger_kinds() -> None:
    from arcteam.workflow.runner_contracts import TriggerContext

    hints = typing.get_type_hints(TriggerContext)

    assert set(typing.get_args(hints["kind"])) == {"manual", "cron", "interval", "event"}


# --- signature regression: goldens captured before SPEC-088 -------------------

# Captured from main 46e06998 (pre-SPEC-088 models.py) via
# json.dumps(parse_definition(EXAMPLE_DOCUMENT).canonical_document(),
#            sort_keys=True, separators=(",", ":")).
_EXAMPLE_CANONICAL_GOLDEN = (
    '{"input":{"schema":"schemas/onboarding_input.json"},"node":[{"agent":"@sales",'
    '"artifacts":["customer_record.json"],"id":"collect","kind":"agent","max_attempts":3,'
    '"output_schema":"schemas/customer_record.json","prompt":"prompts/collect.md",'
    '"skill":"customer-intake","strategy":["react"],"timeout_s":300},'
    '{"gate":"human:approve_high_risk","id":"manual_review","kind":"gate",'
    '"needs":["risk_router"]},{"agent":"@ops","id":"provision","kind":"script",'
    '"needs":["risk_router"],"output_schema":"schemas/provisioned.json",'
    '"script":"scripts/provision.py"},{"agent":"@reviewer","id":"qa","kind":"agent",'
    '"needs":["provision"],"output_schema":"schemas/qa_verdict.json"},{"agent":"@ops",'
    '"id":"revise","kind":"agent","needs":["qa"],'
    '"when":"$nodes.qa.output.verdict == \'revise\'"},{"id":"risk_router","kind":"router",'
    '"needs":["verify"],"routes":[{"to":"provision",'
    '"when":"$nodes.verify.output.risk == \'low\'"},{"default":true,"to":"manual_review"}]},'
    '{"agent":"@sales","args":{"domain":"$nodes.collect.output.company_domain"},'
    '"id":"verify","kind":"tool","needs":["collect"],'
    '"output_schema":"schemas/verification.json","tool":"crm_lookup"}],'
    '"trigger":{"active_hours":{"end":"18:00","start":"08:00","timezone":"America/Chicago"},'
    '"expression":"0 9 * * MON","type":"cron"},"workflow":{"budget":{"tokens":400000,'
    '"wall_clock_s":1800},"channel":"channel://onboarding",'
    '"description":"New customer intake through provisioning","id":"customer-onboarding",'
    '"owner":"@sales","schema_version":"1.0","version":4}}'
)
_EXAMPLE_CONTENT_HASH_GOLDEN = (
    "sha256:96b074afc1a6ce59f80750ddeb148489586c6a0c8fb911e95bd4dbf396f13f74"
)

# A gate with approvers + timeout_s + output_schema: the exact shape that gains
# ``on_timeout`` / ``escalate_to`` defaults under SPEC-088.
_GATED_DOCUMENT: dict[str, Any] = {
    "workflow": {"id": "gated", "owner": "@sales"},
    "node": [
        {"id": "a", "kind": "agent", "agent": "@sales"},
        {
            "id": "review",
            "kind": "gate",
            "gate": "Approve it",
            "needs": ["a"],
            "approvers": ["role:planner", "did:arc:user:alice"],
            "timeout_s": 600,
            "output_schema": "schemas/approval.json",
        },
    ],
}
_GATED_CANONICAL_GOLDEN = (
    '{"node":[{"agent":"@sales","id":"a","kind":"agent"},{"approvers":["role:planner",'
    '"did:arc:user:alice"],"gate":"Approve it","id":"review","kind":"gate","needs":["a"],'
    '"output_schema":"schemas/approval.json","timeout_s":600}],'
    '"workflow":{"id":"gated","owner":"@sales","schema_version":"1.0"}}'
)
_GATED_CONTENT_HASH_GOLDEN = (
    "sha256:08ea5f56db0a7a2d92de95d4d154140690d0e73edfa341ae4f6a85df1463873f"
)


def test_example_workflow_canonical_document_is_byte_identical_to_pre_spec_golden() -> None:
    definition = parse_definition(copy.deepcopy(EXAMPLE_DOCUMENT))

    assert _canonical_json(definition.canonical_document()) == _EXAMPLE_CANONICAL_GOLDEN
    assert content_hash(definition, {}) == _EXAMPLE_CONTENT_HASH_GOLDEN


def test_gated_workflow_canonical_document_is_byte_identical_to_pre_spec_golden() -> None:
    definition = parse_definition(copy.deepcopy(_GATED_DOCUMENT))

    assert _canonical_json(definition.canonical_document()) == _GATED_CANONICAL_GOLDEN
    assert content_hash(definition, {}) == _GATED_CONTENT_HASH_GOLDEN


def test_canonical_document_omits_event_key_when_no_events_declared() -> None:
    definition = parse_definition(copy.deepcopy(_GATED_DOCUMENT))

    assert "event" not in definition.canonical_document()
    assert "event" not in definition.to_document()
