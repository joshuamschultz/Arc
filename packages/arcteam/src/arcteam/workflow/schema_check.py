"""COMP-010 — SchemaCheck: the one JSON Schema helper (SPEC-088 REQ-605/611/614).

``start_run``, ``fire_event``, ``resolve_gate`` and the validator all validate
through here, so a payload is judged by exactly one set of rules:

* :func:`load_schema` reads a schema from a verified bundle. The ref is confined
  to the bundle (no ``..``, no absolute path, no symlink out), the file is capped
  at 64 KiB, and it must be a valid Draft 2020-12 schema.
* :func:`check` returns every failing field with its dotted path, capped at 50
  issues. Before any schema work the instance itself is bounded: serialized size
  at most 64 KiB and nesting depth at most 16, so a hostile payload cannot pin
  the loop or blow the stack (LLM10).

A remote ``$ref`` is never fetched: the validator gets an empty registry whose
retrieval always fails, so a schema can never turn into an outbound request (SSRF).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from itertools import islice
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, validators
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from arcteam.workflow.errors import ValidationIssue

MAX_SCHEMA_BYTES = 64 * 1024
"""Largest schema file :func:`load_schema` reads."""

MAX_INSTANCE_BYTES = 64 * 1024
"""Largest serialized instance :func:`check` validates."""

MAX_INSTANCE_DEPTH = 16
"""Deepest container nesting :func:`check` validates; ``{"a": 1}`` is depth 1."""

MAX_ISSUES = 50
"""Most issues one :func:`check` reports."""


class SchemaRefusedError(ValueError):
    """A schema ref or schema file that may not be used."""


def load_schema(bundle_root: Path, ref: str) -> dict[str, Any]:
    """Read and verify the schema ``ref`` names inside ``bundle_root``.

    Raises:
        SchemaRefusedError: the ref escapes the bundle, the file is missing or
            over 64 KiB, or it is not a valid Draft 2020-12 schema object.
    """
    target = _confined_file(bundle_root, ref)
    with target.open("rb") as handle:
        # Read one byte past the cap, not stat-then-read: the file cannot grow
        # between the size check and the read.
        raw = handle.read(MAX_SCHEMA_BYTES + 1)
    if len(raw) > MAX_SCHEMA_BYTES:
        raise SchemaRefusedError(f"schema {ref!r} is larger than {MAX_SCHEMA_BYTES} bytes")
    try:
        schema = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaRefusedError(f"schema {ref!r} is not valid JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise SchemaRefusedError(f"schema {ref!r} must be a JSON object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SchemaRefusedError(
            f"schema {ref!r} is not a valid Draft 2020-12 schema: {exc.message}"
        ) from exc
    return schema


def check(instance: Any, schema: Mapping[str, Any]) -> tuple[ValidationIssue, ...]:
    """Validate ``instance`` against ``schema``; empty when valid.

    Every failing field is reported with its dotted path (list indices as
    integers, e.g. ``order.lines.1.qty``), at most :data:`MAX_ISSUES` of them.
    """
    guard = _instance_guard(instance)
    if guard is not None:
        return (guard,)
    validator = _Validator(schema, registry=_NO_RETRIEVAL)
    try:
        errors = list(islice(validator.iter_errors(instance), MAX_ISSUES))
    except Unresolvable as exc:
        return (
            ValidationIssue(
                field="$ref",
                error=f"schema reference cannot be resolved ({exc}); remote refs are not fetched",
                admissible=("a $ref inside the same schema",),
            ),
        )
    except RecursionError:
        return (ValidationIssue(field="$schema", error="schema recursion is too deep"),)
    return tuple(_issue(error) for error in errors)


# --- confinement ---------------------------------------------------------------


def _confined_file(bundle_root: Path, ref: str) -> Path:
    """Resolve ``ref`` to a regular file inside the bundle, or refuse."""
    candidate = Path(ref)
    if not ref or candidate.is_absolute() or ".." in candidate.parts:
        raise SchemaRefusedError(f"schema ref {ref!r} must be a relative path inside the bundle")
    root = bundle_root.resolve()
    # resolve() follows every symlink, so a link (file or directory) that points
    # out of the bundle lands outside ``root`` and is refused here.
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise SchemaRefusedError(f"schema ref {ref!r} resolves outside the bundle")
    if not target.is_file():
        raise SchemaRefusedError(f"schema ref {ref!r} is not a file in the bundle")
    return target


# --- instance guards -------------------------------------------------------------


def _instance_guard(instance: Any) -> ValidationIssue | None:
    """Refuse an instance too deep or too large to validate safely.

    Depth is measured first and iteratively: serializing a very deep value
    would itself raise ``RecursionError``.
    """
    depth = _depth_exceeding(instance, MAX_INSTANCE_DEPTH)
    if depth is not None:
        return ValidationIssue(
            field="input",
            error=f"input nests deeper than {MAX_INSTANCE_DEPTH} levels",
            observed=depth,
            admissible=(f"<= {MAX_INSTANCE_DEPTH} levels",),
        )
    try:
        size = len(json.dumps(instance, separators=(",", ":"), ensure_ascii=False).encode())
    except (TypeError, ValueError) as exc:
        return ValidationIssue(field="input", error=f"input is not JSON: {exc}")
    if size > MAX_INSTANCE_BYTES:
        return ValidationIssue(
            field="input",
            error=f"input is larger than {MAX_INSTANCE_BYTES} bytes",
            observed=size,
            admissible=(f"<= {MAX_INSTANCE_BYTES} bytes",),
        )
    return None


def _depth_exceeding(instance: Any, limit: int) -> int | None:
    """The first container depth past ``limit``, or ``None`` if within it."""
    stack: list[tuple[Any, int]] = [(instance, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            children: Iterator[Any] = iter(value.values())
        elif isinstance(value, list | tuple):
            children = iter(value)
        else:
            continue
        if depth + 1 > limit:
            return depth + 1
        stack.extend((child, depth + 1) for child in children)
    return None


# --- validation ----------------------------------------------------------------


# An empty registry whose retrieval always fails (referencing's default): any
# ``$ref`` outside the schema itself is unresolvable, never fetched.
_NO_RETRIEVAL: Registry[Any] = Registry()


def _required_at_field(
    validator: Any, required: Any, instance: Any, schema: Any
) -> Iterator[ValidationError]:
    """``required`` that reports the missing property's own path.

    The stock keyword reports a missing field at its parent object, so two
    missing fields would share one path and the caller could not say which
    field to repair.
    """
    if not validator.is_type(instance, "object"):
        return
    for name in required:
        if name not in instance:
            yield ValidationError(f"{name!r} is a required property", path=[name])


# The jsonschema stubs leave ``extend`` unannotated; it returns a validator class.
_Validator = validators.extend(  # type: ignore[no-untyped-call]  # unannotated in stubs
    Draft202012Validator, {"required": _required_at_field}
)


def _issue(error: ValidationError) -> ValidationIssue:
    field = ".".join(str(part) for part in error.absolute_path)
    return ValidationIssue(
        field=field or "input",
        error=error.message,
        observed=error.instance if error.validator != "required" else None,
    )


__all__ = [
    "MAX_INSTANCE_BYTES",
    "MAX_INSTANCE_DEPTH",
    "MAX_ISSUES",
    "MAX_SCHEMA_BYTES",
    "SchemaRefusedError",
    "check",
    "load_schema",
]
