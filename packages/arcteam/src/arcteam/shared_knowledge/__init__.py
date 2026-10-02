"""Fleet shared-knowledge composition and lifecycle APIs."""

from arcteam.shared_knowledge.attachment import SharedKnowledgeAttachment
from arcteam.shared_knowledge.backend import (
    FleetSharedKnowledgeBackend,
    SharedKnowledgeDemotion,
    SharedKnowledgeDocument,
    SharedKnowledgeHit,
    SharedKnowledgeProvenance,
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
    "SharedKnowledgeDemotion",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgePromotionOutcomeUnknownError",
    "SharedKnowledgePromotionRefusedError",
    "SharedKnowledgeProvenance",
    "SharedKnowledgeReference",
    "SharedKnowledgeSummary",
    "SharedKnowledgeUnavailableError",
]
