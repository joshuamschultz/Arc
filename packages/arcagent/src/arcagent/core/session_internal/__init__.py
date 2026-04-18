"""Internal session & context management — extracted from core in SPEC-017 /review.

Public surface:
  * ``SessionManager`` — per-agent session lifecycle (messages, compaction)
  * ``ContextManager`` — prompt assembly + context-window management

The module is named ``session_internal`` rather than ``session`` to
avoid collision with ``arcagent.modules.session`` (a persistence
module). Outer callers are expected to import through this package:

    from arcagent.core.session_internal import SessionManager, ContextManager

The subpackage keeps core/ under its LOC budget (CLAUDE.md) while
preserving a single import path for consumers.
"""

from __future__ import annotations

from arcagent.core.session_internal.context import ContextManager
from arcagent.core.session_internal.manager import SessionManager

__all__ = ["ContextManager", "SessionManager"]
