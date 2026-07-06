"""ArcTeam: Multi-agent team coordination and lifecycle management."""

__version__ = "0.3.0"

from arcteam.audit import AuditLogger
from arcteam.backends.nats import NatsBackend
from arcteam.config import TeamConfig
from arcteam.files import TeamFileStore
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.messenger import MessagingService, RetryableDeliveryError
from arcteam.registry import EntityRegistry
from arcteam.roster import Roster, RosterEntry
from arcteam.storage import MemoryBackend, StorageBackend
from arcteam.team import Team, TeamStore
from arcteam.types import (
    AuditRecord,
    Channel,
    Cursor,
    Entity,
    EntityStatus,
    EntityType,
    Message,
    MsgType,
    Priority,
)

__all__ = [
    "AuditLogger",
    "AuditRecord",
    "Channel",
    "Cursor",
    "Entity",
    "EntityRegistry",
    "EntityStatus",
    "EntityType",
    "MemoryBackend",
    "Message",
    "MessagingService",
    "MsgType",
    "NatsBackend",
    "Priority",
    "RetryableDeliveryError",
    "Roster",
    "RosterEntry",
    "StorageBackend",
    "Team",
    "TeamConfig",
    "TeamFileStore",
    "TeamMemoryConfig",
    "TeamMemoryService",
    "TeamStore",
]
