"""Bio-memory module — biologically-inspired memory.

Working memory, daily notes, episodes, and entity consolidation.
"""

from arcagent.modules.bio_memory.bio_memory_module import BioMemoryModule
from arcagent.modules.bio_memory.cli import cli_group
from arcagent.modules.bio_memory.config import BioMemoryConfig
from arcagent.modules.bio_memory.deep_consolidator import DeepConsolidator
from arcagent.modules.bio_memory.errors import (
    BioMemoryError,
    ConsolidationError,
    RetrievalError,
)
from arcagent.modules.bio_memory.facts import Fact, format_fact, parse_fact, parse_facts
from arcagent.modules.bio_memory.retriever import RetrievalResult

__all__ = [
    "BioMemoryConfig",
    "BioMemoryError",
    "BioMemoryModule",
    "ConsolidationError",
    "DeepConsolidator",
    "Fact",
    "RetrievalError",
    "RetrievalResult",
    "cli_group",
    "format_fact",
    "parse_fact",
    "parse_facts",
]
