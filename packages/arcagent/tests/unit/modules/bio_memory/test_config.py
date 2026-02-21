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

    def test_default_identity_budget(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.identity_budget == 500

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

    def test_default_significance_model(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.significance_model == "llm"

    def test_default_working_filename(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.working_filename == "working.md"

    def test_default_identity_filename(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.identity_filename == "how-i-work.md"

    def test_default_episodes_dirname(self) -> None:
        cfg = BioMemoryConfig()
        assert cfg.episodes_dirname == "episodes"


class TestBioMemoryConfigOverrides:
    """Custom values are respected."""

    def test_custom_budgets(self) -> None:
        cfg = BioMemoryConfig(
            total_per_turn=8000,
            identity_budget=1000,
            retrieved_budget=5000,
            working_budget=1000,
        )
        assert cfg.total_per_turn == 8000
        assert cfg.identity_budget == 1000
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
            identity_filename="identity.md",
            episodes_dirname="memories",
        )
        assert cfg.working_filename == "scratch.md"
        assert cfg.identity_filename == "identity.md"
        assert cfg.episodes_dirname == "memories"


class TestBioMemoryConfigValidation:
    """Inherits extra=forbid from ModuleConfig — typos caught."""

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BioMemoryConfig(unknown_field="oops")  # type: ignore[call-arg]

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BioMemoryConfig(total_per_turn="not_an_int")  # type: ignore[arg-type]
