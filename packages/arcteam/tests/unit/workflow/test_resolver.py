"""T-840 — ValueResolver tests (COMP-004, REQ-239).

The claim: a reference binds as a **typed value**. There is no code path that
produces an interpolated string, because textual interpolation of upstream
output into a prompt or a command is the injection class ArcFlow designs out
(LLM01). A string that merely embeds a reference is refused, not substituted.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcteam.workflow import (
    Reference,
    TextualInterpolationError,
    UnresolvableReferenceError,
    is_reference,
    malformed_reference_strings,
    parse_reference,
    references_in,
    resolve_args,
    resolve_value,
)

SCOPE: dict[str, Any] = {
    "nodes": {
        "collect": {
            "output": {
                "company_domain": "acme.example",
                "seats": 42,
                "active": True,
                "contacts": [{"email": "a@acme.example"}],
                "meta": {"region": "us-east"},
                "missing_value": None,
            }
        }
    },
    "input": {"priority": "high", "retries": 2},
}


# --- typed binding -----------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("$nodes.collect.output.company_domain", "acme.example"),
        ("$nodes.collect.output.seats", 42),
        ("$nodes.collect.output.active", True),
        ("$nodes.collect.output.meta", {"region": "us-east"}),
        ("$nodes.collect.output.meta.region", "us-east"),
        ("$nodes.collect.output.contacts", [{"email": "a@acme.example"}]),
        ("$nodes.collect.output.missing_value", None),
        ("$input.priority", "high"),
        ("$input.retries", 2),
    ],
)
def test_a_reference_binds_to_its_typed_value(expression: str, expected: Any) -> None:
    resolved = resolve_value(expression, SCOPE)

    assert resolved == expected
    assert type(resolved) is type(expected)


def test_an_integer_stays_an_integer_and_never_becomes_a_string() -> None:
    assert resolve_value("$nodes.collect.output.seats", SCOPE) == 42
    assert not isinstance(resolve_value("$nodes.collect.output.seats", SCOPE), str)


def test_resolve_args_binds_every_value_in_a_tool_argument_mapping() -> None:
    args = {
        "domain": "$nodes.collect.output.company_domain",
        "seats": "$nodes.collect.output.seats",
        "priority": "$input.priority",
        "literal": "not a reference",
        "number": 7,
    }

    assert resolve_args(args, SCOPE) == {
        "domain": "acme.example",
        "seats": 42,
        "priority": "high",
        "literal": "not a reference",
        "number": 7,
    }


def test_resolve_args_recurses_into_nested_structures() -> None:
    args = {
        "outer": {"inner": "$nodes.collect.output.seats"},
        "items": ["$input.priority", {"deep": "$nodes.collect.output.meta.region"}],
    }

    assert resolve_args(args, SCOPE) == {
        "outer": {"inner": 42},
        "items": ["high", {"deep": "us-east"}],
    }


def test_resolve_args_leaves_a_plain_mapping_untouched() -> None:
    args = {"a": 1, "b": ["x", "y"], "c": {"d": True}}

    assert resolve_args(args, SCOPE) == args


# --- textual interpolation is refused, never performed -----------------------


@pytest.mark.parametrize(
    "value",
    [
        "hello $nodes.collect.output.company_domain",
        "$nodes.collect.output.company_domain/path",
        "domain=$nodes.collect.output.company_domain",
        "curl https://$nodes.collect.output.company_domain",
        "{$input.priority}",
        "$input.priority and more",
        "prefix$input.priority",
    ],
)
def test_a_string_that_embeds_a_reference_is_refused(value: str) -> None:
    with pytest.raises(TextualInterpolationError):
        resolve_args({"arg": value}, SCOPE)


def test_an_embedded_reference_nested_deep_is_still_refused() -> None:
    with pytest.raises(TextualInterpolationError):
        resolve_args({"outer": {"inner": ["ok", "run $input.priority now"]}}, SCOPE)


def test_resolve_value_refuses_an_embedded_reference_too() -> None:
    with pytest.raises(TextualInterpolationError):
        resolve_value("hello $input.priority", SCOPE)


def test_no_interpolation_helper_exists_on_the_public_surface() -> None:
    """There must be no supported way to render a reference into text."""
    from arcteam.workflow import resolver

    forbidden = {"interpolate", "format_value", "render", "substitute", "expand"}
    assert not forbidden & set(vars(resolver))


# --- reference parsing and discovery ----------------------------------------


def test_is_reference_recognises_only_a_whole_string_reference() -> None:
    assert is_reference("$input.priority") is True
    assert is_reference("$nodes.collect.output.seats") is True
    assert is_reference("plain") is False
    assert is_reference(42) is False
    assert is_reference("hello $input.priority") is False


def test_parse_reference_reports_root_node_and_segments() -> None:
    reference = parse_reference("$nodes.collect.output.meta.region")

    assert reference == Reference(root="nodes", node_id="collect", segments=("meta", "region"))

    run_input = parse_reference("$input.priority")

    assert run_input == Reference(root="input", node_id=None, segments=("priority",))


def test_references_in_finds_every_reference_in_an_argument_tree() -> None:
    args = {
        "a": "$nodes.collect.output.seats",
        "b": ["$input.priority", {"c": "$nodes.other.output.x"}],
        "d": "plain",
    }

    assert {str(reference) for reference in references_in(args)} == {
        "$nodes.collect.output.seats",
        "$input.priority",
        "$nodes.other.output.x",
    }


# --- unresolvable references fail closed -------------------------------------


def test_an_unknown_node_is_unresolvable() -> None:
    with pytest.raises(UnresolvableReferenceError):
        resolve_value("$nodes.absent.output.x", SCOPE)


def test_an_unknown_field_is_unresolvable() -> None:
    with pytest.raises(UnresolvableReferenceError):
        resolve_value("$nodes.collect.output.absent", SCOPE)


def test_a_declared_null_output_resolves_rather_than_failing() -> None:
    """A field present but null is a value, not an absence."""
    assert resolve_value("$nodes.collect.output.missing_value", SCOPE) is None


REFERENCE_SHAPED = [
    "$nodes.a.output.x",
    "$nodes.a",
    "$nodes.a.b",
    "$input.x",
    "$input.",
    "$nodes.a.output",
    "$nodes.a.result.x",
    "$nodes.a.output.x/path",
    "$input.a.b.c",
    "$5.00",
    "plain",
]


@pytest.mark.parametrize("candidate", REFERENCE_SHAPED)
def test_anything_that_makes_references_in_raise_is_also_reported(candidate: str) -> None:
    """Couple the raising path to the reporting path so a swallow stays safe.

    The validator walks tool arguments with ``references_in`` inside a handler
    that returns nothing on failure. That is only harmless while every input
    capable of triggering it is separately reported by
    ``malformed_reference_strings`` — a relationship resting on three regexes
    agreeing, with nothing structural keeping them in step. Asserting it
    directly means changing one turns this red, rather than silently restoring
    a validator that calls a broken reference valid.
    """
    args = {"v": candidate}
    try:
        references_in(args)
        raised = False
    except UnresolvableReferenceError:
        raised = True

    if raised:
        assert malformed_reference_strings(args), (
            f"{candidate!r} makes references_in raise but nothing reports it — "
            f"the validator would swallow it and call the graph valid"
        )


def test_a_malformed_reference_is_refused() -> None:
    for expression in ("$nodes.collect.company_domain", "$env.HOME", "$nodes", "$input"):
        with pytest.raises((UnresolvableReferenceError, TextualInterpolationError)):
            resolve_value(expression, SCOPE)
