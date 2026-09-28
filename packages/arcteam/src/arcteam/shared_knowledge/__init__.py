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
from arcteam.shared_knowledge.port import FleetSharedKnowledgePort
from arcteam.shared_knowledge.service import (
    FleetSharedKnowledgeService,
    SharedKnowledgePromotionOutcomeUnknownError,
    SharedKnowledgePromotionRefusedError,
    SharedKnowledgeUnavailableError,
)

__all__ = [
    "ComposedSharedKnowledgeAgent",
    "FleetSharedKnowledgeBackend",
    "FleetSharedKnowledgeComposition",
    "FleetSharedKnowledgePort",
    "FleetSharedKnowledgeService",
    "SharedKnowledgeAttachment",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgePromotionOutcomeUnknownError",
    "SharedKnowledgePromotionRefusedError",
    "SharedKnowledgeReference",
    "SharedKnowledgeSummary",
    "SharedKnowledgeUnavailableError",
]
