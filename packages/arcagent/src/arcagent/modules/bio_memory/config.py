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
    retrieved_budget: int = 3000
    working_budget: int = 500
    overflow_strategy: str = "truncate"

    # Consolidation
    light_on_shutdown: bool = True
    consolidation_interval_turns: int = 15

    # Paths (relative to workspace/memory/)
    working_filename: str = "working.md"
    episodes_dirname: str = "episodes"
    daily_notes_dirname: str = "daily-notes"

    # Entity config (workspace-level, NOT inside memory/)
    entities_dirname: str = "entities"
    per_entity_budget: int = 800

    # Deep consolidation
    deep_max_entities: int = 50
    deep_cluster_size: int = 20
    staleness_ttl_days: int = 90
    archive_dirname: str = "archive"
    rotation_state_file: str = ".consolidation-state.json"
