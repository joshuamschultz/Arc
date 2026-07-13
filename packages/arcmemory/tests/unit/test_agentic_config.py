"""Agentic-consolidation config knobs (SPEC agentic-memory)."""

from __future__ import annotations

from arcmemory.config import MemoryConfig


def test_default_engine_is_agentic() -> None:
    assert MemoryConfig().consolidate_engine == "agentic"


def test_agentic_bounds_have_tight_defaults() -> None:
    cfg = MemoryConfig()
    assert cfg.consolidate_agent_max_turns == 8
    assert cfg.consolidate_agent_max_tokens == 12_000
    assert cfg.consolidate_agent_timeout_seconds == 120.0


def test_federal_tightens_agentic_bounds() -> None:
    cfg = MemoryConfig.for_tier("federal")
    # Federal caps turns/tokens harder (bounded consumption, LLM10) than personal.
    assert cfg.consolidate_agent_max_turns <= MemoryConfig().consolidate_agent_max_turns
    assert cfg.consolidate_agent_max_tokens <= MemoryConfig().consolidate_agent_max_tokens


def test_engine_is_overridable() -> None:
    assert MemoryConfig(consolidate_engine="pipeline").consolidate_engine == "pipeline"
