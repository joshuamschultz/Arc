"""Where one egress call goes — the destination class an "Always allow" is scoped to.

An operator's standing grant (SPEC-035 OQ-3, ruled 2026-10-03) covers egress
only to the destination it was granted for; a new destination prompts again. The
destination of a call is resolved here, in one place:

- a connector tool knows its own connection (the connection id is set by code
  when the connection was attached, never read from the model's arguments) and
  passes it as the resolver;
- any other egress tool is judged by EVERY destination its arguments name —
  mail addresses and URLs (reduced to scheme://host) — sorted and joined, so a
  call that adds a second recipient is a different destination;
- an egress call that names nothing recognisable is ``tool:<name>``.

Shape, not argument names: a tool picks its own argument names, so a list of
blessed keys would be a hole the first time a tool called its field ``recipient``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

#: What an argument value has to look like to be an outbound destination.
_TARGET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[^\s@,;<>\"']+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"',;]+"),
)

DestinationResolver = Callable[[Mapping[str, Any]], str]


def _strings(value: object) -> Iterator[str]:
    """Every string inside an argument value, nested containers included."""
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def outbound_target(arguments: Mapping[str, Any]) -> str:
    """The first destination this call names, or ``""`` — for display only."""
    for text in _strings(arguments):
        for pattern in _TARGET_PATTERNS:
            found = pattern.search(text)
            if found is not None:
                return found.group(0)
    return ""


def _destination_class(target: str) -> str:
    """A URL reduces to its origin; an address stays whole. Case-folded."""
    if "://" not in target:
        return target.casefold()
    parts = urlsplit(target)
    return f"{parts.scheme}://{parts.netloc}".casefold()


def egress_destination(
    tool_name: str,
    arguments: Mapping[str, Any],
    resolver: DestinationResolver | None = None,
) -> str:
    """The destination class of one egress call (never empty for a fallback).

    A resolver's answer is final, even when empty: an empty destination is
    one no standing grant covers, so an unresolvable connection prompts.
    """
    if resolver is not None:
        return resolver(arguments)
    found = {
        _destination_class(match.group(0))
        for text in _strings(arguments)
        for pattern in _TARGET_PATTERNS
        for match in pattern.finditer(text)
    }
    return ",".join(sorted(found)) if found else f"tool:{tool_name}"


__all__ = ["DestinationResolver", "egress_destination", "outbound_target"]
