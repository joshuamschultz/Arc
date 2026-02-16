"""Base configuration for all ArcAgent modules.

Provides ``ModuleConfig`` with ``extra="forbid"`` to catch typos
in module configuration keys early. All module configs should
inherit from this base class.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModuleConfig(BaseModel):
    """Base config for ArcAgent modules.

    Uses ``extra="forbid"`` so misspelled config keys raise
    a validation error instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")
