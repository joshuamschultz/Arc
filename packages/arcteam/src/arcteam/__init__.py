"""ArcTeam: Multi-agent team coordination and lifecycle management."""

__version__ = "0.8.0"

from arcteam.audit import AuditLogger
from arcteam.backends.nats import NatsBackend
from arcteam.config import TeamConfig
from arcteam.files import TeamFileStore
from arcteam.mail import (
    AgentMailService,
    MailSendRequest,
    MailSendResult,
    RegistryMailAddressBook,
    mail_participant,
)
from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
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
from arcteam.waiting import OperatorScope, WaitingQuestion, waiting_on_human

__all__ = [
    "AgentMailService",
    "AuditLogger",
    "AuditRecord",
    "Channel",
    "Cursor",
    "DeliveryKind",
    "Entity",
    "EntityRegistry",
    "EntityStatus",
    "EntityType",
    "MailSendRequest",
    "MailSendResult",
    "MemoryBackend",
    "Message",
    "MessagingService",
    "MsgType",
    "NatsBackend",
    "OperatorScope",
    "Priority",
    "RegistryMailAddressBook",
    "RetryableDeliveryError",
    "StorageBackend",
    "Team",
    "TeamConfig",
    "TeamFileStore",
    "TeamMemoryConfig",
    "TeamMemoryService",
    "TeamStore",
    "WaitingQuestion",
    "mail_participant",
    "waiting_on_human",
]
