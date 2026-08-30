"""Fleet shared-knowledge composition and lifecycle APIs."""

from arcteam.shared_knowledge.attachment import SharedKnowledgeAttachment
from arcteam.shared_knowledge.backend import (
    FleetSharedKnowledgeBackend,
    SharedKnowledgeDocument,
    SharedKnowledgeHit,
    SharedKnowledgeReference,
    SharedKnowledgeSummary,
)
from arcteam.shared_knowledge.composition import (
    ComposedSharedKnowledgeAgent,
    FleetSharedKnowledgeComposition,
)
from arcteam.shared_knowledge.service import (
    FleetSharedKnowledgeService,
    SharedKnowledgeUnavailableError,
)

__all__ = [
    "ComposedSharedKnowledgeAgent",
    "FleetSharedKnowledgeBackend",
    "FleetSharedKnowledgeComposition",
    "FleetSharedKnowledgeService",
    "SharedKnowledgeAttachment",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgeReference",
    "SharedKnowledgeSummary",
    "SharedKnowledgeUnavailableError",
]
