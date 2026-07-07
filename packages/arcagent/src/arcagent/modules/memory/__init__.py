"""Thin memory module — wires the config-selected :class:`~arcagent.brain.Brain`.

All memory logic lives in the selected Brain (``arcmemory`` or a BYO plug-in);
this module is pure wiring: capture/recall hooks, one ``memory_search`` tool, and
a consolidation scheduler. See :mod:`arcagent.modules.memory.capabilities`.
"""

from arcagent.modules.memory.config import MemoryConfig

__all__ = ["MemoryConfig"]
