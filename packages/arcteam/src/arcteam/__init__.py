"""ArcTeam: Multi-agent team coordination and lifecycle management."""

__version__ = "0.2.0"

from arcteam.audit import AuditLogger
from arcteam.config import TeamConfig
from arcteam.files import TeamFileStore
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import FileBackend, MemoryBackend, StorageBackend
from arcteam.types import (
    AuditRecord,
    Channel,
    Cursor,
    Entity,
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
    "EntityType",
    "FileBackend",
    "MemoryBackend",
    "Message",
    "MessagingService",
    "MsgType",
    "Priority",
    "StorageBackend",
    "TeamConfig",
    "TeamFileStore",
    "TeamMemoryConfig",
    "TeamMemoryService",
]
