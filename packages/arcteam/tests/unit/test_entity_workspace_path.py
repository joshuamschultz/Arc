"""Tests for Entity.workspace_path field (SPEC-019 FR-2).

Backwards compat is the load-bearing requirement: existing JSON records
without `workspace_path` MUST deserialize cleanly with the field set to None.
"""

from __future__ import annotations

from arcteam.types import Entity, EntityType


class TestEntityWorkspacePath:
    """workspace_path: str | None — added in SPEC-019."""

    def test_default_is_none(self) -> None:
        e = Entity(id="agent://a1", name="A1", type=EntityType.AGENT)
        assert e.workspace_path is None

    def test_explicit_value_persisted(self) -> None:
        e = Entity(
            id="agent://a1",
            name="A1",
            type=EntityType.AGENT,
            workspace_path="/abs/path/to/workspace",
        )
        assert e.workspace_path == "/abs/path/to/workspace"

    def test_legacy_record_without_field_loads(self) -> None:
        """Existing JSON records (pre-SPEC-019) lack the field entirely.

        Pydantic's optional-with-default behavior MUST round-trip them
        without raising — this is the backwards-compat guarantee.
        """
        legacy_record = {
            "id": "agent://legacy",
            "name": "Legacy",
            "type": "agent",
            "roles": ["ops"],
            "capabilities": [],
            "created": "2025-01-01T00:00:00Z",
            "status": "active",
            # Note: no workspace_path key
        }
        e = Entity.model_validate(legacy_record)
        assert e.workspace_path is None
        assert e.id == "agent://legacy"

    def test_serialization_includes_field(self) -> None:
        e = Entity(
            id="agent://a1",
            name="A1",
            type=EntityType.AGENT,
            workspace_path="/abs",
        )
        data = e.model_dump()
        assert "workspace_path" in data
        assert data["workspace_path"] == "/abs"

    def test_serialization_when_none(self) -> None:
        e = Entity(id="agent://a1", name="A1", type=EntityType.AGENT)
        data = e.model_dump()
        # Field present but value None — JSON serializers can omit on None if needed
        assert data.get("workspace_path") is None
