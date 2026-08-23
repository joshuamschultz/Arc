"""arcmemory — Arc's dual-speed analogical memory substrate (SPEC-041).

Public surface: the typed models, the config, the per-agent SQLite substrate, the
four stores, the weighted graph, the index rebuilder, the zero-LLM fast-capture
path, the surface + structural retrieval channels, sleep consolidation, and the
``ArcMemoryBrain`` that satisfies arcagent's structural ``Brain`` seam. The
``ArcLLMEmbedder`` / ``ArcLLMDistiller`` adapters bridge the async embedder /
distiller seams onto arcllm so semantic recall and consolidation run in production.

``semantic_status`` + ``semantic_degraded`` are the operator-facing half of that:
recall degrades to BM25 + graph when no embedder is available, and those two make
the degrade visible instead of silent (see ``arcmemory.degrade``).
"""

from __future__ import annotations

from arcmemory.acl import (
    ACLViolation,
    CrossSessionVisibility,
    MemoryACLConfig,
    SessionACL,
    extract_acl_from_session_data,
)
from arcmemory.agent_consolidate import (
    AgenticResult,
    run_agentic_consolidation,
)
from arcmemory.arcllm_seam import ArcLLMDistiller, ArcLLMEmbedder
from arcmemory.brain import ArcMemoryBrain
from arcmemory.capture import FastCapture
from arcmemory.config import MemoryConfig, Tier
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.degrade import reset_degrade_warnings, semantic_degraded
from arcmemory.distill import (
    Distiller,
    EntityDisambiguator,
    EventCandidate,
    EventExtraction,
    FactCandidate,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    extract_events,
    extract_facts,
    mint_insights,
    resolve_entity,
)
from arcmemory.hygiene import (
    DedupReport,
    GroupMerge,
    StoreReport,
    dedup_workspace,
    discover_workspaces,
    repair_backlinks,
)
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, EmbeddingUnavailableError, IndexRebuilder
from arcmemory.index.structural import (
    InsightBundle,
    Reranker,
    StructuralIndex,
    StructuralResult,
)
from arcmemory.index.surface import SurfaceIndex, SurfaceResult
from arcmemory.operator import (
    EntityRecord,
    LinkRecord,
    MemoryOperator,
    MemoryPage,
    MemoryRecord,
    MutationResult,
    MutationStatus,
)
from arcmemory.provider import build_brain
from arcmemory.react_adapter import ReactLoop, ReactOutcome, run_react_loop
from arcmemory.retrieve import Retriever
from arcmemory.security import (
    boundary_mark,
    gate_no_read_up,
    render_recalls,
)
from arcmemory.status import SemanticStatus, WorkspaceVectors, semantic_status
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.events import EventStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.tools import MemoryTool, build_memory_tools
from arcmemory.types import (
    Bundle,
    Confidence,
    ConsolidationResult,
    Entity,
    Event,
    Fact,
    Insight,
    LifeEvent,
    Procedure,
    Recall,
    RecallCard,
    Scope,
    Situation,
    TimeWindow,
    confidence_from_hits,
)

__version__ = "0.9.0"

__all__ = [
    "ACLViolation",
    "AgenticResult",
    "ArcLLMDistiller",
    "ArcLLMEmbedder",
    "ArcMemoryBrain",
    "Bundle",
    "Confidence",
    "ConsolidationResult",
    "Consolidator",
    "CrossSessionVisibility",
    "DedupReport",
    "Distiller",
    "Embedder",
    "EmbeddingUnavailableError",
    "Entity",
    "EntityDisambiguator",
    "EntityRecord",
    "EpisodicStore",
    "Event",
    "EventCandidate",
    "EventExtraction",
    "EventStore",
    "Fact",
    "FactCandidate",
    "FactExtraction",
    "FastCapture",
    "GroupMerge",
    "IndexRebuilder",
    "Insight",
    "InsightBundle",
    "InsightCandidate",
    "InsightMint",
    "InsightStore",
    "LifeEvent",
    "LinkRecord",
    "MemoryACLConfig",
    "MemoryConfig",
    "MemoryDB",
    "MemoryOperator",
    "MemoryPage",
    "MemoryRecord",
    "MemoryTool",
    "MutationResult",
    "MutationStatus",
    "ProceduralStore",
    "Procedure",
    "ReactLoop",
    "ReactOutcome",
    "Recall",
    "RecallCard",
    "Reranker",
    "Retriever",
    "Scope",
    "SemanticStatus",
    "SemanticStore",
    "SessionACL",
    "Situation",
    "StoreReport",
    "StructuralIndex",
    "StructuralResult",
    "SurfaceIndex",
    "SurfaceResult",
    "Tier",
    "TimeWindow",
    "WeightedGraph",
    "WorkspaceVectors",
    "__version__",
    "boundary_mark",
    "build_brain",
    "build_memory_tools",
    "confidence_from_hits",
    "dedup_workspace",
    "discover_workspaces",
    "extract_acl_from_session_data",
    "extract_events",
    "extract_facts",
    "gate_no_read_up",
    "mint_insights",
    "render_recalls",
    "repair_backlinks",
    "reset_degrade_warnings",
    "resolve_entity",
    "run_agentic_consolidation",
    "run_react_loop",
    "semantic_degraded",
    "semantic_status",
    "sqlite_vec_loadable",
]
