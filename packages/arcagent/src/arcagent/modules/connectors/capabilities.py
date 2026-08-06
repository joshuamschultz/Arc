"""Decorator-form connector module — SPEC-062 COMP-015 (T-901/T-902).

A single ``@capability`` class :class:`Connectors` owns the module's lifecycle.
There is nothing to load yet: attaching manifests, building attachments
(``CliAttachment`` / ``MCPAttachment`` / ``NativeAttachment``), and registering
their tools through the ``CapabilityBridge`` are later SPEC-062 tasks (T-917+) that
land on top of this scaffold. ``setup()``/``teardown()`` only confirm the runtime
was configured, which is what proves ``core/module_discovery.py`` and
``core/agent_lifecycle.py:216-269`` wired this module in correctly before any of
that logic exists.

Runtime state lives in :mod:`arcagent.modules.connectors._runtime`. The agent calls
``_runtime.configure`` once at startup; this capability reads state lazily.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.connectors import _runtime
from arcagent.tools._decorator import capability

_logger = logging.getLogger("arcagent.modules.connectors.capabilities")


@capability(name="connectors")
class Connectors:
    """Lifecycle-bound holder for the connector module's runtime state."""

    async def setup(self, ctx: Any) -> None:
        del ctx  # Loader passes None; state lives in _runtime.
        _runtime.state()  # fails closed if configure() was never called
        _logger.info("Connectors capability started")

    async def teardown(self) -> None:
        _logger.info("Connectors capability stopped")


__all__ = ["Connectors"]
