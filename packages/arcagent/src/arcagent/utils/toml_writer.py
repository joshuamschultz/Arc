"""Minimal TOML emitter for the nested config dicts Arc writes back to disk.

There is no ``tomli_w`` dependency in-tree, and several surfaces must render a
config dict as TOML that ``tomllib`` round-trips to the same dict: materialized
``arcagent.toml`` files (``arccli.blueprints``), ``gateway.toml`` platform blocks
(``arcgateway.connect``), and connector extension config.

Shape: scalars before sub-tables, ``[a.b]`` headers for nesting, ``[[a.b]]``
headers for a list of tables (the shape ``arc trust approve`` persists under
``[[security.validators.approved]]``). Output is deterministic — key order follows
the dict's insertion order, so the same input always produces the same bytes.

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

    Supports ``bool``, ``int``, ``float``, ``str``, ``list``, and nested ``dict``
    in any combination. Raises ``ValueError`` for any other value type rather than
    emitting TOML that would not parse back.
    """
    lines: list[str] = []
    _emit_table(data, [], lines)
    return "\n".join(lines).rstrip("\n") + "\n"


def _is_table_array(val: Any) -> bool:
    """True for a non-empty list of dicts — the only list TOML writes as ``[[a.b]]``.

    An empty list stays an empty inline array: ``approved = []`` and a zero-element
    array of tables are the same parsed value, and the inline form is the one that
    round-trips.
    """
    return isinstance(val, list) and bool(val) and all(isinstance(item, dict) for item in val)


def _emit_table(table: dict[str, Any], path: list[str], lines: list[str]) -> None:
    if path:
        lines.append(f"[{_header(path)}]")
    _emit_body(table, path, lines)


def _emit_body(table: dict[str, Any], path: list[str], lines: list[str]) -> None:
    """Scalars first, then every child that owns its own header.

    Order is load-bearing: a bare key written after a ``[a.b]`` header would be
    parsed into that sub-table instead of this one.
    """
    nested: list[tuple[str, Any]] = []
    for key, val in table.items():
        if isinstance(val, dict) or _is_table_array(val):
            nested.append((key, val))
        else:
            lines.append(f"{_key(key)} = {_scalar(val)}")
    if path:
        lines.append("")
    for key, val in nested:
        if isinstance(val, dict):
            _emit_table(val, [*path, key], lines)
        else:
            _emit_table_array(val, [*path, key], lines)


def _emit_table_array(rows: list[dict[str, Any]], path: list[str], lines: list[str]) -> None:
    """One ``[[a.b]]`` header per row; a row's own sub-tables bind to that row."""
    for row in rows:
        lines.append(f"[[{_header(path)}]]")
        _emit_body(row, path, lines)


def _header(path: list[str]) -> str:
    """A dotted table path, each part quoted when TOML requires it."""
    return ".".join(_key(part) for part in path)


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
    if isinstance(val, dict):
        return "{" + ", ".join(f"{_key(k)} = {_scalar(v)}" for k, v in val.items()) + "}"
    raise ValueError(f"unsupported TOML value type: {type(val).__name__}")


__all__ = ["dumps_toml"]
