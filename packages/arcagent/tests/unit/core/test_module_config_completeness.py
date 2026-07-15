"""Every discovered module ships a config.py with a ModuleConfig subclass.

Completeness invariant: a module is self-contained only if its settings schema
lives beside it on the shared ``ModuleConfig`` base (``extra='forbid'``), so a
misspelled key is a loud validation error rather than a silent no-op.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from arcagent.core.module_config import ModuleConfig
from arcagent.core.module_discovery import discover_modules

# user_profile predates this standard and still subclasses BaseModel directly; it is
# out of scope for this change (would alter its validation semantics) and tracked
# separately.
_KNOWN_NON_MODULECONFIG = frozenset({"user_profile"})


def _config_class(module_name: str) -> type | None:
    config_mod = importlib.import_module(f"arcagent.modules.{module_name}.config")
    for _, obj in inspect.getmembers(config_mod, inspect.isclass):
        if issubclass(obj, ModuleConfig) and obj is not ModuleConfig:
            return obj
    return None


@pytest.mark.parametrize("module_name", discover_modules())
def test_every_module_has_a_config_module(module_name: str) -> None:
    importlib.import_module(f"arcagent.modules.{module_name}.config")


@pytest.mark.parametrize("module_name", sorted(set(discover_modules()) - _KNOWN_NON_MODULECONFIG))
def test_every_module_config_uses_the_shared_base(module_name: str) -> None:
    assert _config_class(module_name) is not None


@pytest.mark.parametrize("module_name", ["planning", "pulse", "proactive", "session"])
def test_new_configs_reject_unknown_keys(module_name: str) -> None:
    cls = _config_class(module_name)
    assert cls is not None
    # Valid empty construction works; an unknown key is rejected (extra='forbid').
    cls()
    with pytest.raises(Exception):  # pydantic ValidationError
        cls(definitely_not_a_real_key=1)


def test_pulse_config_defaults_preserved() -> None:
    from arcagent.modules.pulse.config import PulseConfig

    cfg = PulseConfig()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 600
    assert cfg.pulse_file == "pulse.md"
    assert cfg.state_file == "pulse-state.json"
    assert cfg.timeout_seconds == 300.0


def test_planning_config_defaults_preserved() -> None:
    from arcagent.modules.planning.config import PlanningConfig

    cfg = PlanningConfig()
    assert cfg.enabled is False
    assert cfg.max_replans == 3
    assert cfg.concurrent is False
    assert cfg.max_parallel == 8


def test_proactive_config_defaults_preserved() -> None:
    from arcagent.modules.proactive.config import ProactiveConfig

    cfg = ProactiveConfig()
    assert cfg.leader == "noop"
    assert cfg.redis_key == "arcagent:proactive:leader"


def test_session_config_defaults_preserved() -> None:
    from arcagent.modules.session.config import SessionConfig

    assert SessionConfig().poll_interval == 30.0
