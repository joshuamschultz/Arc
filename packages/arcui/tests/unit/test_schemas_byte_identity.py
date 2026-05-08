"""Phase 6 §9.1 — byte-identity contract tests for arcui response models.

For each typed response model in ``arcui.schemas``, this test asserts
that ``Model(**dict_payload).model_dump(mode="json")`` produces a
dict whose JSON-serialised form is identical to ``json.dumps(dict_payload)``.

If a model definition drifts (added field, reordered fields, type
coercion) the JSON bytes will differ and the test fails — catching
the violation of the wire-identity contract before it reaches a route.
"""

from __future__ import annotations

import json

import pytest
from starlette.responses import JSONResponse

from arcui import schemas


def _roundtrip_bytes(model_cls: type[schemas.BaseModel], payload: dict) -> bytes:
    """Wrap *payload* through Model and JSONResponse; return body bytes."""
    return JSONResponse(model_cls(**payload).model_dump(mode="json")).body


def _direct_bytes(payload: dict) -> bytes:
    """Body bytes of ``JSONResponse(payload)`` — the pre-refactor path."""
    return JSONResponse(payload).body


# Each entry: (model_class, fixed_payload). The payload mirrors what the
# corresponding route handler builds for a representative happy-path call.
_CASES: list[tuple[type[schemas.BaseModel], dict]] = [
    (schemas.ErrorResponse, {"error": "Agent not found"}),
    (schemas.ErrorResponse, {"error": "Invalid window"}),
    (
        schemas.FilesTreeEntry,
        {"path": "skills/foo.md", "type": "file", "size": 123, "mtime": 1.5},
    ),
    (
        schemas.FilesTreeResponse,
        {
            "root": "workspace",
            "entries": [
                {"path": "a", "type": "file", "size": 1, "mtime": 0.0},
            ],
        },
    ),
    (
        schemas.FileReadResponse,
        {
            "path": "policy.md",
            "size": 42,
            "mtime": 100.0,
            "content": "hello",
            "content_type": "text/markdown",
        },
    ),
    (
        schemas.SessionEntry,
        {"sid": "abc", "path": "sessions/abc.jsonl", "size": 200, "mtime": 1.0},
    ),
    (
        schemas.SessionsListResponse,
        {
            "sessions": [
                {"sid": "x", "path": "sessions/x.jsonl", "size": 1, "mtime": 0.0},
            ],
        },
    ),
    (
        schemas.SessionReplayResponse,
        {
            "sid": "abc",
            "page": 1,
            "page_size": 50,
            "total": 3,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ),
    (schemas.TasksResponse, {"tasks": []}),
    (schemas.TasksResponse, {"tasks": [{"id": "t1", "subject": "x"}]}),
    (schemas.SchedulesResponse, {"schedules": []}),
    (
        schemas.TracesResponse,
        {"traces": [], "cursor": None},
    ),
    (
        schemas.TracesResponse,
        {"traces": [{"trace_id": "t1"}], "cursor": "2026-05-08:42"},
    ),
    (
        schemas.StatsResponse,
        {"stats": {"request_count": 5}, "window": "24h"},
    ),
    (schemas.StatsResponse, {"stats": {}, "window": "24h"}),
    (schemas.AuditEventsResponse, {"events": []}),
    (
        schemas.AuditEventsResponse,
        {"events": [{"agent_id": "a", "event_type": "tool.start"}]},
    ),
    (schemas.SkillsResponse, {"skills": []}),
    (
        schemas.SkillsResponse,
        {"skills": [{"name": "s1", "description": "d", "version": "", "path": "p"}]},
    ),
    (
        schemas.ToolsResponse,
        {"tools": [], "allowlist": [], "denylist": []},
    ),
    (
        schemas.ToolsResponse,
        {
            "tools": [{"name": "read", "transport": "builtin"}],
            "allowlist": ["read"],
            "denylist": ["bash"],
        },
    ),
    (
        schemas.PolicyResponse,
        {"raw": "# policy", "bullets": []},
    ),
    (
        schemas.PolicyBulletsResponse,
        {"bullets": [{"id": "b1", "text": "be helpful", "score": 5}]},
    ),
    (
        schemas.PolicyStatsResponse,
        {"total": 10, "active": 8, "retired": 2, "avg_score": 6.5},
    ),
    (
        schemas.ConfigResponse,
        {"config": {"agent": {"name": "demo"}}, "raw": "[agent]\nname='demo'", "mtime": 1.0},
    ),
    (
        schemas.TeamPolicyStatsResponse,
        {
            "total": 5,
            "active": 4,
            "retired": 1,
            "avg_score": 6.0,
            "per_agent": [
                {"agent_id": "a1", "total": 3, "active": 3, "retired": 0, "avg_score": 7.0},
            ],
        },
    ),
    (
        schemas.TeamToolsSkillsResponse,
        {
            "skills": [{"name": "summarise", "agent_id": "a1"}],
            "tools": [{"name": "read", "agents": ["a1"]}],
        },
    ),
    (schemas.AgentsListResponse, {"agents": []}),
    (
        schemas.AgentsListResponse,
        {"agents": [{"agent_id": "a1", "name": "demo"}]},
    ),
    (
        schemas.ControlResponseEnvelope,
        {"response": {"status": "ok", "request_id": "r1"}},
    ),
    (
        schemas.ExportTracesResponse,
        {"traces": [], "count": 0},
    ),
    (
        schemas.ExportTracesResponse,
        {"traces": [{"trace_id": "t1"}], "count": 1},
    ),
]


@pytest.mark.parametrize(("model_cls", "payload"), _CASES)
def test_roundtrip_bytes_equal_direct(model_cls: type[schemas.BaseModel], payload: dict) -> None:
    """``JSONResponse(Model(**d).model_dump(mode="json"))`` must produce
    body bytes identical to ``JSONResponse(d)`` for the same *d*.
    """
    direct = _direct_bytes(payload)
    via_model = _roundtrip_bytes(model_cls, payload)
    assert direct == via_model, (
        f"Byte mismatch for {model_cls.__name__}:\n"
        f"  direct:    {direct!r}\n"
        f"  via_model: {via_model!r}"
    )


def test_models_preserve_json_decode_roundtrip() -> None:
    """Sanity: every body decodes back to the same Python structure."""
    for model_cls, payload in _CASES:
        direct = json.loads(_direct_bytes(payload))
        via_model = json.loads(_roundtrip_bytes(model_cls, payload))
        assert direct == via_model
