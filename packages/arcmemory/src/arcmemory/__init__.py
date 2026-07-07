"""arcmemory — Arc's dual-speed analogical memory substrate (SPEC-041).

Public surface: the typed models, the config, the per-agent SQLite substrate, the
four stores, the weighted graph, the index rebuilder, the zero-LLM fast-capture
path, the surface + structural retrieval channels, sleep consolidation, and the
``ArcMemoryBrain`` that satisfies arcagent's structural ``Brain`` seam. The
``ArcLLMEmbedder`` / ``ArcLLMDistiller`` adapters bridge the async embedder /
distiller seams onto arcllm so semantic recall and consolidation run in production.
"""

from __future__ import annotations

from arcmemory.arcllm_seam import ArcLLMDistiller, ArcLLMEmbedder
from arcmemory.brain import ArcMemoryBrain
from arcmemory.capture import FastCapture
from arcmemory.config import MemoryConfig, Tier
from arcmemory.consolidate import Consolidator
from arcmemory.db import MemoryDB
from arcmemory.distill import (
    Distiller,
    FactCandidate,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    confidence_from_hits,
    extract_facts,
    mint_insights,
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
from arcmemory.retrieve import Retriever
from arcmemory.security import (
    boundary_mark,
    gate_no_read_up,
    render_recalls,
)
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.insight import InsightStore
from arcmemory.stores.procedural import ProceduralStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import (
    Bundle,
    Confidence,
    ConsolidationResult,
    Cue,
    Entity,
    Event,
    Fact,
    Insight,
    Procedure,
    Recall,
    Scope,
    Situation,
    TimeWindow,
)

__version__ = "0.6.0"

__all__ = [
    "ArcLLMDistiller",
    "ArcLLMEmbedder",
    "ArcMemoryBrain",
    "Bundle",
    "Confidence",
    "ConsolidationResult",
    "Consolidator",
    "Cue",
    "Distiller",
    "Embedder",
    "EmbeddingUnavailableError",
    "Entity",
    "EpisodicStore",
    "Event",
    "Fact",
    "FactCandidate",
    "FactExtraction",
    "FastCapture",
    "IndexRebuilder",
    "Insight",
    "InsightBundle",
    "InsightCandidate",
    "InsightMint",
    "InsightStore",
    "MemoryConfig",
    "MemoryDB",
    "ProceduralStore",
    "Procedure",
    "Recall",
    "Reranker",
    "Retriever",
    "Scope",
    "SemanticStore",
    "Situation",
    "StructuralIndex",
    "StructuralResult",
    "SurfaceIndex",
    "SurfaceResult",
    "Tier",
    "TimeWindow",
    "WeightedGraph",
    "__version__",
    "boundary_mark",
    "confidence_from_hits",
    "extract_facts",
    "gate_no_read_up",
    "mint_insights",
    "render_recalls",
]
