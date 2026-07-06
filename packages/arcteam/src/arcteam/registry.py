"""Entity registry (DID-keyed) and the sole address resolver."""

from __future__ import annotations

from datetime import UTC, datetime

from arcteam.audit import AuditLogger
from arcteam.storage import StorageBackend
from arcteam.types import Entity, EntityStatus, parse_uri

REGISTRY_COLLECTION = "messages/registry"


class UnknownHandle(ValueError):  # noqa: N818  # reason: name fixed by REQ-002 (typed `UnknownHandle`)
    """Raised when an address ref names no registered entity.

    REQ-002: an unknown handle is rejected with this typed error, never a
    silent Dead Letter Queue entry.
    """


def _entity_key(did: str) -> str:
    """Convert a DID to a flat, filesystem-safe storage key.

    ``did:arc:local:executor/abc`` -> ``did_arc_local_executor_abc`` so the
    key never contains ``:`` or ``/`` (which would nest directories and hide
    the record from a flat ``*.json`` glob).
    """
    return did.replace(":", "_").replace("/", "_")


def _match_handle(entities: list[Entity], handle: str, ref: str) -> str:
    """Return the DID of the entity with this handle, or raise UnknownHandle."""
    for entity in entities:
        if entity.handle == handle:
            return entity.did
    raise UnknownHandle(ref)


async def resolve(registry: EntityRegistry, ref: str) -> str:
    """Resolve any address ref to its canonical routing identity (REQ-002).

    This is the single resolution path for all addressing. Entity refs
    (``@handle``, ``agent://handle``, ``user://handle``, a bare handle, or a
    raw ``did:``) resolve to the entity's DID. Group refs (``channel://name``,
    ``role://name``) address a stream rather than an identity and pass through
    unchanged. An unknown entity ref raises :class:`UnknownHandle`.
    """
    entities = await registry.list_entities()
    if ref.startswith("did:"):
        if any(e.did == ref for e in entities):
            return ref
        raise UnknownHandle(ref)
    if ref.startswith("@"):
        return _match_handle(entities, ref[1:], ref)
    if "://" in ref:
        try:
            scheme, name = parse_uri(ref)
        except ValueError as exc:
            raise UnknownHandle(ref) from exc
        if scheme in ("channel", "role"):
            return ref
        return _match_handle(entities, name, ref)
    return _match_handle(entities, ref, ref)


async def list_entities_readonly(backend: StorageBackend, role: str | None = None) -> list[Entity]:
    """Read-only enumeration of every Entity, no audit logger required.

    Pure-read callers (CLI status, dashboards) shouldn't need to bootstrap
    the audit chain. Use ``EntityRegistry.list_entities`` inside the team
    service where audit-emitting writes also happen.
    """
    records = await backend.query(REGISTRY_COLLECTION)
    entities = [Entity.model_validate(r) for r in records]
    if role:
        entities = [e for e in entities if role in e.roles]
    return entities


class EntityRegistry:
    """DID-keyed agent and user registration with role-based queries."""

    def __init__(
        self,
        backend: StorageBackend,
        audit: AuditLogger,
    ) -> None:
        self._backend = backend
        self._audit = audit

    async def register(self, entity: Entity) -> None:
        """Register a new entity. Rejects a duplicate DID or handle."""
        key = _entity_key(entity.did)
        if await self._backend.read(REGISTRY_COLLECTION, key) is not None:
            raise ValueError(f"Entity already registered: {entity.did}")
        for existing in await self.list_entities():
            if existing.handle == entity.handle:
                raise ValueError(f"Handle already registered: {entity.handle}")

        if not entity.created:
            entity.created = datetime.now(UTC).isoformat()

        await self._backend.write(REGISTRY_COLLECTION, key, entity.model_dump())
        await self._audit.log(
            event_type="entity.registered",
            subject=f"registry.{entity.type.value}",
            actor_id=entity.did,
            detail=f"Registered {entity.type.value} @{entity.handle} with roles {entity.roles}",
            target_id=entity.did,
        )

    async def get(self, ref: str) -> Entity | None:
        """Read an entity by any address ref (DID, @handle, URI, bare handle)."""
        try:
            did = await resolve(self, ref)
        except UnknownHandle:
            return None
        if not did.startswith("did:"):
            return None
        data = await self._backend.read(REGISTRY_COLLECTION, _entity_key(did))
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

    async def set_status(self, ref: str, status: EntityStatus) -> None:
        """Transition an entity's presence state, resolving any ref to its DID.

        The presence lifecycle entry point (REQ-021): callers set ``active`` on
        serve/turn start, ``idle``/``waiting``/``blocked`` mid-run, and
        ``offline`` on stop. Persists the new state and audits the transition.
        """
        entity = await self.get(ref)
        if entity is None:
            raise ValueError(f"Entity not found: {ref}")

        old_status = entity.status
        entity.status = status
        await self._backend.write(
            REGISTRY_COLLECTION, _entity_key(entity.did), entity.model_dump()
        )
        await self._audit.log(
            event_type="entity.status_changed",
            subject="registry.status",
            actor_id=entity.did,
            detail=f"Status changed from '{old_status.value}' to '{status.value}'",
            target_id=entity.did,
        )

    async def update_status(self, ref: str, status: EntityStatus) -> None:
        """Update an entity's presence state by address ref."""
        await self.set_status(ref, status)

    async def update(self, entity: Entity) -> None:
        """Replace an existing entity record. Emits `entity.updated`."""
        key = _entity_key(entity.did)
        if await self._backend.read(REGISTRY_COLLECTION, key) is None:
            raise ValueError(f"Entity not found: {entity.did}")

        await self._backend.write(REGISTRY_COLLECTION, key, entity.model_dump())
        await self._audit.log(
            event_type="entity.updated",
            subject=f"registry.{entity.type.value}",
            actor_id=entity.did,
            detail=f"Updated {entity.type.value} @{entity.handle}",
            target_id=entity.did,
        )
