"""Tests for Entity.workspace_path field (SPEC-019 FR-2).

workspace_path is optional-with-default: a record that omits it MUST
deserialize cleanly with the field set to None.
"""

from __future__ import annotations

from arcteam.types import Entity, EntityType


def _entity(**overrides: object) -> Entity:
    base: dict[str, object] = {
        "did": "did:arc:test:agent/a1",
        "handle": "a1",
        "id": "agent://a1",
        "name": "A1",
        "type": EntityType.AGENT,
    }
    base.update(overrides)
    return Entity(**base)  # type: ignore[arg-type]


class TestEntityWorkspacePath:
    """workspace_path: str | None — added in SPEC-019."""

    def test_default_is_none(self) -> None:
        assert _entity().workspace_path is None

    def test_explicit_value_persisted(self) -> None:
        e = _entity(workspace_path="/abs/path/to/workspace")
        assert e.workspace_path == "/abs/path/to/workspace"

    def test_record_without_field_loads(self) -> None:
        """A record that omits workspace_path round-trips with None."""
        record = {
            "did": "did:arc:test:agent/legacy",
            "handle": "legacy",
            "id": "agent://legacy",
            "name": "Legacy",
            "type": "agent",
            "roles": ["ops"],
            "capabilities": [],
            "created": "2025-01-01T00:00:00Z",
            "status": "active",
            # Note: no workspace_path key
        }
        e = Entity.model_validate(record)
        assert e.workspace_path is None
        assert e.id == "agent://legacy"

    def test_serialization_includes_field(self) -> None:
        data = _entity(workspace_path="/abs").model_dump()
        assert "workspace_path" in data
        assert data["workspace_path"] == "/abs"

    def test_serialization_when_none(self) -> None:
        data = _entity().model_dump()
        # Field present but value None — JSON serializers can omit on None if needed
        assert data.get("workspace_path") is None
