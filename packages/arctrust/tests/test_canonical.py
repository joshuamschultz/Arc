"""Byte contract for :func:`arctrust.canonical_json` — the one serializer a
signature binds. Every signing package reuses this primitive; the cross-package
byte-identity test lives in arcagent (which depends on arcllm/arcrun/arctrust).
"""

from __future__ import annotations

from arctrust import canonical_json


def test_canonical_json_is_sorted_compact_ascii() -> None:
    obj = {"b": 1, "a": {"z": True, "y": [3, 2]}}
    assert canonical_json(obj) == b'{"a":{"y":[3,2],"z":true},"b":1}'


def test_canonical_json_key_order_is_deterministic() -> None:
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_json_escapes_non_ascii() -> None:
    # ensure_ascii=True → pure-ASCII bytes, stable regardless of platform locale.
    out = canonical_json({"k": "é"})
    assert out == b'{"k":"\\u00e9"}'
    out.decode("ascii")  # must not raise
