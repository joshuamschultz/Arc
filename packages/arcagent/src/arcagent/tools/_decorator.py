"""Capability decorators — SPEC-021 unified capability surface.

A capability is a function or class stamped with metadata that the
loader picks up and registers. Four kinds:

  * ``@tool``           — callable surface for the LLM (RegisteredTool)
  * ``@hook``           — bus subscriber (1.2)
  * ``@background_task``— interval-driven async task (1.2)
  * ``@capability`` cls — lifecycle resource bundle (1.2)

All four stamp ``func._arc_capability_meta`` with a frozen
:class:`CapabilityMetadata` instance. Loader scans for the stamp;
registry receives the metadata. Schema for ``@tool`` is inferred from
the typed signature — AI-authored tools never write JSON Schema by
hand.

The metadata is frozen (immutable post-stamp) so a malicious post-load
mutation cannot escalate ``classification`` from ``state_modifying``
to ``read_only`` or downgrade ``version``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin, get_type_hints

ToolClassification = Literal["read_only", "state_modifying"]


@dataclass(frozen=True)
class ToolMetadata:
    """Stamped on ``@tool``-decorated functions.

    ``kind`` is ``"tool"`` so the loader can dispatch to the tool path
    without isinstance checks. ``examples`` is a tuple to keep the
    dataclass hashable and frozen-friendly.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    classification: ToolClassification
    capability_tags: tuple[str, ...] = ()
    when_to_use: str = ""
    requires_skill: str | None = None
    version: str = "1.0.0"
    examples: tuple[str, ...] = ()
    model_hint: str | None = None
    kind: Literal["tool"] = "tool"


@dataclass(frozen=True)
class HookMetadata:
    """Stamped on ``@hook``-decorated bus subscribers.

    ``priority`` orders subscribers within a single event (lower runs
    first). ``tryfirst`` / ``trylast`` are pluggy-style overrides that
    set priority to 90 / 110 respectively. The two are mutually
    exclusive — the decorator raises at definition time if both are
    set.
    """

    name: str
    event: str
    priority: int = 100
    tryfirst: bool = False
    trylast: bool = False
    kind: Literal["hook"] = "hook"


@dataclass(frozen=True)
class BackgroundTaskMetadata:
    """Stamped on ``@background_task``-decorated async functions.

    ``interval`` is seconds between iterations. The loader spawns the
    task with ``asyncio.create_task`` and re-schedules it on each
    reload via drain-then-replace (R-062).
    """

    name: str
    interval: float
    kind: Literal["background_task"] = "background_task"


@dataclass(frozen=True)
class CapabilityClassMetadata:
    """Stamped on ``@capability``-decorated classes.

    A capability class bundles a setup/teardown lifecycle with optional
    ``@tool``-decorated methods. ``depends_on`` lets the loader
    topologically order setup (R-061).
    """

    name: str
    depends_on: tuple[str, ...] = ()
    kind: Literal["capability"] = "capability"


CapabilityMetadata = ToolMetadata | HookMetadata | BackgroundTaskMetadata | CapabilityClassMetadata


def tool(
    *,
    name: str | None = None,
    description: str = "",
    classification: ToolClassification = "state_modifying",
    capability_tags: Iterable[str] | None = None,
    when_to_use: str = "",
    requires_skill: str | None = None,
    version: str = "1.0.0",
    examples: Iterable[str] | None = None,
    model_hint: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Stamp tool metadata on an async function.

    ``classification`` defaults to ``state_modifying`` (fail-closed) so
    a tool added without explicit classification never races in a
    parallel batch. Override with ``"read_only"`` for safe tools.

    The new SPEC-021 fields are all optional with safe defaults so
    existing call sites keep working unchanged:

      * ``when_to_use``    — short hint for prompt manifest (R-020)
      * ``requires_skill`` — name of skill that teaches this tool (R-014)
      * ``version``        — semver string for diff/replace tracking
      * ``examples``       — sample invocation strings
      * ``model_hint``     — preferred model size if any

    Usage::

        @tool(description="read a file", classification="read_only",
              capability_tags=["file_read"], when_to_use="when reading source")
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
            capability_tags=tuple(capability_tags or ()),
            when_to_use=when_to_use,
            requires_skill=requires_skill,
            version=version,
            examples=tuple(examples or ()),
            model_hint=model_hint,
        )
        # Stash on the original function so call semantics are
        # unchanged. The opaque underscore-prefixed name avoids
        # collision with user-authored attributes; ``_arc_capability_``
        # prefix is reserved for the loader.
        fn._arc_capability_meta = meta  # type: ignore[attr-defined]
        return fn

    return decorator


# Pluggy-style ordering overrides. ``tryfirst`` / ``trylast`` translate
# to fixed priority offsets so the registry can sort by a single int.
_TRYFIRST_PRIORITY = 90
_TRYLAST_PRIORITY = 110


def hook(
    *,
    event: str,
    name: str | None = None,
    priority: int = 100,
    tryfirst: bool = False,
    trylast: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Stamp hook metadata on an async bus subscriber.

    The loader picks up ``_arc_capability_meta`` and registers the
    function as a subscriber for ``event`` on the module bus.

    ``tryfirst=True`` sets priority to 90 (runs before defaults).
    ``trylast=True`` sets priority to 110 (runs after defaults). Both
    cannot be set simultaneously.
    """
    if tryfirst and trylast:
        raise ValueError("@hook: tryfirst and trylast are mutually exclusive")
    if tryfirst:
        priority = _TRYFIRST_PRIORITY
    elif trylast:
        priority = _TRYLAST_PRIORITY

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        meta = HookMetadata(
            name=name or fn.__name__,
            event=event,
            priority=priority,
            tryfirst=tryfirst,
            trylast=trylast,
        )
        fn._arc_capability_meta = meta  # type: ignore[attr-defined]
        return fn

    return decorator


def background_task(
    *,
    interval: float,
    name: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Stamp background-task metadata on an async function.

    ``interval`` must be > 0; the loader rejects 0 or negative values
    at decoration time so a misconfigured task never reaches runtime.
    """
    if interval <= 0:
        raise ValueError(f"@background_task: interval must be > 0, got {interval!r}")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        meta = BackgroundTaskMetadata(
            name=name or fn.__name__,
            interval=float(interval),
        )
        fn._arc_capability_meta = meta  # type: ignore[attr-defined]
        return fn

    return decorator


def capability(
    *,
    name: str | None = None,
    depends_on: Iterable[str] | None = None,
) -> Callable[[type], type]:
    """Stamp capability-class metadata on a class.

    The class is expected to expose ``async setup(ctx)`` and
    ``async teardown()`` methods (lifecycle resource management). It
    may also contain ``@tool``-decorated methods — those keep their own
    ``_arc_capability_meta`` stamp and the loader binds them to a
    class instance at registration time.
    """

    def decorator(cls: type) -> type:
        meta = CapabilityClassMetadata(
            name=name or cls.__name__,
            depends_on=tuple(depends_on or ()),
        )
        cls._arc_capability_meta = meta  # type: ignore[attr-defined]
        return cls

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


__all__ = [
    "BackgroundTaskMetadata",
    "CapabilityClassMetadata",
    "CapabilityMetadata",
    "HookMetadata",
    "ToolClassification",
    "ToolMetadata",
    "background_task",
    "capability",
    "hook",
    "tool",
]
