"""Session module configuration.

The session index poll cadence is the only tunable; ``extra='forbid'`` (via
:class:`ModuleConfig`) turns a misspelled key into a loud validation error.
"""

from __future__ import annotations

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


class SessionConfig(ModuleConfig):
    """Configuration for the session module."""

    enabled: bool = True
    # Seconds between JSONL polls in the session index watcher.
    poll_interval: float = Field(default=30.0, gt=0)
