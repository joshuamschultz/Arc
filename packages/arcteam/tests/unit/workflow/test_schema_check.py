"""T-1339 — SchemaCheck (SPEC-088 COMP-010; REQ-605, REQ-611, REQ-614).

``arcteam.workflow.schema_check`` is the one helper start_run, fire_event,
resolve_gate and the validator share:

* ``load_schema(bundle_root, ref) -> dict`` — confined to the bundle, 64 KiB
  cap, must be a valid Draft 2020-12 schema. Refusals raise ``ValueError`` (or
  a subclass), so a caller can never mistake a refused ref for a schema.
* ``check(instance, schema) -> tuple[ValidationIssue, ...]`` — every failing
  field with its path, capped at 50; empty when valid. Instance guards refuse a
  serialized size over 64 KiB and nesting depth over 16.

Path convention asserted here: dotted, list indices as integers
(``order.lines.1.qty``), matching the rest of arcteam's ``ValidationIssue.field``.
Depth convention: the number of nested containers; ``{"a": 1}`` is depth 1.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from arcteam.workflow.errors import ValidationIssue

KIB = 1024
_VALID_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"qty": {"type": "integer"}},
    "required": ["qty"],
}


def _schema_check() -> Any:
    from arcteam.workflow import schema_check

    return schema_check


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "order.json").write_text(json.dumps(_VALID_SCHEMA))
    return root


def _nested_dict(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {"leaf": 1}
    for _ in range(depth - 1):
        value = {"a": value}
    return value


def _nested_list(depth: int) -> list[Any]:
    value: list[Any] = [1]
    for _ in range(depth - 1):
        value = [value]
    return value


# --- load_schema: happy path -------------------------------------------------


def test_load_schema_returns_the_schema_object_for_a_confined_ref(bundle: Path) -> None:
    loaded = _schema_check().load_schema(bundle, "schemas/order.json")

    assert loaded == _VALID_SCHEMA


# --- load_schema: confinement (REQ-605) --------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "../outside.json",
        "schemas/../../outside.json",
        "schemas/../../../etc/passwd",
    ],
)
def test_load_schema_refuses_dotdot_ref_escaping_the_bundle(bundle: Path, ref: str) -> None:
    (bundle.parent / "outside.json").write_text(json.dumps(_VALID_SCHEMA))

    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, ref)


def test_load_schema_refuses_absolute_ref_even_when_it_points_inside(bundle: Path) -> None:
    absolute = str((bundle / "schemas" / "order.json").resolve())

    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, absolute)


def test_load_schema_refuses_symlinked_file_pointing_outside(bundle: Path) -> None:
    outside = bundle.parent / "secret.json"
    outside.write_text(json.dumps(_VALID_SCHEMA))
    os.symlink(outside, bundle / "schemas" / "link.json")

    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, "schemas/link.json")


def test_load_schema_refuses_symlinked_directory_pointing_outside(bundle: Path) -> None:
    outside_dir = bundle.parent / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "order.json").write_text(json.dumps(_VALID_SCHEMA))
    os.symlink(outside_dir, bundle / "linked")

    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, "linked/order.json")


def test_load_schema_refuses_missing_file(bundle: Path) -> None:
    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, "schemas/absent.json")


# --- load_schema: content -----------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"type": 12}),  # type must be a string or array
        json.dumps({"type": "object", "required": "qty"}),  # required must be an array
        json.dumps({"minLength": -1}),
        json.dumps(["not", "an", "object"]),
        "{not json",
    ],
)
def test_load_schema_refuses_invalid_draft_2020_12_schema(bundle: Path, content: str) -> None:
    (bundle / "schemas" / "bad.json").write_text(content)

    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, "schemas/bad.json")


def test_load_schema_refuses_file_over_64_kib(bundle: Path) -> None:
    big = {"type": "object", "description": "x" * (64 * KIB + 1)}
    (bundle / "schemas" / "big.json").write_text(json.dumps(big))
    assert (bundle / "schemas" / "big.json").stat().st_size > 64 * KIB

    with pytest.raises(ValueError):
        _schema_check().load_schema(bundle, "schemas/big.json")


def test_load_schema_accepts_file_just_under_64_kib(bundle: Path) -> None:
    payload = {"type": "object", "description": "x" * (63 * KIB)}
    (bundle / "schemas" / "near.json").write_text(json.dumps(payload))

    loaded = _schema_check().load_schema(bundle, "schemas/near.json")

    assert loaded["type"] == "object"


# --- check: issues (REQ-605, REQ-611) ----------------------------------------


def test_check_returns_empty_tuple_for_a_valid_instance() -> None:
    issues = _schema_check().check({"qty": 3}, _VALID_SCHEMA)

    assert issues == ()


def test_check_returns_validation_issues() -> None:
    issues = _schema_check().check({"qty": "three"}, _VALID_SCHEMA)

    assert isinstance(issues, tuple)
    assert issues
    assert all(isinstance(issue, ValidationIssue) for issue in issues)


def test_check_reports_every_failing_field_with_its_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "string"},
            "c": {"type": "integer"},
            "ok": {"type": "string"},
        },
        "required": ["a", "b"],
    }

    issues = _schema_check().check({"c": "not-int", "ok": "fine"}, schema)

    assert {issue.field for issue in issues} == {"a", "b", "c"}


def test_check_reports_nested_and_indexed_paths() -> None:
    schema = {
        "type": "object",
        "properties": {
            "order": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"qty": {"type": "integer"}},
                        },
                    }
                },
            }
        },
    }
    instance = {"order": {"lines": [{"qty": 1}, {"qty": "x"}]}}

    issues = _schema_check().check(instance, schema)

    assert [issue.field for issue in issues] == ["order.lines.1.qty"]


def test_check_caps_issues_at_fifty() -> None:
    names = [f"f{index}" for index in range(60)]
    schema = {"type": "object", "required": names}

    issues = _schema_check().check({}, schema)

    assert len(issues) == 50


def test_check_does_not_reach_the_network_for_a_remote_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema ``$ref`` to a URL must never become an outbound fetch (SSRF)."""
    attempts: list[Any] = []

    def _refuse_connect(*args: Any, **kwargs: Any) -> None:
        attempts.append(args)
        raise AssertionError("schema check opened a network connection")

    monkeypatch.setattr(socket, "create_connection", _refuse_connect)
    monkeypatch.setattr(socket.socket, "connect", _refuse_connect)
    schema = {"$ref": "https://attacker.example/schema.json"}

    try:
        _schema_check().check({"qty": 1}, schema)
    except ValueError:
        pass

    assert attempts == []


# --- check: instance guards (REQ-614, REQ-608) -------------------------------


def test_check_refuses_instance_over_64_kib_even_with_permissive_schema() -> None:
    instance = {"s": "x" * (65 * KIB)}

    issues = _schema_check().check(instance, {})

    assert issues


def test_check_accepts_instance_just_under_64_kib() -> None:
    instance = {"s": "x" * (63 * KIB)}

    issues = _schema_check().check(instance, {})

    assert issues == ()


def test_check_accepts_dict_nesting_depth_16() -> None:
    issues = _schema_check().check(_nested_dict(16), {})

    assert issues == ()


def test_check_refuses_dict_nesting_depth_17_even_with_permissive_schema() -> None:
    issues = _schema_check().check(_nested_dict(17), {})

    assert issues


def test_check_accepts_list_nesting_depth_16() -> None:
    issues = _schema_check().check(_nested_list(16), {})

    assert issues == ()


def test_check_refuses_list_nesting_depth_17() -> None:
    issues = _schema_check().check(_nested_list(17), {})

    assert issues


def test_check_refuses_pathologically_deep_instance_without_crashing() -> None:
    """Depth well past the recursion limit must be refused, not RecursionError."""
    issues = _schema_check().check(_nested_list(5000), {})

    assert issues
