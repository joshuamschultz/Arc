"""A deterministic stand-in output for a stubbed test-run node, built from its schema.

A test run never performs a state-modifying step, so the node needs an output
that a downstream node can read. Echoing the call back (the old behaviour) said
nothing about whether the node's declared ``output_schema`` is satisfiable, so a
draft could pass in test mode and fail live. The stub is generated from the
schema instead and then runs through the normal completion gate.

The generator is deliberately small: required fields only, a fixed placeholder
per type, first enum value, first usable branch of ``anyOf``/``oneOf``. A
construct it cannot satisfy with certainty (``pattern``, ``$ref``, ``not``, ...)
raises ``UnsatisfiableSchema`` naming where, so the test run fails loudly rather
than passing on a guess.
"""

from __future__ import annotations

from typing import Any

# Keywords that make a value unguessable from the schema alone.
_UNGENERATABLE = (
    "pattern",
    "$ref",
    "not",
    "if",
    "patternProperties",
    "format",
    "dependentSchemas",
)


class UnsatisfiableSchema(ValueError):  # noqa: N818 — reads as the condition it names
    """The schema defeats the stub generator; ``path`` says where."""

    def __init__(self, path: str, why: str) -> None:
        super().__init__(f"{path} ({why})")
        self.path = path


def stub_from_schema(schema: dict[str, Any], path: str = "<root>") -> Any:
    """A value that satisfies ``schema`` for the shapes this generator knows.

    Raises:
        UnsatisfiableSchema: the schema uses something that cannot be generated.
    """
    for keyword in _UNGENERATABLE:
        if keyword in schema:
            raise UnsatisfiableSchema(path, f"'{keyword}' cannot be generated")
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        if not schema["enum"]:
            raise UnsatisfiableSchema(path, "empty enum")
        return schema["enum"][0]
    for combinator in ("anyOf", "oneOf"):
        if combinator in schema:
            return _first_branch(schema[combinator], path)
    if "allOf" in schema:
        merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
        for part in schema["allOf"]:
            merged = {**merged, **part}
        return stub_from_schema(merged, path)
    return _typed_stub(schema, path)


def _first_branch(branches: list[dict[str, Any]], path: str) -> Any:
    for branch in branches:
        try:
            return stub_from_schema(branch, path)
        except UnsatisfiableSchema:
            continue
    raise UnsatisfiableSchema(path, "no branch can be generated")


def _typed_stub(schema: dict[str, Any], path: str) -> Any:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if kind == "object":
        return _object_stub(schema, path)
    if kind == "array":
        return _array_stub(schema, path)
    if kind == "string":
        return "x" * int(schema.get("minLength", 1))
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    raise UnsatisfiableSchema(path, f"type {kind!r} cannot be generated")


def _object_stub(schema: dict[str, Any], path: str) -> dict[str, Any]:
    properties = schema.get("properties") or {}
    stub: dict[str, Any] = {}
    for name in schema.get("required") or ():
        child = properties.get(name)
        if child is None:
            raise UnsatisfiableSchema(_join(path, name), "required but not declared")
        stub[name] = stub_from_schema(child, _join(path, name))
    return stub


def _array_stub(schema: dict[str, Any], path: str) -> list[Any]:
    count = int(schema.get("minItems", 0))
    if count == 0:
        return []
    items = schema.get("items")
    if not isinstance(items, dict):
        raise UnsatisfiableSchema(path, "minItems without an items schema")
    return [stub_from_schema(items, f"{path}[{i}]") for i in range(count)]


def _join(path: str, name: str) -> str:
    return name if path == "<root>" else f"{path}/{name}"


__all__ = ["UnsatisfiableSchema", "stub_from_schema"]
