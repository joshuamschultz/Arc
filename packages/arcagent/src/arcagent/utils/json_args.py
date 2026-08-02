"""Coerce a structured tool argument out of the shape a model actually sent.

A tool declares ``output: dict``, ``nodes: list[dict]``, ``args: dict`` — and a
model routinely hands the JSON *text* of that value instead. Every layer that
treated the declared type as a guarantee has produced the same failure twice in
this codebase: the value is refused or crashes, the model is given nothing it
can repair, and the run burns its turn budget reissuing an identical call.

Model output is untrusted input to a tool (LLM05). Parsing what is parseable is
not laxity — the alternative is not stricter, it is merely unusable, because the
refusal names no repair. What stays strict is everything downstream: a coerced
value is validated exactly as a natively-typed one is.
"""

from __future__ import annotations

import json
from typing import Any


def as_object(value: Any, field: str) -> dict[str, Any]:
    """One structured argument as a dict, or a ``ValueError`` naming the shape."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be an object; this is not JSON ({exc.msg})") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"{field} must be an object, not {type(parsed).__name__}")
    raise ValueError(f"{field} must be an object, not {type(value).__name__}")


def as_optional_object(value: Any, field: str) -> dict[str, Any] | None:
    """Same, but ``None``/absent stays absent — an omitted argument is not an error."""
    if value is None:
        return None
    return as_object(value, field)


__all__ = ["as_object", "as_optional_object"]
