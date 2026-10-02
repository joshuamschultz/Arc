"""Item 71 — a test run's stub is held to the node's output_schema.

The echo used to bypass the schema gate, so a draft could pass in test mode and
fail live. The stub is now generated from the schema and goes through the same
completion gate a real output does; a schema the generator cannot satisfy fails
the test run and says where.
"""

from __future__ import annotations

from typing import Any

import pytest

from .test_workflow_test_mode import _claimed, _Registry, _run_tool, state  # noqa: F401

_STUB_SCHEMA = {
    "type": "object",
    "required": ["id", "status", "tags"],
    "properties": {
        "id": {"type": "integer", "minimum": 5},
        "status": {"enum": ["ok", "bad"]},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "additionalProperties": False,
}


async def test_test_mode_stub_output_validated_against_schema(state: Any) -> None:  # noqa: F811
    import jsonschema

    state.tool_registry = _Registry("state_modifying")

    row = await _run_tool(state, await _claimed(state, output_schema=_STUB_SCHEMA))

    assert row.status == "done"
    assert row.output is not None
    jsonschema.validate(row.output, _STUB_SCHEMA)
    assert row.output["status"] == "ok", "an enum stubs to its first value"
    assert "stubbed" not in row.output, "the stub is shaped by the schema, not the echo"


async def test_test_mode_stub_goes_through_the_normal_completion_gate(
    state: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcagent.modules.tasks import capabilities

    state.tool_registry = _Registry("state_modifying")
    monkeypatch.setattr(
        capabilities, "_node_completion_refusal", lambda *a, **k: "gate said no at 'id'"
    )

    row = await _run_tool(state, await _claimed(state, output_schema=_STUB_SCHEMA))

    assert row.status == "failed"
    assert "gate said no" in (row.last_error or "")


async def test_unsatisfiable_schema_fails_test_run_with_path(state: Any) -> None:  # noqa: F811
    schema = {
        "type": "object",
        "required": ["code"],
        "properties": {"code": {"type": "string", "pattern": "^[a-z]{3}-[0-9]{4}$"}},
    }
    state.tool_registry = _Registry("state_modifying")

    row = await _run_tool(state, await _claimed(state, output_schema=schema))

    assert row.status == "failed"
    assert (row.last_error or "").startswith("test stub cannot satisfy output_schema: ")
    assert "code" in (row.last_error or ""), "the error names where the schema defeats the stub"
