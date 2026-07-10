"""SPEC-054 COMP-007 — config-gated arg capture at the observe() boundary (REQ-117).

Pins the capture API: ``TraceStore(workspace, *, session_id="", capture_args=False,
tier="personal")`` and ``observe(..., args: dict | None = None)``. Default OFF records
``args_hash`` = sha256 of the scrubbed, canonically serialized args
(``json.dumps(scrub_args(args), sort_keys=True, default=str)``) and never persists raw
args. Capture ON persists ``ToolCallRecord.args`` — scrubbed BEFORE persistence via
``arcskill.improver._util.scrub_args`` (secret-looking tokens replaced with
``[REDACTED]`` token-level and recursively; invisible/zero-width chars stripped via the
sanitize path, ASI-06) — so secret material never reaches the JSONL bytes on disk.
Federal tier stays hash-only regardless of the knob (SI-12(2), non-overridable).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from arcskill.improver._util import scrub_args
from arcskill.improver.models import ToolCallRecord
from arcskill.improver.trace_store import TraceStore

_SK_SAMPLE = "sk-abc123def456ghi789jkl012"
_JWT_SAMPLE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqIn0.dGVzdHNpZw"
_BEARER_SAMPLE = f"Bearer {_JWT_SAMPLE}"


def _store(workspace: Path, **kwargs: Any) -> TraceStore:
    return TraceStore(workspace, session_id="sess-1", **kwargs)


def _observe(store: TraceStore, args: dict[str, Any]) -> None:
    store.observe(
        skill_name="calc", tool_name="fetch_data", status="ok", error_type=None, args=args
    )


def _raw_jsonl(workspace: Path) -> bytes:
    files = list((workspace / "skill_traces" / "calc").glob("traces-*.jsonl"))
    assert len(files) == 1
    return files[0].read_bytes()


def _persisted_call(workspace: Path) -> dict[str, Any]:
    line = _raw_jsonl(workspace).decode("utf-8").strip()
    record: dict[str, Any] = json.loads(line)
    calls: list[dict[str, Any]] = record["tool_calls"]
    assert len(calls) == 1
    return calls[0]


def _expected_hash(args: dict[str, Any]) -> str:
    """The pinned hash formula: sha256 over the scrubbed canonical serialization."""
    return hashlib.sha256(
        json.dumps(scrub_args(args), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# -- default OFF: hash-only, args never persisted ---------------------------------


def test_default_off_records_scrubbed_hash_and_never_persists_args(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _observe(store, {"query": "weather in Tokyo", "api_key": _SK_SAMPLE})
    store.close_turn()

    raw = _raw_jsonl(tmp_path)
    assert b"weather in Tokyo" not in raw
    assert _SK_SAMPLE.encode("utf-8") not in raw

    call = _persisted_call(tmp_path)
    assert call.get("args") is None
    assert call["args_hash"] == _expected_hash(
        {"query": "weather in Tokyo", "api_key": _SK_SAMPLE}
    )


def test_observe_without_args_keeps_hash_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.observe(skill_name="calc", tool_name="fetch_data", status="ok", error_type=None)
    store.close_turn()

    call = _persisted_call(tmp_path)
    assert call["args_hash"] == ""
    assert call.get("args") is None


# -- capture ON: scrub happens BEFORE persistence (REQ-117) ------------------------


def test_capture_on_persists_scrubbed_args_secrets_never_reach_disk(tmp_path: Path) -> None:
    store = _store(tmp_path, capture_args=True)
    args = {
        "query": "weather in Tokyo",
        "api_key": _SK_SAMPLE,
        "headers": {"authorization": _BEARER_SAMPLE},
    }
    _observe(store, args)
    store.close_turn()

    raw = _raw_jsonl(tmp_path)
    assert b"weather in Tokyo" in raw  # capture actually captured
    assert _SK_SAMPLE.encode("utf-8") not in raw
    assert _JWT_SAMPLE.encode("utf-8") not in raw

    call = _persisted_call(tmp_path)
    assert call["args"]["query"] == "weather in Tokyo"
    assert call["args"]["api_key"] == "[REDACTED]"
    # scrub is recursive and token-level: the nested bearer token is gone
    assert _JWT_SAMPLE not in call["args"]["headers"]["authorization"]
    assert "[REDACTED]" in call["args"]["headers"]["authorization"]
    assert call["args_hash"] == _expected_hash(args)


def test_capture_on_strips_invisible_chars_before_persistence(tmp_path: Path) -> None:
    """ASI-06: zero-width instruction-smuggling chars never reach disk (sanitize path)."""
    store = _store(tmp_path, capture_args=True)
    _observe(store, {"note": "plan\u200bnine"})
    store.close_turn()

    raw = _raw_jsonl(tmp_path)
    assert "\u200b".encode() not in raw
    assert b"\\u200b" not in raw  # json may escape non-ascii; neither form survives
    assert _persisted_call(tmp_path)["args"]["note"] == "plannine"


def test_capture_on_roundtrips_args_through_load_traces(tmp_path: Path) -> None:
    store = _store(tmp_path, capture_args=True)
    _observe(store, {"city": "tokyo"})
    store.close_turn()

    traces = store.load_traces("calc")
    assert len(traces) == 1
    assert traces[0].tool_calls[0].args == {"city": "tokyo"}


def test_toolcallrecord_args_field_defaults_none() -> None:
    record = ToolCallRecord(
        tool_name="fetch_data", args_hash="", result_status="ok", duration_ms=0.0
    )
    assert record.args is None
    assert record.to_dict()["args"] is None


# -- federal override: hash-only regardless of the knob (REQ-117) ------------------


def test_federal_tier_is_hash_only_regardless_of_capture_knob(tmp_path: Path) -> None:
    store = TraceStore(tmp_path, session_id="sess-1", capture_args=True, tier="federal")
    args = {"query": "weather in Tokyo", "api_key": _SK_SAMPLE}
    _observe(store, args)
    store.close_turn()

    raw = _raw_jsonl(tmp_path)
    assert b"weather in Tokyo" not in raw  # not just secrets — nothing is captured
    assert _SK_SAMPLE.encode("utf-8") not in raw

    call = _persisted_call(tmp_path)
    assert call.get("args") is None
    assert call["args_hash"] == _expected_hash(args)


# -- the scrub hook itself ----------------------------------------------------------


def test_scrub_args_redacts_tokens_preserves_prose_and_does_not_mutate() -> None:
    original = {
        "note": f"call with {_SK_SAMPLE} today",
        "auth": _BEARER_SAMPLE,
        "city": "tokyo",
    }
    snapshot = json.dumps(original, sort_keys=True)

    scrubbed = scrub_args(original)

    assert json.dumps(original, sort_keys=True) == snapshot  # input untouched
    assert scrubbed["city"] == "tokyo"
    # token-level replacement: surrounding prose survives, the token does not
    assert _SK_SAMPLE not in scrubbed["note"]
    assert "[REDACTED]" in scrubbed["note"]
    assert scrubbed["note"].startswith("call with")
    assert scrubbed["note"].endswith("today")
    assert _JWT_SAMPLE not in scrubbed["auth"]
    assert "[REDACTED]" in scrubbed["auth"]
