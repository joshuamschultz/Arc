"""Tests for TeamMemoryConfig."""

from __future__ import annotations

from pathlib import Path

from arcteam.memory.config import TeamMemoryConfig


class TestTeamMemoryConfig:
    """TeamMemoryConfig should have sensible defaults and validate correctly."""

    def test_defaults(self) -> None:
        config = TeamMemoryConfig()
        assert config.enabled is True
        assert config.per_entity_budget == 800
        assert config.max_hops == 3
        assert config.bm25_threshold_ratio == 0.3
        assert config.max_results == 20
        assert config.classification_required is True
        assert config.tier == "personal"

    def test_custom_root(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path / "custom")
        assert config.root == tmp_path / "custom"

    def test_custom_entity_types(self) -> None:
        config = TeamMemoryConfig(entity_types=["agent", "tool"])
        assert config.entity_types == ["agent", "tool"]

    def test_federal_tier(self) -> None:
        config = TeamMemoryConfig(tier="federal")
        assert config.tier == "federal"

    def test_entities_dir(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path)
        assert config.entities_dir == tmp_path / "entities"

    def test_index_path(self, tmp_path: Path) -> None:
        config = TeamMemoryConfig(root=tmp_path)
        assert config.index_path == tmp_path / "entities" / "_index.json"

    def test_disabled(self) -> None:
        config = TeamMemoryConfig(enabled=False)
        assert config.enabled is False
