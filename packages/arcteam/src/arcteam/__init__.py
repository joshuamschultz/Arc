"""ArcTeam: Multi-agent team coordination and lifecycle management."""

from typing import TYPE_CHECKING, Any

__version__ = "0.8.0"

from arcteam.audit import AuditLogger
from arcteam.composition import is_fleet_transport_error
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
from arcteam.storage import (
    FleetBackendUnavailableError,
    MemoryBackend,
    StorageBackend,
    UnavailableBackend,
)
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

if TYPE_CHECKING:
    from arcteam.backends.nats import NatsBackend


def __getattr__(name: str) -> Any:
    """Load the optional NATS adapter only when explicitly requested."""
    if name == "NatsBackend":
        from arcteam.backends.nats import NatsBackend

        return NatsBackend
    raise AttributeError(name)


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
    "FleetBackendUnavailableError",
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
    "UnavailableBackend",
    "WaitingQuestion",
    "is_fleet_transport_error",
    "mail_participant",
    "waiting_on_human",
]
