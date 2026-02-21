"""Team memory configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class TeamMemoryConfig(BaseModel):
    """Team memory configuration. All fields have defaults."""

    enabled: bool = True
    root: Path = Path.home() / ".arc" / "team"

    # Entity settings
    entity_types: list[str] = Field(
        default=["person", "organization", "project", "domain", "process", "playbook"]
    )
    per_entity_budget: int = 800  # tokens

    # Search settings
    max_hops: int = 3
    bm25_threshold_ratio: float = 0.3
    max_results: int = 20

    # Consolidation (Phase 2 — config ready now)
    consolidation_enabled: bool = True
    consolidation_model: str = ""  # empty = arcllm default

    # Security
    classification_required: bool = True
    encryption_at_rest: bool = False

    # Tier (read from global config)
    tier: str = "personal"  # "federal" | "enterprise" | "personal"

    @property
    def entities_dir(self) -> Path:
        """Path to entities directory."""
        return self.root / "entities"

    @property
    def index_path(self) -> Path:
        """Path to _index.json."""
        return self.entities_dir / "_index.json"
