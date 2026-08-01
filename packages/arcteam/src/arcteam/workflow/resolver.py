"""COMP-004 — bind ``$nodes``/``$input`` references to typed values (REQ-239).

Wiring is where an upstream node's output crosses into a downstream tool call
or prompt section, which makes it the single highest-value injection surface in
the whole feature. Every expression-injection class in comparable systems comes
from splicing an expression's value into a command or template string, so this
module removes the possibility rather than warning about it:

* a reference resolves to the **typed value** it names — an int stays an int, a
  mapping stays a mapping;
* a string that *contains* a reference without *being* one is
  :class:`~arcteam.workflow.errors.TextualInterpolationError`, refused rather
  than substituted;
* there is no interpolation, formatting, or rendering helper here at all, so
  there is nothing to reach for when the refusal is inconvenient.

Downstream instructions reach a node through the prompt-assembly sections seam
with the value attached as structured data — never concatenated into the
instruction text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from arcteam.workflow.errors import TextualInterpolationError, UnresolvableReferenceError

_REFERENCE = re.compile(
    r"^\$(?P<root>nodes|input)((?:\.[A-Za-z][A-Za-z0-9_-]*)+)$",
)
_EMBEDDED = re.compile(r"\$(?:nodes|input)\.[A-Za-z]")


@dataclass(frozen=True, slots=True)
class Reference:
    """A parsed ``$nodes.<id>.output.<field>...`` or ``$input.<field>...``."""

    root: Literal["nodes", "input"]
    node_id: str | None
    segments: tuple[str, ...]
    """Field path *below* the node output, or below the run input."""

    def __str__(self) -> str:
        if self.root == "nodes":
            return "$nodes." + ".".join((str(self.node_id), "output", *self.segments))
        return "$input." + ".".join(self.segments)


def is_reference(value: Any) -> bool:
    """True when ``value`` is a string that is *entirely* one reference."""
    return isinstance(value, str) and _REFERENCE.match(value) is not None


def parse_reference(expression: str) -> Reference:
    """Parse a whole-string reference.

    Raises:
        UnresolvableReferenceError: the string is not a well-formed reference.
    """
    match = _REFERENCE.match(expression)
    if match is None:
        raise UnresolvableReferenceError(
            f"{expression!r} is not a reference; expected "
            f"$nodes.<node_id>.output.<field> or $input.<field>"
        )
    parts = tuple(match.group(2).lstrip(".").split("."))
    if match.group("root") == "input":
        return Reference("input", None, parts)
    if len(parts) < 3 or parts[1] != "output":
        raise UnresolvableReferenceError(
            f"{expression!r} must read a node output: $nodes.<node_id>.output.<field>"
        )
    return Reference("nodes", parts[0], parts[2:])


def resolve_value(expression: str, scope: Mapping[str, Any]) -> Any:
    """Resolve one whole-string reference to its typed value.

    Args:
        expression: A reference such as ``$nodes.collect.output.company_domain``.
        scope: ``{"nodes": {node_id: {"output": {...}}}, "input": {...}}``.

    Raises:
        TextualInterpolationError: ``expression`` embeds a reference in text.
        UnresolvableReferenceError: the reference names something absent.
    """
    if not is_reference(expression):
        _refuse_if_embedded(expression)
        raise UnresolvableReferenceError(
            f"{expression!r} is not a reference; expected "
            f"$nodes.<node_id>.output.<field> or $input.<field>"
        )
    return _lookup(parse_reference(expression), scope)


def resolve_args(args: Mapping[str, Any], scope: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve every reference in a tool-argument mapping, recursively.

    Non-reference values pass through untouched. A string embedding a
    reference is refused — see the module docstring for why that refusal is
    the point of this component rather than an inconvenience in it.
    """
    return {key: _resolve_any(value, scope) for key, value in args.items()}


def references_in(value: Any) -> tuple[Reference, ...]:
    """Every reference in an argument tree, for static analysis before a run."""
    if is_reference(value):
        return (parse_reference(value),)
    if isinstance(value, Mapping):
        return tuple(r for item in value.values() for r in references_in(item))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(r for item in value for r in references_in(item))
    return ()


def embedded_reference_strings(value: Any) -> tuple[str, ...]:
    """Every string in a value tree that embeds a reference instead of being one.

    The validator uses this to refuse an interpolating argument at authoring
    time, so the refusal lands where a model can repair it rather than mid-run.
    """
    if isinstance(value, str):
        return () if is_reference(value) or not _EMBEDDED.search(value) else (value,)
    if isinstance(value, Mapping):
        values = value.values()
        return tuple(f for item in values for f in embedded_reference_strings(item))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(f for item in value for f in embedded_reference_strings(item))
    return ()


def _resolve_any(value: Any, scope: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        if is_reference(value):
            return _lookup(parse_reference(value), scope)
        _refuse_if_embedded(value)
        return value
    if isinstance(value, Mapping):
        return {key: _resolve_any(item, scope) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_resolve_any(item, scope) for item in value]
    return value


def _refuse_if_embedded(value: str) -> None:
    """Refuse a string that embeds a reference instead of being one (LLM01)."""
    if _EMBEDDED.search(value):
        raise TextualInterpolationError(
            f"{value!r} embeds a reference in text. Upstream output binds as a typed value, "
            f"never as substituted text — pass the reference as the whole argument value, or "
            f"hand the value to the node as a prompt section rather than splicing it into a "
            f"string."
        )


def _lookup(reference: Reference, scope: Mapping[str, Any]) -> Any:
    """Walk a reference through ``scope`` by mapping lookup only."""
    if reference.root == "input":
        return _walk(scope.get("input"), reference.segments, str(reference), "$input")
    nodes = scope.get("nodes")
    if not isinstance(nodes, Mapping) or reference.node_id not in nodes:
        raise UnresolvableReferenceError(
            f"{reference} is unresolvable: no output recorded for node {reference.node_id!r}"
        )
    node = nodes[reference.node_id]
    output = node.get("output") if isinstance(node, Mapping) else None
    return _walk(output, reference.segments, str(reference), f"$nodes.{reference.node_id}.output")


def _walk(current: Any, segments: tuple[str, ...], label: str, prefix: str) -> Any:
    walked = prefix
    for segment in segments:
        if not isinstance(current, Mapping) or segment not in current:
            raise UnresolvableReferenceError(
                f"{label} is unresolvable: no {segment!r} under {walked}"
            )
        current = current[segment]
        walked = f"{walked}.{segment}"
    return current


__all__ = [
    "Reference",
    "embedded_reference_strings",
    "is_reference",
    "parse_reference",
    "references_in",
    "resolve_args",
    "resolve_value",
]
