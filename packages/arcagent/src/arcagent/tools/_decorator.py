"""``@tool`` decorator for dynamic tools — SPEC-017 R-051.

AI-authored tools are declared as type-annotated ``async def``
functions with a single ``@tool(description=...)`` wrapper. The
decorator:

  * Infers the JSON Schema from type hints (no hand-written schema)
  * Records classification + capability_tags on the function
  * Preserves the original callable so normal ``await fn(...)`` works

The resulting ``_arc_tool_meta`` attribute is picked up by the
dynamic tool loader at registration time.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin, get_type_hints

ToolClassification = Literal["read_only", "state_modifying"]


@dataclass(frozen=True)
class ToolMetadata:
    """Metadata attached by ``@tool`` to the decorated function.

    The loader / registry reads these fields to build a
    :class:`RegisteredTool` without needing to introspect the
    function a second time.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    classification: ToolClassification
    capability_tags: list[str] = field(default_factory=list)


def tool(
    *,
    name: str | None = None,
    description: str = "",
    classification: ToolClassification = "state_modifying",
    capability_tags: list[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that stamps tool metadata on an async function.

    ``classification`` defaults to ``state_modifying`` (fail-closed)
    so a tool added without explicit classification never races in a
    parallel batch. Override with ``"read_only"`` for safe tools.

    Usage::

        @tool(description="read a file", classification="read_only",
              capability_tags=["file_read"])
        async def read_file(path: str) -> str:
            ...
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        tool_desc = description or (fn.__doc__ or "").strip()
        schema = _schema_from_signature(fn)
        meta = ToolMetadata(
            name=tool_name,
            description=tool_desc,
            input_schema=schema,
            classification=classification,
            capability_tags=list(capability_tags or []),
        )
        # Stash on the original function so call semantics are
        # unchanged. Opaque underscore-prefixed name avoids
        # collision with user-authored attributes.
        fn._arc_tool_meta = meta  # type: ignore[attr-defined]
        return fn

    return decorator


# --- Schema inference ------------------------------------------------------

_PY_TYPE_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    tuple: "array",
    set: "array",
}


def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build a JSON Schema ``object`` from a function's signature.

    Each parameter becomes a property. Required list holds parameters
    with no default. Unknown annotations fall back to ``"string"``
    (conservative; the loader can reject by running ``validate_call``
    at dispatch time).

    Uses :func:`typing.get_type_hints` so ``from __future__ import
    annotations`` (string-form) annotations are resolved to real
    types before lookup.
    """
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        # If forward-refs can't be resolved we fall back to raw
        # signatures — better than crashing the decorator.
        hints = {}

    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        annotation = hints.get(param_name, param.annotation)
        prop: dict[str, Any] = {"type": _json_type(annotation)}
        properties[param_name] = prop

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _json_type(annotation: Any) -> str:
    """Map a Python type annotation to a JSON Schema primitive type."""
    if annotation is inspect.Parameter.empty:
        return "string"
    if annotation in _PY_TYPE_TO_JSON:
        return _PY_TYPE_TO_JSON[annotation]
    origin = get_origin(annotation)
    if origin is not None and origin in _PY_TYPE_TO_JSON:
        return _PY_TYPE_TO_JSON[origin]
    # Optional[X] / Union — walk args, take first non-None primitive.
    if origin is not None:
        for arg in get_args(annotation):
            if arg in _PY_TYPE_TO_JSON:
                return _PY_TYPE_TO_JSON[arg]
    return "string"


__all__ = ["ToolClassification", "ToolMetadata", "tool"]
