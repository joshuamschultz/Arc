"""Promotion — decide whether a private memory item may become shared (SPEC-083).

The pipeline is deliberately split into small, pure parts: a privacy score on the
item (COMP-001), a raise-only hard filter (COMP-003), and a config-banded router
(COMP-004). Every part fails closed — an unscored, unparseable, or error case
keeps the item private. Nothing here writes to the shared store; that is the
bridge's job downstream.
"""

from __future__ import annotations
