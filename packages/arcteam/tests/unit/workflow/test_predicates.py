"""T-828 — PredicateEvaluator tests (COMP-003, REQ-219).

The security claim under test is narrow and absolute: a call, an attribute
walk, an import, or an environment read is a *parse* error. It never reaches
evaluation, because there is nothing in the grammar that could evaluate it.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcteam.workflow import (
    BoolOp,
    Compare,
    PredicateEvaluationError,
    PredicateParseError,
    evaluate,
    parse_predicate,
    referenced_nodes,
)

SCOPE: dict[str, Any] = {
    "nodes": {
        "verify": {"output": {"risk": "low", "score": 42, "tags": ["a", "b"], "ok": True}},
        "qa": {"output": {"verdict": "revise", "nested": {"deep": 7}}},
    },
    "input": {"tier": "gold", "count": 3, "flag": False},
}


# --- the grammar that exists -------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("$nodes.verify.output.risk == 'low'", True),
        ('$nodes.verify.output.risk == "high"', False),
        ("$nodes.verify.output.risk != 'high'", True),
        ("$nodes.verify.output.score > 40", True),
        ("$nodes.verify.output.score >= 42", True),
        ("$nodes.verify.output.score < 40", False),
        ("$nodes.verify.output.score <= 42", True),
        ("$input.tier == 'gold'", True),
        ("$input.count == 3", True),
        ("$nodes.qa.output.nested.deep == 7", True),
        ("'a' in $nodes.verify.output.tags", True),
        ("'z' in $nodes.verify.output.tags", False),
        ("'z' not in $nodes.verify.output.tags", True),
        ("$input.tier in ['gold', 'silver']", True),
        ("$nodes.verify.output.ok", True),
        ("$input.flag", False),
        ("not $input.flag", True),
        ("$input.tier == 'gold' and $nodes.verify.output.score > 40", True),
        ("$input.tier == 'gold' and $nodes.verify.output.score > 99", False),
        ("$input.tier == 'bronze' or $nodes.verify.output.risk == 'low'", True),
        ("($input.tier == 'bronze' or $input.count == 3) and not $input.flag", True),
        ("$input.count == 3.0", True),
        ("$nodes.qa.output.nested.deep != null", True),
    ],
)
def test_grammar_evaluates(expression: str, expected: bool) -> None:
    assert evaluate(expression, SCOPE) is expected


def test_evaluate_always_returns_a_real_bool() -> None:
    """A truthy string must not leak out as a string."""
    result = evaluate("$input.tier", SCOPE)

    assert result is True
    assert isinstance(result, bool)


def test_comparison_chaining_is_not_in_the_grammar() -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate("$input.count == 3 == 3")


def test_a_path_that_looks_like_a_method_is_a_field_lookup_that_fails_closed() -> None:
    """``.lower`` is a dict key that is not there — never a bound method call."""
    with pytest.raises(PredicateEvaluationError):
        evaluate("$nodes.verify.output.risk.lower == 'low'", SCOPE)


def test_parsed_ast_is_only_whitelisted_node_types() -> None:
    predicate = parse_predicate("$input.tier == 'gold' and $nodes.qa.output.verdict == 'revise'")

    assert isinstance(predicate, BoolOp)
    assert predicate.op == "and"
    assert all(isinstance(operand, Compare) for operand in predicate.operands)


def test_referenced_nodes_reports_every_upstream_the_predicate_reads() -> None:
    predicate = parse_predicate(
        "$nodes.verify.output.risk == 'low' or $nodes.qa.output.verdict == 'revise'"
    )

    assert referenced_nodes(predicate) == frozenset({"verify", "qa"})
    assert referenced_nodes(parse_predicate("$input.tier == 'gold'")) == frozenset()


# --- the grammar that must not exist ----------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "len($input.tier) > 2",
        "$input.tier.upper() == 'GOLD'",
        "__import__('os').system('rm -rf /')",
        "import os",
        "os.environ['HOME']",
        "$input.__class__",
        "$input.tier.__class__.__mro__",
        "open('/etc/passwd')",
        "eval('1+1') == 2",
        "lambda: 1",
        "[x for x in $input.tier]",
        "$input.count + 1 == 4",
        "$input.count if True else 0",
        "True; import os",
        "exec('x=1')",
        "$env.HOME == '/root'",
        "$nodes.verify.output.risk = 'low'",
        "globals()",
        "{'a': 1}",
    ],
)
def test_dangerous_constructs_are_parse_errors_not_evaluations(expression: str) -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate(expression)

    with pytest.raises(PredicateParseError):
        evaluate(expression, SCOPE)


def test_call_syntax_is_rejected_even_on_a_valid_path() -> None:
    """A path followed by ``(`` must fail at parse, never bind and then call."""
    with pytest.raises(PredicateParseError):
        parse_predicate("$nodes.verify.output.risk()")


def test_dunder_path_segments_are_rejected() -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate("$nodes.verify.output._private == 1")


def test_unknown_root_is_rejected() -> None:
    """Only ``$nodes`` and ``$input`` exist; there is no clock or environment."""
    for expression in ("$now > 0", "$random.value == 1", "$secrets.token == 'x'"):
        with pytest.raises(PredicateParseError):
            parse_predicate(expression)


def test_trailing_garbage_is_rejected() -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate("$input.count == 3 garbage")


def test_empty_expression_is_rejected() -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate("   ")


def test_an_over_long_expression_is_refused_before_parsing() -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate("$input.count == 3 and " * 500 + "$input.count == 3")


def test_deeply_nested_parentheses_are_refused() -> None:
    with pytest.raises(PredicateParseError):
        parse_predicate("(" * 60 + "$input.flag" + ")" * 60)


# --- evaluation-time failures are fail-closed, not silently false ------------


def test_a_missing_node_raises_rather_than_evaluating_false() -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate("$nodes.absent.output.risk == 'low'", SCOPE)


def test_a_missing_field_raises_rather_than_evaluating_false() -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate("$nodes.verify.output.absent == 'low'", SCOPE)


def test_traversing_into_a_non_mapping_raises() -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate("$nodes.verify.output.risk.deeper == 'low'", SCOPE)


def test_ordering_comparison_across_incomparable_types_raises() -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate("$nodes.verify.output.risk > 3", SCOPE)


def test_membership_against_a_non_container_raises() -> None:
    with pytest.raises(PredicateEvaluationError):
        evaluate("'a' in $nodes.verify.output.score", SCOPE)


def test_path_resolution_uses_mapping_lookup_never_attribute_access() -> None:
    """A scope object's Python attributes are invisible to the grammar."""

    class Sneaky(dict[str, Any]):
        secret = "leaked"

    scope = {"nodes": {}, "input": Sneaky()}

    with pytest.raises(PredicateEvaluationError):
        evaluate("$input.secret == 'leaked'", scope)


def test_no_eval_or_exec_appears_in_the_predicate_module() -> None:
    """The whole point of a hand-written parser is that this stays true."""
    import inspect

    from arcteam.workflow import predicates

    source = inspect.getsource(predicates)
    for banned in ("eval(", "exec(", "__import__", "ast.parse", "getattr(", "globals("):
        assert banned not in source, f"{banned} must never appear in the predicate parser"
