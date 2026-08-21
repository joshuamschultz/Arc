"""RED — mid-loop recall injection via transform_context (SPEC-072 COMP-003).

A decision-point recall reaches the model BETWEEN loop steps by riding arcrun's existing
append-only ``transform_context`` hook: arcagent stages a block on a per-run, DID-keyed
buffer (``arcagent.core.midloop_recall``) and ``ContextManager.transform_context`` appends
it to the message tail before the next model call — prefix unchanged (append-only holds,
ARCRUN_ASSERT_APPEND_ONLY), empty when nothing staged, drained once. arcrun stays unaware
(it only calls the callback); arcmemory is never imported here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from arcagent.core import midloop_recall
from arcagent.core.config import ContextConfig
from arcagent.core.session_internal.context import ContextManager

_DID = "did:arc:midloop-agent"


def _cm(did: str = _DID) -> ContextManager:
    cfg = ContextConfig(
        max_tokens=100_000,
        prune_threshold=0.70,
        compact_threshold=0.85,
        emergency_threshold=0.95,
        estimate_multiplier=1.1,
    )
    return ContextManager(config=cfg, telemetry=MagicMock(), agent_did=did)


def _clear() -> None:
    midloop_recall.drain(_DID)
    midloop_recall.drain("did:arc:other")


def test_transform_context_appends_staged_block_append_only() -> None:
    _clear()
    cm = _cm()
    messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

    midloop_recall.stage(_DID, "<memory-result>decision recall</memory-result>")
    out = cm.transform_context(list(messages))

    assert out[: len(messages)] == messages  # append-only: cached prefix unchanged
    assert len(out) == len(messages) + 1
    assert "decision recall" in str(out[-1])


def test_transform_context_is_noop_when_nothing_staged() -> None:
    _clear()
    cm = _cm()
    messages = [{"role": "user", "content": "hello"}]
    assert cm.transform_context(list(messages)) == messages


def test_staged_block_is_drained_once() -> None:
    _clear()
    cm = _cm()
    messages = [{"role": "user", "content": "hello"}]

    midloop_recall.stage(_DID, "<memory-result>once</memory-result>")
    first = cm.transform_context(list(messages))
    assert len(first) == len(messages) + 1

    second = cm.transform_context(list(messages))
    assert second == messages  # already drained — never re-appended


def test_stage_ignores_empty_and_isolates_by_did() -> None:
    _clear()
    midloop_recall.stage(_DID, "")  # empty is ignored
    assert midloop_recall.drain(_DID) == []

    midloop_recall.stage("did:arc:other", "block-x")
    assert midloop_recall.drain(_DID) == []  # another agent's buffer never leaks here
    assert midloop_recall.drain("did:arc:other") == ["block-x"]
