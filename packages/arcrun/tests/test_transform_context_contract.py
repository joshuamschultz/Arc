"""SPEC-029 D-394 — transform_context append-only contract guard.

The debug-gated assertion catches a per-turn prefix rewrite (the cache-busting
anti-pattern) while allowing pure appends and deliberate compaction (shorter).
"""

import pytest

from arcrun.strategies.react import _check_append_only


def _msgs(*roles: str) -> list[dict]:
    return [{"role": r, "content": r} for r in roles]


def test_identity_is_allowed():
    original = _msgs("system", "user", "assistant")
    _check_append_only(original, list(original))  # no raise


def test_pure_append_is_allowed():
    original = _msgs("system", "user")
    transformed = [*original, {"role": "assistant", "content": "hi"}]
    _check_append_only(original, transformed)  # no raise


def test_compaction_shorter_is_allowed():
    original = _msgs("system", "user", "assistant", "user", "assistant")
    transformed = _msgs("system", "assistant")  # boundary reset — shorter
    _check_append_only(original, transformed)  # no raise


def test_same_length_prefix_mutation_raises():
    original = _msgs("system", "user", "assistant")
    # sliding-window prune: same length, an earlier message rewritten
    transformed = _msgs("system", "user", "assistant")
    transformed[1] = {"role": "user", "content": "[pruned]"}
    with pytest.raises(AssertionError, match="mutated the cached prefix"):
        _check_append_only(original, transformed)


def test_longer_with_mutated_prefix_raises():
    original = _msgs("system", "user")
    transformed = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "[pruned]"},  # prefix changed
        {"role": "assistant", "content": "hi"},  # and appended
    ]
    with pytest.raises(AssertionError, match="mutated the cached prefix"):
        _check_append_only(original, transformed)
