"""Promotion — decide whether a private memory item may become shared (SPEC-083).

Every part fails closed: an error case keeps the item private. Nothing here
writes to the shared store; that is the bridge's job downstream.
"""

from __future__ import annotations
