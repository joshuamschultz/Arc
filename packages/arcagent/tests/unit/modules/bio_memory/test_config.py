"""Tests for BioMemoryConfig — Pydantic validation and defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.bio_memory.config import BioMemoryConfig


class TestBioMemoryConfigDefaults:
    """All fields have sensible defaults — zero-config experience."""

    def test_default_total_per_turn(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.total_per_turn == 4000

    def test_default_retrieved_budget(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.retrieved_budget == 3000

    def test_default_working_budget(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.working_budget == 500

    def test_default_overflow_strategy(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.overflow_strategy == "truncate"

    def test_default_light_on_shutdown(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.light_on_shutdown is True

    def test_default_working_filename(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.working_filename == "working.md"

    def test_default_episodes_dirname(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.episodes_dirname == "episodes"

    def test_default_entities_dirname(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.entities_dirname == "entities"

    def test_default_per_entity_budget(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.per_entity_budget == 800

    def test_default_deep_max_entities(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.deep_max_entities == 50

    def test_default_deep_cluster_size(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.deep_cluster_size == 20

    def test_default_staleness_ttl_days(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.staleness_ttl_days == 90

    def test_default_archive_dirname(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.archive_dirname == "archive"

    def test_default_rotation_state_file(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.rotation_state_file == ".consolidation-state.json"


class TestBioMemoryConfigOverrides:
    """Custom values are respected."""

    def test_custom_budgets(self) -> None:
        cfg = BioMemoryConfig(
            total_per_turn=8000,
            retrieved_budget=5000,
            working_budget=1000,
        )
        assert cfg.total_per_turn == 8000
        assert cfg.retrieved_budget == 5000
        assert cfg.working_budget == 1000

    def test_custom_overflow_strategy(self) -> None:
        cfg = BioMemoryConfig(overflow_strategy="summarize")
        assert cfg.overflow_strategy == "summarize"

    def test_disable_light_consolidation(self) -> None:
        cfg = BioMemoryConfig(light_on_shutdown=False)
        assert cfg.light_on_shutdown is False

    def test_custom_filenames(self) -> None:
        cfg = BioMemoryConfig(
            working_filename="scratch.md",
            episodes_dirname="memories",
        )
        assert cfg.working_filename == "scratch.md"
        assert cfg.episodes_dirname == "memories"

    def test_custom_entity_config(self) -> None:
        cfg = BioMemoryConfig(
            entities_dirname="knowledge",
            per_entity_budget=1200,
        )
        assert cfg.entities_dirname == "knowledge"
        assert cfg.per_entity_budget == 1200

    def test_custom_deep_consolidation_config(self) -> None:
        cfg = BioMemoryConfig(
            deep_max_entities=100,
            deep_cluster_size=30,
            staleness_ttl_days=180,
            archive_dirname="old",
            rotation_state_file=".rotation.json",
        )
        assert cfg.deep_max_entities == 100
        assert cfg.deep_cluster_size == 30
        assert cfg.staleness_ttl_days == 180
        assert cfg.archive_dirname == "old"
        assert cfg.rotation_state_file == ".rotation.json"


class TestBioMemoryConfigValidation:
    """Inherits extra=forbid from ModuleConfig — typos caught."""

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BioMemoryConfig(unknown_field="oops")  # type: ignore[call-arg]

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BioMemoryConfig(total_per_turn="not_an_int")  # type: ignore[arg-type]
