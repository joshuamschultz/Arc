"""Deterministic detected-moment gate + in-window dedup (SPEC-071, COMP-004/006).

Two independent, model-free pieces used by ``ArcMemoryBrain.on_moment`` to decide
*whether* a proactive recall should fire and *which cards* it may inject:

* the **detector registry** (:data:`DETECTORS` + :func:`evaluate_moment`) — one
  deterministic detector per ``kind``, so a decision made on every user turn /
  task start / plan step is cheap and reproducible. No embedder, no LLM ever
  touches this path; that is a hard non-negotiable pinned by the store/embedder
  poison-object tests.
* :class:`WindowDedup` — a per-session sliding-window seen-set that stops the
  same card being re-injected within ``window`` turns.

Both stay flat and side-effect-light on purpose: they run in the hot path.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from arcmemory.types import Entity

# Jaccard similarity below this counts as a topic shift. 2-of-3 shared cues
# (~0.67) is "same topic"; disjoint cue sets (0.0) always trip it.
_TOPIC_SHIFT_THRESHOLD = 0.5

_WORD_RE = re.compile(r"[a-z0-9]+")

# The handful of high-frequency function words the working-set salience filter drops
# so generic glue never crowds out a proper-noun/entity cue. Deliberately tiny and
# deterministic — the bound + decay do the real work; this only skims obvious noise.
_STOPWORDS: frozenset[str] = frozenset(
    {"the", "and", "for", "that", "this", "with", "you", "are", "was", "but", "not", "its"}
)


def _is_salient(cue: str) -> bool:
    """A cue worth keeping in the working set: not a trivial/too-short glue word."""
    norm = cue.strip().lower()
    return len(norm) >= 3 and norm not in _STOPWORDS


@dataclass(frozen=True)
class Decision:
    """A detector's verdict: whether to recall, and the cues to query with.

    Frozen because it is a value carried across the arcmemory/arcagent seam —
    nothing downstream may mutate a decision after the detector returns it.
    """

    fire: bool
    query_cues: list[str]


class SessionStateLike(Protocol):
    """Duck type a detector needs from session state: the prior turn's cues."""

    prior_cues: list[str]


class EntityStoreLike(Protocol):
    """Duck type a detector needs from the semantic store: enumerate + read.

    Deliberately the whole contract surface — ``entity_seen`` reaches for
    nothing else, so no embedder-backed store method can be pulled onto the
    deterministic path.
    """

    def slugs(self) -> list[str]: ...

    def read(self, slug: str) -> Entity | None: ...


DetectorFn = Callable[[list[str], str, SessionStateLike, EntityStoreLike], Decision]


def _norm(term: str) -> str:
    """Fold a cue/name to a case-insensitive, whitespace-trimmed key."""
    return term.strip().lower()


def _cues_from_text(text: str) -> list[str]:
    """Derive deterministic query cues from free text (dedup, order-preserving).

    Same text always yields the same cues — nothing here samples a model.
    """
    ordered: dict[str, None] = {}
    for token in _WORD_RE.findall(text.lower()):
        ordered.setdefault(token, None)
    return list(ordered)


def _fire_on_presence(cues: list[str], text: str) -> Decision:
    """Shared presence rule for task_start / decision_point.

    Fires when any signal is present; echoes explicit cues, else derives them
    from text. Not a kind switch — both detectors delegate here without any
    branch on their own kind.
    """
    if cues:
        return Decision(fire=True, query_cues=list(cues))
    stripped = text.strip()
    if stripped:
        return Decision(fire=True, query_cues=_cues_from_text(text) or [stripped])
    return Decision(fire=False, query_cues=[])


def _task_start(
    cues: list[str], text: str, session_state: SessionStateLike, store: EntityStoreLike
) -> Decision:
    """A task run began — recall context for what the agent is about to do."""
    return _fire_on_presence(cues, text)


def _decision_point(
    cues: list[str], text: str, session_state: SessionStateLike, store: EntityStoreLike
) -> Decision:
    """The agent is about to choose — recall prior decisions on what is in play.

    Explicit cues/text (e.g. a pre_tool moment's tool + args) drive the query directly.
    A cue-less pre_plan moment falls back to the per-session working set (SPEC-072
    COMP-001/004), so it recalls on the entities in play at this plan step even though
    the turn event itself carries no situation text. Empty on both → no fire.
    """
    if cues or text.strip():
        return _fire_on_presence(cues, text)
    working_set = list(getattr(session_state, "working_set", []) or [])
    if working_set:
        return Decision(fire=True, query_cues=list(working_set))
    return Decision(fire=False, query_cues=[])


def _expand_terms(value: str) -> set[str]:
    """A name/alias/tag plus its word tokens.

    Entity names are routinely multi-word (``"Nebula 0"``, ``"Auth Service"``) while
    a turn's cue is one salient word (``"nebula"``). Matching only the whole
    normalized string would then never fire on a named entity, so a term also
    contributes each of its word tokens as a match key.
    """
    terms = {_norm(value)}
    terms.update(_WORD_RE.findall(value.lower()))
    return terms


def _known_entity_terms(store: EntityStoreLike) -> set[str]:
    """Every normalized name/alias/tag — and each of their word tokens."""
    terms: set[str] = set()
    for slug in store.slugs():
        entity = store.read(slug)
        if entity is None:
            continue
        for value in (entity.name, *entity.aliases, *entity.tags):
            terms |= _expand_terms(value)
    return terms


def _entity_seen(
    cues: list[str], text: str, session_state: SessionStateLike, store: EntityStoreLike
) -> Decision:
    """A known entity is in play — recall what we already know about it.

    "In play" spans the current cues AND the per-session working set (SPEC-072
    COMP-001), so an entity named a turn ago still fires this detector even when the
    latest message omits it. The working set is read defensively — a session_state
    that predates it (only ``prior_cues``) simply contributes none.
    """
    working_set = list(getattr(session_state, "working_set", []) or [])
    pool = list(dict.fromkeys([*cues, *working_set]))
    if not pool:
        return Decision(fire=False, query_cues=[])
    known = _known_entity_terms(store)
    if any(_norm(cue) in known for cue in pool):
        return Decision(fire=True, query_cues=pool)
    return Decision(fire=False, query_cues=[])


def _topic_shift(
    cues: list[str], text: str, session_state: SessionStateLike, store: EntityStoreLike
) -> Decision:
    """The conversation turned — recall context for the new topic.

    No baseline (no prior cues) is not a shift, so a session's first turn never
    fires here.
    """
    prior = session_state.prior_cues
    if not prior or not cues:
        return Decision(fire=False, query_cues=[])
    prior_set = {_norm(cue) for cue in prior}
    current_set = {_norm(cue) for cue in cues}
    overlap = len(prior_set & current_set) / len(prior_set | current_set)
    if overlap < _TOPIC_SHIFT_THRESHOLD:
        return Decision(fire=True, query_cues=list(cues))
    return Decision(fire=False, query_cues=[])


# One detector per moment kind. A dict, not an if/elif chain, so adding a kind is
# a single registry line and `evaluate_moment` never grows a branch per kind.
DETECTORS: dict[str, DetectorFn] = {
    "task_start": _task_start,
    "entity_seen": _entity_seen,
    "topic_shift": _topic_shift,
    "decision_point": _decision_point,
}


def evaluate_moment(
    kind: str,
    *,
    cues: list[str],
    text: str,
    session_state: SessionStateLike,
    store: EntityStoreLike,
) -> Decision:
    """Route a detected moment to its detector; unknown kind fails open (no fire).

    Fail-open (return, never raise) keeps a bad ``kind`` from ever blocking the
    agent's turn — the worst case is a missed proactive recall, not a crash.
    """
    detector = DETECTORS.get(kind)
    if detector is None:
        return Decision(fire=False, query_cues=[])
    return detector(cues, text, session_state, store)


class WindowDedup:
    """Per-session sliding-window seen-set for proactive card injection.

    Each :meth:`filter_novel` call is one turn for its session. A card recorded
    on turn N is suppressed through turn ``N + window - 1`` and eligible again at
    turn ``N + window``. State is pruned every turn so memory stays bounded to
    the trailing window regardless of how many distinct cards cycle through.
    """

    def __init__(self, window: int) -> None:
        self._window = window
        self._turn: dict[str | None, int] = {}
        self._seen: dict[str | None, dict[str, int]] = {}

    def filter_novel(self, session_id: str | None, card_ids: list[str]) -> list[str]:
        """Return card ids not seen for this session within the last ``window`` turns.

        Records the returned ids as seen on this turn and advances the session's
        turn counter by one.
        """
        turn = self._turn.get(session_id, 0) + 1
        seen = self._seen.setdefault(session_id, {})
        novel = [cid for cid in card_ids if cid not in seen or turn - seen[cid] >= self._window]
        for cid in novel:
            seen[cid] = turn
        self._prune(seen, turn)
        self._turn[session_id] = turn
        return novel

    def _prune(self, seen: dict[str, int], turn: int) -> None:
        """Drop entries whose window has fully elapsed — keeps memory bounded."""
        stale = [cid for cid, last in seen.items() if turn - last >= self._window]
        for cid in stale:
            del seen[cid]


class WorkingSet:
    """Bounded, decaying, salience-filtered per-session accumulator of cues in play.

    Generalizes the SPEC-071 per-session prior-cue baseline (SPEC-072 COMP-001): each
    :meth:`update` is one turn for its session, merging that turn's salient cues into
    the set so a later detector can key off an entity named turns ago (REQ-349/350). A
    cue not refreshed within ``decay_turns`` turns is dropped, and the set is capped at
    ``max_size`` (newest kept) — so it never grows unbounded (REQ-350, Scalability).
    Pure and model-free: no embedder, no LLM ever touches this hot path (REQ-361).
    """

    def __init__(self, max_size: int, decay_turns: int) -> None:
        self._max = max(1, max_size)
        self._decay = max(1, decay_turns)
        self._turn: dict[str | None, int] = {}
        self._members: dict[str | None, dict[str, int]] = {}

    def update(self, session_id: str | None, cues: list[str]) -> list[str]:
        """Merge this turn's salient cues; decay + bound; return the members (recent first)."""
        turn = self._turn.get(session_id, 0) + 1
        members = self._members.setdefault(session_id, {})
        for cue in cues:
            if _is_salient(cue):
                members[cue.strip().lower()] = turn
        stale = [cue for cue, last in members.items() if turn - last >= self._decay]
        for cue in stale:
            del members[cue]
        if len(members) > self._max:
            kept = dict(self._by_recency(members)[: self._max])
            members.clear()
            members.update(kept)
        self._turn[session_id] = turn
        return [cue for cue, _ in self._by_recency(members)]

    @staticmethod
    def _by_recency(members: dict[str, int]) -> list[tuple[str, int]]:
        """Members ordered newest-first, ties broken by cue for determinism."""
        return sorted(members.items(), key=lambda kv: (-kv[1], kv[0]))


__all__ = [
    "DETECTORS",
    "Decision",
    "DetectorFn",
    "EntityStoreLike",
    "SessionStateLike",
    "WindowDedup",
    "WorkingSet",
    "evaluate_moment",
]
