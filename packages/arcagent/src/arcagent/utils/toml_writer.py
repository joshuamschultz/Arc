"""Minimal TOML emitter for the nested config dicts Arc writes back to disk.

There is no ``tomli_w`` dependency in-tree, and several surfaces must render a
config dict as TOML that ``tomllib`` round-trips to the same dict: materialized
``arcagent.toml`` files (``arccli.blueprints``), ``gateway.toml`` platform blocks
(``arcgateway.connect``), and connector extension config.

Shape: scalars before sub-tables, ``[a.b]`` headers for nesting. Output is
deterministic — key order follows the dict's insertion order, so the same input
always produces the same bytes.

Lives in ``arcagent.utils`` because both ``arcgateway`` and ``arccli`` already
depend on ``arc-agent``; the dependency arrow keeps pointing down.
"""

from __future__ import annotations

import re
from typing import Any

#: A key TOML accepts unquoted. Anything else is written as a quoted key, because
#: an emitter able to produce a file that will not re-parse is a landmine
#: regardless of who validates upstream: a config the agent cannot read is an
#: agent that does not start.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize a nested config dict to TOML that ``tomllib`` round-trips.

    Supports ``bool``, ``int``, ``float``, ``str``, ``list``, and nested ``dict``.
    Raises ``ValueError`` for any other value type rather than emitting TOML that
    would not parse back.
    """
    lines: list[str] = []
    _emit_table(data, [], lines)
    return "\n".join(lines).rstrip("\n") + "\n"


def _emit_table(table: dict[str, Any], path: list[str], lines: list[str]) -> None:
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    subtables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if path:
        lines.append(f"[{'.'.join(_key(part) for part in path)}]")
    for key, val in scalars:
        lines.append(f"{_key(key)} = {_scalar(val)}")
    if path:
        lines.append("")
    for key, val in subtables:
        _emit_table(val, [*path, key], lines)


def _key(key: str) -> str:
    """One key, bare when TOML allows it and quoted when it does not."""
    return key if _BARE_KEY.fullmatch(key) else _quote(key)


def _quote(text: str) -> str:
    """A TOML basic string: escapes and control characters both handled."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    body = "".join(c if c >= " " and c != "\x7f" else f"\\u{ord(c):04X}" for c in escaped)
    return f'"{body}"'


def _scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return _quote(val)
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        return "[" + ", ".join(_scalar(v) for v in val) + "]"
    raise ValueError(f"unsupported TOML value type: {type(val).__name__}")


__all__ = ["dumps_toml"]
