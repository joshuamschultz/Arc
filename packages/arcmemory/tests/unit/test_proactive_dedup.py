"""Session-scoped in-window dedup for proactive detected-moment recall (SPEC-071).

COMP-006 / REQ-346: a card that was already injected into a session's prompt
within the last ``window`` turns must not be re-injected. This is a small,
in-memory, per-session sliding-window seen-set — separate from the detector
registry (COMP-004) that will also live in ``arcmemory.detectors`` — so it
gets its own test module.

Pinned API (``arcmemory.detectors.WindowDedup``):

    class WindowDedup:
        def __init__(self, window: int) -> None: ...
        def filter_novel(
            self, session_id: str | None, card_ids: list[str]
        ) -> list[str]:
            '''Return only card ids NOT seen for this session within the last
            ``window`` turns; record the returned ids as seen and advance the
            session's turn counter by one.'''

Turn semantics: each call to ``filter_novel`` for a given ``session_id`` is
one turn for that session. A card recorded on turn N is suppressed on any
call up to and including turn N + window - 1, and is eligible again once the
session has advanced ``window`` turns past the turn it was recorded on.
"""

from __future__ import annotations

from arcmemory.detectors import WindowDedup


def test_filter_novel_first_call_returns_all_cards_as_novel() -> None:
    dedup = WindowDedup(window=3)

    result = dedup.filter_novel("session-1", ["a", "b"])

    assert result == ["a", "b"]


def test_filter_novel_suppresses_card_seen_in_prior_turn_within_window() -> None:
    dedup = WindowDedup(window=3)
    dedup.filter_novel("session-1", ["a", "b"])

    result = dedup.filter_novel("session-1", ["a", "c"])

    assert result == ["c"]


def test_filter_novel_reintroduces_card_after_window_turns_elapse() -> None:
    dedup = WindowDedup(window=2)
    dedup.filter_novel("session-1", ["a"])  # turn 1: records "a"

    # One turn strictly inside the window: "a" is still suppressed.
    within_window = dedup.filter_novel("session-1", ["a"])
    assert within_window == []

    # Advance turns until the window has fully elapsed past the recording turn.
    dedup.filter_novel("session-1", ["b"])
    dedup.filter_novel("session-1", ["c"])

    result = dedup.filter_novel("session-1", ["a"])

    assert result == ["a"]


def test_filter_novel_keeps_independent_seen_sets_per_session() -> None:
    dedup = WindowDedup(window=5)
    dedup.filter_novel("session-1", ["a"])

    # "a" is novel in a different session even though it was just seen in session-1.
    result = dedup.filter_novel("session-2", ["a"])

    assert result == ["a"]


def test_filter_novel_does_not_suppress_across_unrelated_sessions_after_reuse() -> None:
    dedup = WindowDedup(window=5)
    dedup.filter_novel("session-1", ["a"])
    dedup.filter_novel("session-2", ["a"])

    # session-1's seen-set is untouched by session-2's activity.
    result = dedup.filter_novel("session-1", ["a"])

    assert result == []


def test_filter_novel_treats_none_session_id_as_its_own_scope() -> None:
    dedup = WindowDedup(window=3)
    dedup.filter_novel(None, ["a"])

    result_none = dedup.filter_novel(None, ["a"])
    result_named = dedup.filter_novel("session-1", ["a"])

    assert result_none == []
    assert result_named == ["a"]


def test_filter_novel_empty_card_ids_returns_empty_and_records_nothing() -> None:
    dedup = WindowDedup(window=3)

    result = dedup.filter_novel("session-1", [])

    assert result == []
    # A later call with the same id list is unaffected by the empty call.
    assert dedup.filter_novel("session-1", ["a"]) == ["a"]


def test_filter_novel_seen_set_does_not_grow_unbounded_across_many_turns() -> None:
    """Bounded memory: only cards within the trailing ``window`` turns stay
    suppressed — old entries are pruned, not retained forever. We assert this
    purely behaviorally (no reach into internals): a card recorded on turn 1
    must become re-eligible once the session has advanced far past the
    window, even after many distinct cards have cycled through in between
    (which would blow up an unbounded seen-set long before turn 50).
    """
    window = 2
    dedup = WindowDedup(window=window)

    dedup.filter_novel("session-1", ["a"])  # turn 1

    # Push far past the window with distinct cards each turn. Every one of
    # these must also be reported novel — an unbounded/leaky implementation
    # that never forgets would eventually start reporting old cards as seen
    # even though each id here is used exactly once.
    for i in range(50):
        assert dedup.filter_novel("session-1", [f"card-{i}"]) == [f"card-{i}"]

    # "a" must be re-eligible: it is not held forever by an unbounded seen-set.
    result = dedup.filter_novel("session-1", ["a"])
    assert result == ["a"]
