"""Configuration for the bio-memory module.

Owned by the bio_memory module — not part of core config.
Loaded from ``[modules.bio_memory.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class BioMemoryConfig(ModuleConfig):
    """Bio-memory configuration.

    All fields have defaults so the module works out-of-the-box
    with zero configuration. Inherits ``extra="forbid"`` from
    ModuleConfig for typo detection.
    """

    # Token budgets
    total_per_turn: int = 4000
    identity_budget: int = 500
    retrieved_budget: int = 3000
    working_budget: int = 500
    overflow_strategy: str = "truncate"

    # Consolidation
    light_on_shutdown: bool = True
    significance_model: str = "llm"

    # Paths (relative to workspace/memory/)
    working_filename: str = "working.md"
    identity_filename: str = "how-i-work.md"
    episodes_dirname: str = "episodes"
