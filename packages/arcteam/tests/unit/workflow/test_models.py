"""T-826 — WorkflowDefinition model tests (COMP-001, REQ-217/REQ-218)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arcteam.workflow import (
    MAX_NODES,
    SCHEMA_VERSION,
    AgentNode,
    GateNode,
    RouterNode,
    ScriptNode,
    ToolNode,
    WorkflowParseError,
    parse_definition,
)

from .conftest import minimal_document


def test_example_document_parses_every_node_kind(example_document: dict[str, Any]) -> None:
    definition = parse_definition(example_document)

    assert definition.id == "customer-onboarding"
    assert definition.version == 4
    assert definition.owner == "@sales"
    assert definition.channel == "channel://onboarding"
    assert definition.budget is not None
    assert definition.budget.tokens == 400_000
    assert definition.input_spec is not None
    assert definition.input_spec.schema_ref == "schemas/onboarding_input.json"
    assert definition.node_ids == (
        "collect",
        "verify",
        "risk_router",
        "manual_review",
        "provision",
        "qa",
        "revise",
    )

    kinds = {node.id: node.kind for node in definition.nodes}
    assert kinds == {
        "collect": "agent",
        "verify": "tool",
        "risk_router": "router",
        "manual_review": "gate",
        "provision": "script",
        "qa": "agent",
        "revise": "agent",
    }


def test_each_kind_parses_to_its_own_typed_model(example_document: dict[str, Any]) -> None:
    definition = parse_definition(example_document)

    collect = definition.node_by_id("collect")
    assert isinstance(collect, AgentNode)
    assert collect.prompt == "prompts/collect.md"
    assert collect.skill == "customer-intake"
    assert collect.strategy == ("react",)
    assert collect.artifacts == ("customer_record.json",)
    assert collect.timeout_s == 300
    assert collect.max_attempts == 3

    verify = definition.node_by_id("verify")
    assert isinstance(verify, ToolNode)
    assert verify.tool == "crm_lookup"
    assert verify.args == {"domain": "$nodes.collect.output.company_domain"}
    assert verify.needs == ("collect",)

    router = definition.node_by_id("risk_router")
    assert isinstance(router, RouterNode)
    assert router.mode == "rules"
    assert [route.to for route in router.routes] == ["provision", "manual_review"]
    assert router.routes[1].default is True

    gate = definition.node_by_id("manual_review")
    assert isinstance(gate, GateNode)
    assert gate.gate == "human:approve_high_risk"

    script = definition.node_by_id("provision")
    assert isinstance(script, ScriptNode)
    assert script.script == "scripts/provision.py"


def test_join_defaults_to_all_and_any_is_honoured(example_document: dict[str, Any]) -> None:
    definition = parse_definition(example_document)

    assert definition.node_by_id("collect").join == "all"
    assert definition.node_by_id("qa").join == "any"


def test_loop_fields_are_carried(example_document: dict[str, Any]) -> None:
    revise = parse_definition(example_document).node_by_id("revise")

    assert revise.loop_back_to == "provision"
    assert revise.max_iterations == 3
    assert revise.when == "$nodes.qa.output.verdict == 'revise'"


def test_schema_version_defaults_to_the_current_language_version() -> None:
    definition = parse_definition(minimal_document())

    assert definition.schema_version == SCHEMA_VERSION


def test_unknown_schema_version_major_is_refused() -> None:
    with pytest.raises(WorkflowParseError) as excinfo:
        parse_definition(minimal_document(schema_version="2.0"))

    issue = excinfo.value.issues[0]
    assert issue.field == "schema_version"
    assert issue.observed == "2.0"
    assert SCHEMA_VERSION in issue.admissible


def test_a_newer_minor_of_the_same_major_is_accepted() -> None:
    definition = parse_definition(minimal_document(schema_version="1.7"))

    assert definition.schema_version == "1.7"


def test_models_are_frozen(example_document: dict[str, Any]) -> None:
    definition = parse_definition(example_document)

    with pytest.raises(ValidationError):
        definition.nodes[0].id = "renamed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        definition.version = 99  # type: ignore[misc]


def test_no_llm_wire_control_fields_exist_on_a_node() -> None:
    """Model config lives with the agent, never in the workflow (DESIGN §4)."""
    for model in (AgentNode, ToolNode, ScriptNode, RouterNode, GateNode):
        forbidden = {"model", "temperature", "top_p", "max_tokens", "provider"}
        assert not forbidden & set(model.model_fields)


def test_unknown_node_field_is_refused_not_silently_dropped() -> None:
    doc = minimal_document()
    doc["node"][0]["temperature"] = 0.7

    with pytest.raises(WorkflowParseError):
        parse_definition(doc)


def test_unknown_node_kind_is_refused_with_admissible_kinds() -> None:
    doc = minimal_document()
    doc["node"][0]["kind"] = "wizard"

    with pytest.raises(WorkflowParseError) as excinfo:
        parse_definition(doc)

    assert any("agent" in issue.admissible for issue in excinfo.value.issues)


def test_a_tool_node_without_a_tool_is_refused() -> None:
    doc = minimal_document()
    doc["node"][0] = {"id": "a", "kind": "tool"}

    with pytest.raises(WorkflowParseError):
        parse_definition(doc)


def test_node_count_quota_is_enforced_at_parse() -> None:
    doc = minimal_document()
    doc["node"] = [
        {"id": f"n{i}", "kind": "agent", "agent": "@sales"} for i in range(MAX_NODES + 1)
    ]

    with pytest.raises(WorkflowParseError) as excinfo:
        parse_definition(doc)

    assert any(issue.field == "node" for issue in excinfo.value.issues)


def test_a_document_without_nodes_is_refused() -> None:
    with pytest.raises(WorkflowParseError):
        parse_definition({"workflow": {"id": "empty", "owner": "@sales"}})


def test_canonical_document_is_insensitive_to_node_and_needs_ordering() -> None:
    doc = minimal_document()
    doc["node"].append({"id": "c", "kind": "agent", "agent": "@sales", "needs": ["a", "b"]})
    reordered = minimal_document()
    reordered["node"] = [
        {"id": "c", "kind": "agent", "agent": "@sales", "needs": ["b", "a"]},
        reordered["node"][1],
        reordered["node"][0],
    ]

    assert (
        parse_definition(doc).canonical_document()
        == parse_definition(reordered).canonical_document()
    )


def test_to_document_round_trips_through_parse(example_document: dict[str, Any]) -> None:
    definition = parse_definition(example_document)

    assert parse_definition(definition.to_document()) == definition


def test_display_name_defaults_to_none_when_absent(example_document: dict[str, Any]) -> None:
    definition = parse_definition(example_document)

    assert definition.name is None
    # TOML cannot hold None, so an unset name never appears in the document.
    assert "name" not in definition.to_document()["workflow"]


def test_display_name_round_trips_through_parse_and_serialize(
    example_document: dict[str, Any],
) -> None:
    example_document["workflow"]["name"] = "Customer Onboarding"
    definition = parse_definition(example_document)

    assert definition.name == "Customer Onboarding"
    document = definition.to_document()
    assert document["workflow"]["name"] == "Customer Onboarding"
    # The label is part of the signed canonical projection, and it survives the
    # full parse -> serialize -> parse round trip unchanged.
    assert parse_definition(document) == definition
    assert definition.canonical_document()["workflow"]["name"] == "Customer Onboarding"
