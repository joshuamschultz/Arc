"""Proactive detected-moment recall opt-out (SPEC-071).

``proactive_enabled`` governs whether the memory module emits/subscribes to
``agent:moment`` at all. Default True (proactive recall on by default);
setting it False must fully disable the feature per REQ-348 (the agent runs
unchanged with no subscription effect) — that behavior is exercised in the
wiring tests once the hook exists. Here we pin only the config surface: the
field exists, defaults True, and is settable.
"""

from __future__ import annotations

from arcagent.modules.memory.config import MemoryConfig


def test_proactive_enabled_defaults_to_true() -> None:
    assert MemoryConfig().proactive_enabled is True


def test_proactive_enabled_can_be_disabled() -> None:
    assert MemoryConfig(proactive_enabled=False).proactive_enabled is False
