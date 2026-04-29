"""Entity registry for agents and users with role-based queries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from arcteam.audit import AuditLogger
from arcteam.storage import StorageBackend
from arcteam.types import Entity

REGISTRY_COLLECTION = "messages/registry"


def _entity_key(entity_id: str) -> str:
    """Convert entity URI to filesystem-safe key: agent://x -> agent_x."""
    return entity_id.replace("://", "_")


async def list_entities_readonly(backend: StorageBackend, role: str | None = None) -> list[Entity]:
    """Read-only enumeration of every Entity, no audit logger required.

    Wave 2 review: arccli was rebuilding `AuditLogger` + `FileBackend`
    +  HMAC key load just to enumerate registered agents. Pure-read
    callers (CLI status, dashboards) shouldn't need to bootstrap the
    audit chain — this helper exposes the read path without it.

    Use this from arccli/scripts; use `EntityRegistry.list_entities`
    inside the team service where audit-emitting writes also happen.
    """
    records = await backend.query(REGISTRY_COLLECTION)
    entities = [Entity.model_validate(r) for r in records]
    if role:
        entities = [e for e in entities if role in e.roles]
    return entities


class EntityRegistry:
    """Agent and user registration with role-based queries."""

    def __init__(
        self,
        backend: StorageBackend,
        audit: AuditLogger,
        ui_reporter: Any | None = None,
    ) -> None:
        self._backend = backend
        self._audit = audit
        # Duck-typed UIReporter hook. No arcui import — caller injects if needed.
        self._ui_reporter = ui_reporter

    async def register(self, entity: Entity) -> None:
        """Register a new entity. Rejects duplicates."""
        key = _entity_key(entity.id)
        existing = await self._backend.read(REGISTRY_COLLECTION, key)
        if existing is not None:
            raise ValueError(f"Entity already registered: {entity.id}")

        if not entity.created:
            entity.created = datetime.now(UTC).isoformat()

        await self._backend.write(REGISTRY_COLLECTION, key, entity.model_dump())
        await self._audit.log(
            event_type="entity.registered",
            subject=f"registry.{entity.type.value}",
            actor_id=entity.id,
            detail=f"Registered {entity.type.value} '{entity.name}' with roles {entity.roles}",
            target_id=entity.id,
        )
        # UIReporter hook — fires if a reporter was injected (duck-typed, no arcui dep).
        if self._ui_reporter is not None:
            self._ui_reporter.emit_team_event(
                event_type="entity_register",
                data={
                    "entity_id": entity.id,
                    "entity_name": entity.name,
                    "entity_type": entity.type.value,
                },
            )

    async def get(self, entity_id: str) -> Entity | None:
        """Read entity by ID."""
        key = _entity_key(entity_id)
        data = await self._backend.read(REGISTRY_COLLECTION, key)
        if data is None:
            return None
        return Entity.model_validate(data)

    async def list_entities(self, role: str | None = None) -> list[Entity]:
        """All entities, optionally filtered by role."""
        records = await self._backend.query(REGISTRY_COLLECTION)
        entities = [Entity.model_validate(r) for r in records]
        if role:
            entities = [e for e in entities if role in e.roles]
        return entities

    async def by_role(self, role: str) -> list[Entity]:
        """All entities with this role. Used for role-based addressing."""
        return await self.list_entities(role=role)

    async def update_status(self, entity_id: str, status: str) -> None:
        """Update entity status."""
        key = _entity_key(entity_id)
        data = await self._backend.read(REGISTRY_COLLECTION, key)
        if data is None:
            raise ValueError(f"Entity not found: {entity_id}")

        old_status = data.get("status", "unknown")
        data["status"] = status
        await self._backend.write(REGISTRY_COLLECTION, key, data)
        await self._audit.log(
            event_type="entity.status_changed",
            subject="registry.status",
            actor_id=entity_id,
            detail=f"Status changed from '{old_status}' to '{status}'",
            target_id=entity_id,
        )

    async def update(self, entity: Entity) -> None:
        """Replace an existing entity record. Emits `entity.updated` audit event.

        SPEC-019 T1.2: write-through update used by `arc team backfill-workspaces`
        to persist `workspace_path`. Identity (id) is the lookup key; rejecting
        unknown ids prevents the caller from silently registering via update().
        """
        key = _entity_key(entity.id)
        existing = await self._backend.read(REGISTRY_COLLECTION, key)
        if existing is None:
            raise ValueError(f"Entity not found: {entity.id}")

        await self._backend.write(REGISTRY_COLLECTION, key, entity.model_dump())
        await self._audit.log(
            event_type="entity.updated",
            subject=f"registry.{entity.type.value}",
            actor_id=entity.id,
            detail=f"Updated {entity.type.value} '{entity.name}'",
            target_id=entity.id,
        )
