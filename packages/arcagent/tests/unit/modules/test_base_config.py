"""Tests for ModuleConfig base class — extra='forbid' typo detection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcagent.modules.base_config import ModuleConfig
from arcagent.modules.memory.config import MemoryConfig
from arcagent.modules.policy.config import PolicyConfig


class TestModuleConfigForbidsExtra:
    """All module configs reject unknown keys."""

    def test_base_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            ModuleConfig(unknown_key="value")  # type: ignore[call-arg]

    def test_memory_rejects_typo(self) -> None:
        with pytest.raises(ValidationError):
            MemoryConfig(contex_budget_tokens=2000)  # type: ignore[call-arg]

    def test_memory_accepts_valid(self) -> None:
        cfg = MemoryConfig(context_budget_tokens=3000)
        assert cfg.context_budget_tokens == 3000

    def test_policy_rejects_typo(self) -> None:
        with pytest.raises(ValidationError):
            PolicyConfig(eval_intervall_turns=5)  # type: ignore[call-arg]

    def test_policy_accepts_valid(self) -> None:
        cfg = PolicyConfig(eval_interval_turns=10)
        assert cfg.eval_interval_turns == 10

    def test_scheduler_rejects_typo(self) -> None:
        from arcagent.modules.scheduler.config import SchedulerConfig

        with pytest.raises(ValidationError):
            SchedulerConfig(min_intervall_seconds=30)  # type: ignore[call-arg]

    def test_scheduler_accepts_valid(self) -> None:
        from arcagent.modules.scheduler.config import SchedulerConfig

        cfg = SchedulerConfig(min_interval_seconds=30)
        assert cfg.min_interval_seconds == 30

    def test_defaults_work(self) -> None:
        """All configs work with zero args (all fields have defaults)."""
        MemoryConfig()
        PolicyConfig()
