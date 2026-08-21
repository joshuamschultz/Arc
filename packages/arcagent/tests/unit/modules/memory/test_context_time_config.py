"""RED — arcagent memory-module toggles for SPEC-072 (working-set + decision-point).

The thin memory module gains two operator surfaces: ``working_set_enabled`` (default
on; folded into the backend ``dynamics`` so it reaches the arcmemory Brain) and
``decision_point_pre_tool`` (default off; the pre-tool decision-point opt-in). These
pin the config surface only — the wiring behavior is exercised in later tasks.
"""

from __future__ import annotations

from arcagent.modules.memory.config import MemoryConfig


def test_working_set_enabled_defaults_to_true() -> None:
    assert MemoryConfig().working_set_enabled is True


def test_working_set_enabled_can_be_disabled() -> None:
    assert MemoryConfig(working_set_enabled=False).working_set_enabled is False


def test_decision_point_pre_tool_defaults_to_false() -> None:
    assert MemoryConfig().decision_point_pre_tool is False


def test_decision_point_pre_tool_can_be_enabled() -> None:
    assert MemoryConfig(decision_point_pre_tool=True).decision_point_pre_tool is True


def test_working_set_enabled_folds_into_backend_dynamics() -> None:
    """The toggle reaches the arcmemory Brain via the forwarded ``dynamics`` dict."""
    cfg = MemoryConfig(working_set_enabled=False)
    dynamics = cfg.backend.get("dynamics", {})
    assert dynamics.get("working_set_enabled") is False


def test_explicit_dynamics_override_still_reaches_backend() -> None:
    """An explicit ``dynamics`` block is preserved when the toggle is folded in."""
    cfg = MemoryConfig(dynamics={"temporal_enabled": False})
    dynamics = cfg.backend.get("dynamics", {})
    assert dynamics.get("temporal_enabled") is False
    assert "working_set_enabled" in dynamics
