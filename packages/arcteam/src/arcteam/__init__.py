"""ArcTeam: Multi-agent team coordination and lifecycle management."""

__version__ = "0.8.0"

from arcteam.audit import AuditLogger
from arcteam.backends.nats import NatsBackend
from arcteam.config import TeamConfig
from arcteam.files import TeamFileStore
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.mail import AgentMailService, MailSendRequest, MailSendResult, mail_participant
from arcteam.messenger import MessagingService, RetryableDeliveryError
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend, StorageBackend
from arcteam.team import Team, TeamStore
from arcteam.types import (
    AuditRecord,
    Channel,
    Cursor,
    DeliveryKind,
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
    "AgentMailService",
    "Channel",
    "Cursor",
    "DeliveryKind",
    "Entity",
    "EntityRegistry",
    "EntityStatus",
    "EntityType",
    "MemoryBackend",
    "Message",
    "MailSendRequest",
    "MailSendResult",
    "mail_participant",
    "MessagingService",
    "MsgType",
    "NatsBackend",
    "Priority",
    "RetryableDeliveryError",
    "StorageBackend",
    "Team",
    "TeamConfig",
    "TeamFileStore",
    "TeamMemoryConfig",
    "TeamMemoryService",
    "TeamStore",
]
