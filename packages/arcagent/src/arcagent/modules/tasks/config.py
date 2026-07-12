"""Configuration for the tasks module.

Owned by the tasks module — not part of core config.
Loaded from ``[modules.tasks.config]`` in arcagent.toml.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class TasksConfig(ModuleConfig):
    """Tasks module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    enabled: bool = False
    # Forwarded to ``arcstore.config.resolve_data_dir`` — empty string defers
    # to that function's own env > default precedence (SPEC-026 §13.2) so
    # this module and arcui always agree on which SQLite file is the durable
    # Task directory.
    data_dir: str = ""
    # NATS JetStream url for the shared arcteam registry (mirrors
    # MessagingConfig.nats_url). Empty means no live registry is built —
    # assign_task/create_task's @handle resolution degrades with a clear
    # error instead of silently building a useless, disconnected registry.
    nats_url: str = ""
