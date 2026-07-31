"""QueryRunner (COMP-009) — put the question, anchored to the dataset's date.

Ingest is over; this is the read side. Three properties carry the design:

*A fresh session key.* The question goes to the SAME agent instance — that
agent's arcmemory store is the system under test — but on a session key outside
the ``ingest:<s>:<c>`` space (REQ-187). Reusing an ingest key would replay that
chunk's transcript into the question's prompt, and the answer would then be
scored against text the model could read rather than text it had to recall.

*The date comes from the dataset, never the wall clock* (REQ-214). Arc's system
prompt carries no date at all — ``assemble_system_prompt`` builds from
``identity.md``, ``context.md`` and module-injected sections only — so
``[Current date: YYYY-MM-DD]`` on the question turn is the only anchor a
relative reference like "last month" has. Stamping today would anchor the
reader to the run year instead of the haystack's, failing the same 133
temporal-reasoning questions a different way. This module therefore reads no
clock anywhere, and a test asserts that by patching one.

Symmetric with the write side (REQ-178): the session date rides in the chunk
text, the question date rides in the query turn. Both are harness-side text
prefixes with zero framework change.

*The answer is recorded verbatim* and the date that produced it travels with it
(REQ-215). ``question_date_source`` says whether the anchor came from the
dataset's own field or from the derived latest-haystack fallback, so a silently
absent dataset field cannot masquerade as a memory failure in the
temporal-reasoning numbers.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel

from evaluations.ingest.types import Chunk

if TYPE_CHECKING:
    from evaluations.longmemeval.adapter import QuestionMeta

QUERY_SESSION_KEY = "query"
"""The read-side session key — deliberately outside the ``ingest:`` namespace."""

DATE_PREFIX = "[Current date: {stamp}]"
"""The read-side anchor, mirroring the chunker's ``[Session date: ...]``."""


class QueryTarget(Protocol):
    """The slice of ``ArcAgent`` the query touches.

    A protocol rather than the concrete class, matching ``IngestTarget``: one
    method is all the read side needs, and it is what lets the tests drive a
    real query turn without a provider, a network call or a workspace.
    """

    async def run_collected(self, input_text: str, *, session_key: str) -> Any: ...


class Answer(BaseModel):
    """One question's answer, with the anchor that produced it.

    ``text`` is the agent's response as returned, unedited — scrubbing happens
    once, later, at the ledger boundary (COMP-015), so the judge grades what the
    agent actually said. The two date fields are carried onto the result row
    rather than recomputed there, because the row has to stay self-describing
    after rows from several runs are concatenated.
    """

    text: str
    question_date_used: date
    question_date_source: Literal["dataset", "derived"]
    recalled_chunk_ids: list[str]


def build_query_turn(meta: QuestionMeta) -> str:
    """Render the question turn: the dataset's date, then the question.

    ``meta.question_date`` is already resolved and sourced by the adapter
    (REQ-215), which raises rather than substitute a clock, so there is no
    fallback to make here — the date is either the dataset's or the question
    was voided before this point.
    """
    return f"{DATE_PREFIX.format(stamp=meta.question_date.isoformat())}\n{meta.question}"


class QueryRunner:
    """Put one question to the agent that just ingested its haystack."""

    async def ask(self, agent: QueryTarget, meta: QuestionMeta, chunks: Sequence[Chunk]) -> Answer:
        """Ask the question on a fresh session and record what came back."""
        input_text = build_query_turn(meta)
        result = await agent.run_collected(input_text, session_key=QUERY_SESSION_KEY)
        return Answer(
            text=result.content,
            question_date_used=meta.question_date,
            question_date_source=meta.question_date_source,
            recalled_chunk_ids=attribute_recalled_chunks(observe_recall(input_text), chunks),
        )


def observe_recall(query_text: str) -> str:
    """The recall arcmemory injected into this exact turn's prompt.

    Read from the memory module's per-turn recall cache — the same private
    runtime state COMP-020's preflight reads — because no public API reports
    what a turn recalled and the SDD forbids a framework change. Keyed by the
    turn's own text, so it can only ever return this question's recall, and a
    miss is the same as an empty recall: nothing was injected.

    Raises ``MemoryIsolationError`` when no agent DID is bound to the running
    task. That is loud on purpose: a harness that silently reported "recalled
    nothing" because the state read failed would publish a retrieval number of
    zero and call it a memory result.
    """
    from arcagent.modules.memory import _runtime

    return _runtime.state().recall_cache.get(hash(query_text), "")


def attribute_recalled_chunks(recall_text: str, chunks: Sequence[Chunk]) -> list[str]:
    """The ingest chunks whose text this recall actually carried.

    Surface recall returns the captured event content, which is the chunk text
    as fed, so containment after NFKC folding is an exact attribution rather
    than a similarity guess. Consolidated facts are LLM-derived text that
    matches no chunk and is deliberately not attributed to one: recalling a
    distilled fact is not evidence that the turn it came from was retrieved.
    """
    if not recall_text:
        return []
    haystack = _fold(recall_text)
    return [
        f"{chunk.session_idx}:{chunk.chunk_idx}"
        for chunk in chunks
        if _fold(chunk.text) in haystack
    ]


def _fold(text: str) -> str:
    """NFKC-normalize and collapse whitespace, matching what capture stored."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


__all__ = [
    "DATE_PREFIX",
    "QUERY_SESSION_KEY",
    "Answer",
    "QueryRunner",
    "QueryTarget",
    "attribute_recalled_chunks",
    "build_query_turn",
    "observe_recall",
]
