"""Optional adapters that implement external knowledge-port contracts structurally."""

from arcmemory.adapters.fleet_shared_knowledge import FleetSharedKnowledgeBackend
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter

__all__ = ["FleetSharedKnowledgeBackend", "PersonalKnowledgeAdapter", "SharedKnowledgeAdapter"]
