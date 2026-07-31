"""Boundary types every corpus is normalized into (COMP-001 / REQ-174).

A corpus enters the harness as ``Session`` objects of ordered ``Turn`` objects
and leaves the chunker as ``Chunk`` objects. Those three shapes are the entire
vocabulary the chunker, the ingest driver, and the consolidation waiter speak,
which is what lets a new corpus cost exactly one adapter module.

Pydantic validates here rather than downstream (CON-2): a junk date or a
non-list ``turn_ids`` that flowed through untyped would surface much later as a
plausible-looking bad score instead of an error. Pydantic v2's default
(non-strict) mode already rejects every wrong shape this boundary cares about
— ``str`` refuses ``int``, ``list[str]`` refuses a bare ``str``, and ``date``
refuses unparseable prose — so no strictness config is configured on top of it.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class Turn(BaseModel):
    """One utterance in a conversation, addressable by ``turn_id``.

    ``turn_id`` is the key gold-evidence overlap is computed against
    (REQ-181), so it must survive chunking intact.
    """

    turn_id: str
    role: str
    text: str


class Session(BaseModel):
    """One conversation from the source corpus, in the corpus's own order.

    ``turns`` is never reordered: ingest order is what arcmemory's recency
    channel ranks by (REQ-178), so reordering here would silently change what
    the benchmark measures. ``source_date`` is stamped onto every chunk of the
    session by the chunker.
    """

    conversation_id: str
    source_date: date
    turns: list[Turn]


class Chunk(BaseModel):
    """One session fragment fed to the agent as a single ingest turn.

    ``(session_idx, chunk_idx)`` is the chunk's ingest-order coordinate and
    also names its session key (``ingest:<session_idx>:<chunk_idx>``, REQ-179).
    ``turn_ids`` records which turns the text came from, so a sanitize-fidelity
    verdict can be attributed back to gold evidence (REQ-181).
    """

    text: str
    session_idx: int
    chunk_idx: int
    turn_ids: list[str]
