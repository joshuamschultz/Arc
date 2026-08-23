"""Base configuration for all ArcAgent modules.

Module-framework infrastructure: provides ``ModuleConfig`` with
``extra="forbid"`` so misspelled module-config keys raise a validation error
instead of being silently ignored. Every module's ``config.py`` defines its
``<Name>Config`` on this base.
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError


class ModuleConfig(BaseModel):
    """Base config for ArcAgent modules.

    Uses ``extra="forbid"`` so misspelled config keys raise
    a validation error instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")


class _ModuleEntryLike(Protocol):
    enabled: bool
    config: dict[str, Any]


def validate_module_configs(modules: dict[str, Any]) -> list[str]:
    """Validate each enabled module's config table against its declared model.

    The runtime parses ``[modules.NAME.config]`` inside each module's
    ``configure()``, so a key the schema no longer accepts only surfaces as
    "Required module configuration failed" at first agent load — after any
    deploy gate reported green. This is the static half of that contract:
    resolve the module's ``<Name>Config`` by convention (``tasks`` →
    ``TasksConfig`` in ``arcagent.modules.tasks.config``) and validate the
    table without configuring anything.

    Returns one message per failing module, naming the config path and the
    offending keys. A module without a resolvable model (external plugins,
    modules that take a raw dict) is skipped — this gate can only enforce
    contracts that are declared.
    """
    errors: list[str] = []
    for name, entry in modules.items():
        if not getattr(entry, "enabled", False):
            continue
        model = _config_model_for(name)
        if model is None:
            continue
        try:
            model.model_validate(getattr(entry, "config", {}))
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in exc.errors()
            )
            errors.append(f"modules.{name}.config: {details}")
    return errors


def _config_model_for(name: str) -> type[ModuleConfig] | None:
    """The module's conventional top-level config model, if it declares one."""
    class_name = "".join(part.capitalize() for part in name.split("_")) + "Config"
    try:
        module = importlib.import_module(f"arcagent.modules.{name}.config")
    except ImportError:
        return None
    model = getattr(module, class_name, None)
    if isinstance(model, type) and issubclass(model, ModuleConfig):
        return model
    return None
