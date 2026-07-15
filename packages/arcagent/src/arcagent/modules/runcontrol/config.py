"""Configuration for the run-control module.

Owned by the run-control module — not part of core config.
Loaded from ``[modules.runcontrol.config]`` in arcagent.toml.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class RuncontrolConfig(ModuleConfig):
    """Run-control module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    # Config-level enable mirrors the module-config convention (tasks et al.);
    # the load gate is ModuleEntry.enabled in the [modules.runcontrol] table.
    enabled: bool = False
    # Forwarded to ``arcstore.config.resolve_data_dir`` — empty string defers to
    # that function's own env > default precedence (SPEC-026 §13.2) so this
    # module, arcui, and the CLI always agree on which SQLite file holds the
    # durable ``cancellations`` directory.
    data_dir: str = ""
    # A pending request that never matches a live run (already-ended run, or a
    # streaming run the cooperative path can't reach) is swept to ``expired`` once
    # it is older than this. Default gives a run ample time to start before its
    # cancel request is aged out.
    stale_ttl_seconds: int = 300
