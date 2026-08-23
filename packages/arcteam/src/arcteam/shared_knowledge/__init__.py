"""Fleet shared-knowledge composition and lifecycle APIs."""

from arcteam.shared_knowledge.attachment import SharedKnowledgeAttachment
from arcteam.shared_knowledge.backend import (
    FleetSharedKnowledgeBackend,
    SharedKnowledgeDocument,
    SharedKnowledgeHit,
    SharedKnowledgeReference,
)
from arcteam.shared_knowledge.service import (
    FleetSharedKnowledgeService,
    SharedKnowledgeUnavailableError,
)

__all__ = [
    "FleetSharedKnowledgeBackend",
    "FleetSharedKnowledgeService",
    "SharedKnowledgeAttachment",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgeReference",
    "SharedKnowledgeUnavailableError",
]
