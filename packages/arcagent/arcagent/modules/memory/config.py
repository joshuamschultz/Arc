"""Configuration for the memory module.

Owned by the memory module — not part of core config.
Loaded from ``[modules.memory.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from pydantic import BaseModel


class MemoryConfig(BaseModel):
    """Memory module configuration.

    All fields have defaults so the module works out-of-the-box
    with zero configuration.
    """

    context_budget_tokens: int = 2000
    notes_budget_today_tokens: int = 1000
    notes_budget_yesterday_tokens: int = 500
    search_weight_bm25: float = 0.7
    search_weight_vector: float = 0.3
    embedding_model: str = "all-MiniLM-L6-v2"
    entity_extraction_enabled: bool = True
