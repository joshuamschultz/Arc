"""A provider that wraps its JSON must not read as an empty answer.

Structured-output modes sometimes return the object inside a single-key envelope —
``{"$parameter": {...}}``, ``{"output": {...}}`` — rather than at the top level.
Every distiller call reads its own key straight off the parsed dict, so a wrapped
response silently became "no facts", "no insights", "merge nothing": a total
failure that is indistinguishable from a considered negative answer, and it was
observed live on both the merge-confirm and step-consolidation calls.

Unwrapping is deliberately narrow — exactly one key, whose value is a dict, and
only when the expected key is not already present — so a legitimate single-key
result is never mistaken for an envelope.
"""

from __future__ import annotations

from arcmemory.arcllm_seam import unwrap_envelope


def test_a_wrapped_object_is_unwrapped() -> None:
    """The live shape: the answer sits one level down under a provider key."""
    assert unwrap_envelope({"$parameter": {"steps": [1]}}, "steps") == {"steps": [1]}
    assert unwrap_envelope({"output": {"merge": []}}, "merge") == {"merge": []}


def test_a_top_level_answer_is_left_alone() -> None:
    """The ordinary case must not be touched."""
    payload = {"steps": [1], "note": "x"}
    assert unwrap_envelope(payload, "steps") is payload


def test_a_single_key_result_that_is_the_answer_is_not_unwrapped() -> None:
    """``{"merge": [...]}`` is one key AND the expected key — an answer, not an envelope."""
    payload = {"merge": [["a", "b"]]}
    assert unwrap_envelope(payload, "merge") is payload


def test_a_multi_key_object_is_never_unwrapped() -> None:
    """Two keys is a result shape, whatever it contains."""
    payload = {"a": {"steps": []}, "b": {}}
    assert unwrap_envelope(payload, "steps") is payload


def test_an_envelope_around_a_non_dict_is_left_alone() -> None:
    """``{"steps": [...]}`` under an unexpected key is not an envelope to open."""
    payload = {"anything": [1, 2, 3]}
    assert unwrap_envelope(payload, "steps") is payload
