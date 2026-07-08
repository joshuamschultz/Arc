"""Session module — JSONL store wrapper + SQLite FTS5 indexer + session_search tool.

Architecture (SDD §3.2):
  - store.py    : thin helpers for reading JSONL files without duplicating
                  core.session_manager logic
  - index.py    : SessionIndex — polling indexer (crash-safe, byte-offset checkpoint)
  - capabilities.py : SessionIndexCapability lifecycle + session_search @tool
  - identity_graph.py : IdentityGraph — cross-platform user identity resolution

The live surface is discovered by the SPEC-021 capability scan over
capabilities.py; this package init only re-exports the shared types other
modules import.
"""

from __future__ import annotations

from arcagent.modules.session.identity_graph import IdentityGraph, Link
from arcagent.modules.session.index import SearchHit, SessionIndex
from arcagent.modules.session.store import read_messages_from_offset

__all__ = [
    "IdentityGraph",
    "Link",
    "SearchHit",
    "SessionIndex",
    "read_messages_from_offset",
]
