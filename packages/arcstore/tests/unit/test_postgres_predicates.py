"""PostgreSQL mutable-plane predicate compilation."""

from __future__ import annotations

from typing import Any

from arcstore.backends.postgres import _where_clauses


def test_where_clauses_parameterize_dotted_paths_and_json_values() -> None:
    params: list[Any] = []

    clauses = _where_clauses(
        {"metadata.flow_run_id": "run-1", "status": None},
        params,
    )

    assert clauses == [
        "value #> $1::text[] IS NOT DISTINCT FROM $2::jsonb",
        "value #> $3::text[] IS NOT DISTINCT FROM $4::jsonb",
    ]
    assert params == [["metadata", "flow_run_id"], '"run-1"', ["status"], "null"]


def test_where_clauses_compile_cross_row_guards_with_the_same_dotted_semantics() -> None:
    params: list[Any] = []

    clauses = _where_clauses(
        {"metadata.owner": "did:arc:agent:one"}, params, value_column="m2.value"
    )

    assert clauses == ["m2.value #> $1::text[] IS NOT DISTINCT FROM $2::jsonb"]
    assert params == [["metadata", "owner"], '"did:arc:agent:one"']
