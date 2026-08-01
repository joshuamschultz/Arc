"""T-830 — GraphValidator tests (COMP-002, REQ-219/REQ-220).

The headline case is the deadlock the design's own example shipped with: ``qa``
needs both ``provision`` and ``manual_review``, but the router upstream
guarantees exactly one of them runs. Under all-needs-must-be-satisfied that
node waits forever. It is caught statically here, not discovered in production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcteam.workflow import (
    MAX_NODES,
    KnownReferences,
    ValidationIssue,
    parse_definition,
    validate_definition,
)

from .conftest import minimal_document


def _issues(document: dict[str, Any], **kwargs: Any) -> tuple[ValidationIssue, ...]:
    return validate_definition(parse_definition(document), **kwargs)


def _errors(document: dict[str, Any], **kwargs: Any) -> set[str]:
    return {issue.error for issue in _issues(document, **kwargs)}


def _fields(document: dict[str, Any], **kwargs: Any) -> set[tuple[str | None, str]]:
    return {(issue.node_id, issue.field) for issue in _issues(document, **kwargs)}


# --- the happy path ----------------------------------------------------------


def test_the_example_graph_validates(example_document: dict[str, Any]) -> None:
    assert _issues(example_document) == ()


def test_a_minimal_linear_graph_validates() -> None:
    assert _issues(minimal_document()) == ()


# --- join / router exclusivity: THE deadlock --------------------------------


def test_needs_spanning_exclusive_router_routes_without_join_any_is_rejected(
    example_document: dict[str, Any],
) -> None:
    """The DESIGN §4 defect: qa would wait forever under join=all."""
    qa = next(node for node in example_document["node"] if node["id"] == "qa")
    del qa["join"]

    issues = _issues(example_document)

    assert [(i.node_id, i.field) for i in issues] == [("qa", "join")]
    assert issues[0].observed == "all"
    assert "any" in issues[0].admissible


def test_join_any_admits_the_merge_of_two_exclusive_branches(
    example_document: dict[str, Any],
) -> None:
    assert _issues(example_document) == ()


def test_needs_from_one_branch_only_does_not_require_join_any() -> None:
    document = {
        "workflow": {"id": "one-branch", "owner": "@a"},
        "node": [
            {"id": "start", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["start"],
                "routes": [
                    {"to": "left", "when": "$input.x == 1"},
                    {"to": "right", "default": True},
                ],
            },
            {"id": "left", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {"id": "left2", "kind": "agent", "agent": "@a", "needs": ["left"]},
            {"id": "right", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {"id": "merge", "kind": "agent", "agent": "@a", "needs": ["left", "left2"]},
        ],
    }

    assert _issues(document) == ()


def test_exclusivity_is_detected_through_transitive_descendants() -> None:
    """The merge point may sit several hops below each exclusive branch."""
    document = {
        "workflow": {"id": "deep-merge", "owner": "@a"},
        "node": [
            {"id": "start", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["start"],
                "routes": [{"to": "l1", "when": "$input.x == 1"}, {"to": "r1", "default": True}],
            },
            {"id": "l1", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {"id": "l2", "kind": "agent", "agent": "@a", "needs": ["l1"]},
            {"id": "r1", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {"id": "r2", "kind": "agent", "agent": "@a", "needs": ["r1"]},
            {"id": "merge", "kind": "agent", "agent": "@a", "needs": ["l2", "r2"]},
        ],
    }

    assert ("merge", "join") in _fields(document)


# --- cycles ------------------------------------------------------------------


def test_an_undeclared_cycle_is_rejected() -> None:
    document = {
        "workflow": {"id": "cyclic", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a", "needs": ["c"]},
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["a"]},
            {"id": "c", "kind": "agent", "agent": "@a", "needs": ["b"]},
        ],
    }

    issues = _issues(document)

    assert issues
    assert all(issue.field == "needs" for issue in issues)
    assert any("undeclared cycle" in issue.error for issue in issues)


def test_a_self_dependency_is_rejected() -> None:
    document = minimal_document()
    document["node"][1]["needs"] = ["a", "b"]

    assert ("b", "needs") in _fields(document)


def test_a_declared_bounded_loop_is_accepted(example_document: dict[str, Any]) -> None:
    assert _issues(example_document) == ()


def test_a_loop_without_max_iterations_is_rejected(example_document: dict[str, Any]) -> None:
    revise = next(node for node in example_document["node"] if node["id"] == "revise")
    del revise["max_iterations"]

    assert ("revise", "max_iterations") in _fields(example_document)


def test_two_back_edges_into_one_cycle_are_rejected_as_overlapping_loops() -> None:
    document = {
        "workflow": {"id": "overlap", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["a"]},
            {
                "id": "c",
                "kind": "agent",
                "agent": "@a",
                "needs": ["b"],
                "loop_back_to": "b",
                "max_iterations": 2,
            },
            {
                "id": "d",
                "kind": "agent",
                "agent": "@a",
                "needs": ["c"],
                "loop_back_to": "b",
                "max_iterations": 2,
            },
        ],
    }

    issues = _issues(document)

    assert any("exactly one declared" in issue.error for issue in issues)


def test_loop_back_to_a_node_that_is_not_an_ancestor_is_rejected() -> None:
    document = {
        "workflow": {"id": "stray-loop", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["a"]},
            {"id": "c", "kind": "agent", "agent": "@a"},
            {
                "id": "d",
                "kind": "agent",
                "agent": "@a",
                "needs": ["b"],
                "loop_back_to": "c",
                "max_iterations": 2,
            },
        ],
    }

    assert ("d", "loop_back_to") in _fields(document)


def test_loop_back_to_an_unknown_node_is_rejected() -> None:
    document = minimal_document()
    document["node"][1]["loop_back_to"] = "ghost"
    document["node"][1]["max_iterations"] = 2

    assert ("b", "loop_back_to") in _fields(document)


def test_max_iterations_without_a_loop_is_rejected() -> None:
    document = minimal_document()
    document["node"][1]["max_iterations"] = 3

    assert ("b", "max_iterations") in _fields(document)


# --- dangling and unknown references ----------------------------------------


def test_a_dangling_need_is_rejected_with_the_known_ids_as_admissible() -> None:
    document = minimal_document()
    document["node"][1]["needs"] = ["ghost"]

    issues = _issues(document)

    assert [(i.node_id, i.field) for i in issues] == [("b", "needs")]
    assert issues[0].observed == "ghost"
    assert "a" in issues[0].admissible


def test_duplicate_node_ids_are_rejected() -> None:
    document = minimal_document()
    document["node"][1]["id"] = "a"

    assert ("a", "id") in _fields(document)


def test_a_route_to_an_unknown_node_is_rejected() -> None:
    document = {
        "workflow": {"id": "badroute", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["a"],
                "routes": [{"to": "ghost", "default": True}],
            },
        ],
    }

    assert ("r", "routes") in _fields(document)


def test_a_route_target_must_declare_the_router_in_its_needs() -> None:
    document = {
        "workflow": {"id": "unwired", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["a"],
                "routes": [{"to": "b", "default": True}],
            },
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["a"]},
        ],
    }

    assert ("r", "routes") in _fields(document)


def test_a_rules_router_route_without_a_predicate_or_default_is_rejected() -> None:
    document = {
        "workflow": {"id": "nowhen", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {"id": "r", "kind": "router", "needs": ["a"], "routes": [{"to": "b"}]},
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["r"]},
        ],
    }

    assert ("r", "routes") in _fields(document)


def test_two_default_routes_are_rejected() -> None:
    document = {
        "workflow": {"id": "twodefaults", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["a"],
                "routes": [{"to": "b", "default": True}, {"to": "c", "default": True}],
            },
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {"id": "c", "kind": "agent", "agent": "@a", "needs": ["r"]},
        ],
    }

    assert ("r", "routes") in _fields(document)


def test_an_unparseable_when_predicate_is_rejected() -> None:
    document = minimal_document()
    document["node"][1]["when"] = "__import__('os').system('x')"

    assert ("b", "when") in _fields(document)


def test_an_unparseable_route_predicate_is_rejected() -> None:
    document = {
        "workflow": {"id": "badpred", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["a"],
                "routes": [{"to": "b", "when": "len($input.x) > 1"}, {"to": "c", "default": True}],
            },
            {"id": "b", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {"id": "c", "kind": "agent", "agent": "@a", "needs": ["r"]},
        ],
    }

    assert ("r", "routes") in _fields(document)


# --- statically unsatisfiable output references ------------------------------


def test_referencing_a_node_that_is_not_an_ancestor_is_rejected() -> None:
    document = minimal_document()
    document["node"].append(
        {"id": "c", "kind": "agent", "agent": "@a", "when": "$nodes.b.output.x == 1"}
    )

    issues = _issues(document)

    assert ("c", "when") in {(i.node_id, i.field) for i in issues}


def test_referencing_a_node_on_a_mutually_exclusive_branch_is_rejected() -> None:
    """The node cannot co-occur with its source, so the reference never binds."""
    document = {
        "workflow": {"id": "crossbranch", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "r",
                "kind": "router",
                "needs": ["a"],
                "routes": [
                    {"to": "left", "when": "$input.x == 1"},
                    {"to": "right", "default": True},
                ],
            },
            {"id": "left", "kind": "agent", "agent": "@a", "needs": ["r"]},
            {
                "id": "right",
                "kind": "tool",
                "tool": "t",
                "agent": "@a",
                "needs": ["r"],
                "args": {"v": "$nodes.left.output.value"},
            },
        ],
    }

    issues = _issues(document)

    assert ("right", "args") in {(i.node_id, i.field) for i in issues}
    assert any(
        "cannot co-occur" in issue.error or "not a dependency" in issue.error for issue in issues
    )


def test_referencing_an_unknown_node_in_args_is_rejected() -> None:
    document = {
        "workflow": {"id": "badargs", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "b",
                "kind": "tool",
                "tool": "t",
                "agent": "@a",
                "needs": ["a"],
                "args": {"v": "$nodes.ghost.output.value"},
            },
        ],
    }

    assert ("b", "args") in _fields(document)


def test_an_embedded_reference_in_args_is_rejected_at_validation_time() -> None:
    document = {
        "workflow": {"id": "interp", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "b",
                "kind": "tool",
                "tool": "t",
                "agent": "@a",
                "needs": ["a"],
                "args": {"cmd": "curl https://$nodes.a.output.host"},
            },
        ],
    }

    assert ("b", "args") in _fields(document)


def test_a_reference_to_an_ancestor_is_accepted() -> None:
    document = {
        "workflow": {"id": "good", "owner": "@a"},
        "node": [
            {"id": "a", "kind": "agent", "agent": "@a"},
            {
                "id": "b",
                "kind": "tool",
                "tool": "t",
                "agent": "@a",
                "needs": ["a"],
                "args": {"v": "$nodes.a.output.value"},
            },
        ],
    }

    assert _issues(document) == ()


# --- known-reference resolution ---------------------------------------------


def test_an_unknown_agent_is_rejected_with_the_roster_as_admissible() -> None:
    known = KnownReferences(agents=frozenset({"@a", "@b"}), tools=frozenset(), skills=frozenset())
    document = minimal_document()
    document["node"][0]["agent"] = "@nobody"

    issues = validate_definition(parse_definition(document), known=known)

    assert ("a", "agent") in {(i.node_id, i.field) for i in issues}
    assert set(next(i for i in issues if i.field == "agent").admissible) == {"@a", "@b"}


def test_an_unknown_tool_is_rejected() -> None:
    known = KnownReferences(
        agents=frozenset({"@a"}), tools=frozenset({"crm_lookup"}), skills=frozenset()
    )
    document = {
        "workflow": {"id": "badtool", "owner": "@a"},
        "node": [{"id": "a", "kind": "tool", "tool": "nope", "agent": "@a"}],
    }

    issues = validate_definition(parse_definition(document), known=known)

    assert ("a", "tool") in {(i.node_id, i.field) for i in issues}


def test_an_unknown_skill_is_rejected() -> None:
    known = KnownReferences(
        agents=frozenset({"@a"}), tools=frozenset(), skills=frozenset({"intake"})
    )
    document = minimal_document()
    document["node"][0]["skill"] = "nope"

    issues = validate_definition(parse_definition(document), known=known)

    assert ("a", "skill") in {(i.node_id, i.field) for i in issues}


def test_an_unknown_workflow_owner_is_rejected() -> None:
    known = KnownReferences(agents=frozenset({"@a"}), tools=frozenset(), skills=frozenset())
    document = minimal_document()
    document["workflow"]["owner"] = "@ghost"
    for node in document["node"]:
        node["agent"] = "@a"

    issues = validate_definition(parse_definition(document), known=known)

    assert (None, "owner") in {(i.node_id, i.field) for i in issues}


# --- file references ---------------------------------------------------------


def test_a_missing_schema_file_is_rejected(tmp_path: Path) -> None:
    document = minimal_document()
    document["node"][0]["output_schema"] = "schemas/absent.json"

    issues = validate_definition(parse_definition(document), bundle_root=tmp_path)

    assert ("a", "output_schema") in {(i.node_id, i.field) for i in issues}


def test_a_present_schema_prompt_and_script_resolve(tmp_path: Path) -> None:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "schemas" / "out.json").write_text("{}")
    (tmp_path / "schemas" / "in.json").write_text("{}")
    (tmp_path / "prompts" / "p.md").write_text("do the thing")
    (tmp_path / "scripts" / "s.py").write_text("pass")
    document = {
        "workflow": {"id": "files", "owner": "@a"},
        "input": {"schema": "schemas/in.json"},
        "node": [
            {
                "id": "a",
                "kind": "agent",
                "agent": "@a",
                "prompt": "prompts/p.md",
                "output_schema": "schemas/out.json",
            },
            {"id": "b", "kind": "script", "script": "scripts/s.py", "agent": "@a", "needs": ["a"]},
        ],
    }

    assert validate_definition(parse_definition(document), bundle_root=tmp_path) == ()


@pytest.mark.parametrize("escape", ["../secrets.json", "/etc/passwd", "schemas/../../x.json"])
def test_a_file_reference_escaping_the_bundle_is_rejected(tmp_path: Path, escape: str) -> None:
    document = minimal_document()
    document["node"][0]["output_schema"] = escape

    issues = validate_definition(parse_definition(document), bundle_root=tmp_path)

    assert ("a", "output_schema") in {(i.node_id, i.field) for i in issues}


def test_an_artifact_path_escaping_the_workspace_is_rejected(tmp_path: Path) -> None:
    document = minimal_document()
    document["node"][0]["artifacts"] = ["../../etc/passwd"]

    issues = validate_definition(parse_definition(document), bundle_root=tmp_path)

    assert ("a", "artifacts") in {(i.node_id, i.field) for i in issues}


# --- quotas ------------------------------------------------------------------


def test_the_definition_size_quota_is_enforced() -> None:
    issues = validate_definition(parse_definition(minimal_document()), raw_size_bytes=1024 * 1024)

    assert (None, "size") in {(i.node_id, i.field) for i in issues}


def test_the_node_count_quota_is_reported_by_the_validator_too() -> None:
    """Parse refuses over-quota documents, so the validator guards the in-memory path."""
    definition = parse_definition(minimal_document())
    assert len(definition.nodes) <= MAX_NODES


# --- issue shape -------------------------------------------------------------


def test_every_issue_carries_the_repair_fields() -> None:
    document = minimal_document()
    document["node"][1]["needs"] = ["ghost"]

    for issue in _issues(document):
        assert issue.field
        assert issue.error
        assert isinstance(issue.admissible, tuple)


def test_all_problems_are_reported_together_not_one_at_a_time() -> None:
    document = minimal_document()
    document["node"][1]["needs"] = ["ghost"]
    document["node"][1]["when"] = "len($input.x) > 1"

    assert len(_issues(document)) >= 2
