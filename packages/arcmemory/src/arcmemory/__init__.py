"""arcmemory — Arc's dual-speed analogical memory substrate (SPEC-041).

Public surface for the foundation phase: the typed models, the config, the
per-agent SQLite substrate, the four stores, the weighted graph, the index
rebuilder, and the zero-LLM fast-capture path. Retrieval, consolidation, and the
``Brain`` Protocol land in later phases.
"""

from __future__ import annotations

from arcmemory.capture import FastCapture
from arcmemory.config import MemoryConfig, Tier
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, IndexRebuilder
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
)

__version__ = "0.1.0"

__all__ = [
    "Bundle",
    "Confidence",
    "ConsolidationResult",
    "Cue",
    "Embedder",
    "Entity",
    "EpisodicStore",
    "Event",
    "Fact",
    "FastCapture",
    "IndexRebuilder",
    "Insight",
    "InsightStore",
    "MemoryConfig",
    "MemoryDB",
    "ProceduralStore",
    "Procedure",
    "Recall",
    "Scope",
    "SemanticStore",
    "Situation",
    "Tier",
    "WeightedGraph",
    "__version__",
]
