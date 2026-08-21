"""Contract test for ``Brain.on_moment`` (SPEC-071 T-974).

``on_moment`` is the detected-moment proactive-recall seam: arcagent's loop
calls it at emission sites (task start, pre-plan, pre-respond) with a
``kind`` and primitive cues/text, and gets back injectable text (or ``""``
when nothing fires). This test locks the exact signature — primitives only,
kw-only after ``kind`` — and proves ``NullBrain`` satisfies it as a no-op.
"""

from __future__ import annotations

import inspect

from arcagent.brain import Brain, NullBrain


def test_brain_declares_on_moment_with_documented_signature() -> None:
    """``Brain.on_moment`` exists with the orchestrator-fixed primitive signature."""
    assert hasattr(Brain, "on_moment"), "Brain Protocol must declare on_moment"

    sig = inspect.signature(Brain.on_moment)
    params = sig.parameters

    # kind: required, positional-or-keyword, no default.
    assert "kind" in params
    assert params["kind"].default is inspect.Parameter.empty

    # Everything after kind is keyword-only with the documented defaults.
    kw_only = {"cues", "text", "clearance", "top_k", "budget", "session_id"}
    for name in kw_only:
        assert name in params, f"on_moment must declare kw-only param {name!r}"
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name!r} must be keyword-only"
        )

    assert params["cues"].default is None
    assert params["text"].default == ""
    assert params["clearance"].default == "unclassified"
    assert params["top_k"].default == 3
    assert params["budget"].default == 512
    assert params["session_id"].default is None

    # Return annotation is bare str (primitive, injectable text).
    assert sig.return_annotation is str


async def test_null_brain_on_moment_is_a_noop_returning_empty_string() -> None:
    """``NullBrain().on_moment(...)`` never fires — always returns ``""``."""
    brain = NullBrain()

    assert await brain.on_moment("task_start") == ""
    assert await brain.on_moment("entity_seen", cues=["foo"], text="bar") == ""
    assert await brain.on_moment("topic_shift", session_id="sess-1") == ""
    assert await brain.on_moment("decision_point", top_k=1, budget=10) == ""
    # Even an undocumented/unknown kind must degrade to a no-op, not raise.
    assert await brain.on_moment("something_unknown") == ""


def test_null_brain_structurally_satisfies_brain_with_on_moment() -> None:
    """``NullBrain`` is (still) a structural ``Brain`` once ``on_moment`` is required."""
    assert hasattr(NullBrain, "on_moment"), "NullBrain must implement on_moment"
    assert isinstance(NullBrain(), Brain), (
        "NullBrain must structurally satisfy the runtime_checkable Brain Protocol"
    )
