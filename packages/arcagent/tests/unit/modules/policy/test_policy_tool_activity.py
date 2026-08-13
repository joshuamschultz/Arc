"""The policy Reflector must see the RUN, not just the chat.

``agent:post_respond`` carries exactly two entries — the user's raw text and the
assistant's final text — so the Reflector that writes policy bullets about
tool-calling behavior was blind to every tool call. The policy module now keeps
its OWN record of tool activity off the ``agent:pre_tool`` / ``agent:post_tool``
bus events. This file locks:

  1. The buffer pairs a call with its result, survives a missing pre/post half,
     bounds entry count, and truncates oversized args/results *visibly*.
  2. What the engine actually receives contains tool name, arguments, result.
  3. The ``agent:post_respond`` payload (the memory-capture channel) is
     UNCHANGED — conversation only, never polluted with tool plumbing.
  4. The buffer is cleared once evaluated, and persists across a restart.
  5. With no tool ever called, everything degrades to the old behavior.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from arcagent.modules.policy import _runtime
from arcagent.modules.policy._tool_activity import (
    MAX_ARGS_CHARS,
    MAX_ENTRIES,
    MAX_RESULT_CHARS,
    ToolActivity,
)
from arcagent.utils.io import format_messages


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _runtime.configure(workspace=ws, agent_name="t", config={"eval_interval_turns": 1})
    _runtime.state().eval_model = AsyncMock()
    return ws


def _turn_data() -> dict[str, Any]:
    return {
        "result": None,
        "messages": [
            {"role": "user", "content": "find the runtime docs"},
            {"role": "assistant", "content": "here they are"},
        ],
        "session_id": "sess-1",
        "automated": False,
    }


async def _call_tool(name: str, args: dict[str, Any], result: str) -> None:
    """Drive the two tool hooks the way ``tool_registry`` emits them."""
    from arcagent.modules.policy import capabilities as policy_caps

    await policy_caps.record_tool_call(SimpleNamespace(data={"tool": name, "args": args}))
    await policy_caps.record_tool_result(
        SimpleNamespace(data={"tool": name, "result": result, "duration": 0.1})
    )


async def _run_turn(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ``periodic_policy_eval`` turn; return what the engine received."""
    from arcagent.modules.policy import capabilities as policy_caps

    captured: list[Any] = []

    def _capture(coro: Any, **_: Any) -> None:
        captured.append(coro)

    with patch.object(policy_caps, "spawn_background", side_effect=_capture):
        await policy_caps.periodic_policy_eval(SimpleNamespace(data=data))

    assert captured, "policy eval did not fire"
    with patch.object(
        _runtime.state().engine, "evaluate", new_callable=AsyncMock
    ) as mock_evaluate:
        await captured[0]
    sent: list[dict[str, Any]] = mock_evaluate.call_args.args[0]
    return sent


class TestToolActivityBuffer:
    def test_pairs_call_with_its_result(self) -> None:
        activity = ToolActivity()
        activity.record_call("web_search", {"query": "arc"})
        activity.record_result("web_search", "three hits")
        rendered = format_messages(activity.as_messages(), limit=0)
        assert "web_search" in rendered
        assert '"query": "arc"' in rendered
        assert "three hits" in rendered

    def test_result_without_a_recorded_call_is_still_kept(self) -> None:
        activity = ToolActivity()
        activity.record_result("bash", "exit 0")
        rendered = format_messages(activity.as_messages(), limit=0)
        assert "bash" in rendered
        assert "exit 0" in rendered

    def test_unfinished_call_reads_as_a_failure(self) -> None:
        """No ``post_tool`` means the call raised/timed out/was vetoed — say so."""
        activity = ToolActivity()
        activity.record_call("bash", {"cmd": "sleep 999"})
        rendered = format_messages(activity.as_messages(), limit=0)
        assert "did not complete" in rendered

    def test_concurrent_calls_pair_to_distinct_entries(self) -> None:
        activity = ToolActivity()
        activity.record_call("read", {"path": "a"})
        activity.record_call("read", {"path": "b"})
        activity.record_result("read", "content-B")
        activity.record_result("read", "content-A")
        rendered = format_messages(activity.as_messages(), limit=0)
        assert "content-A" in rendered
        assert "content-B" in rendered
        assert len(activity.as_messages()) == 2

    def test_entry_count_is_bounded(self) -> None:
        activity = ToolActivity()
        for i in range(MAX_ENTRIES + 25):
            activity.record_call("t", {"i": i})
            activity.record_result("t", f"r{i}")
        assert len(activity.as_messages()) == MAX_ENTRIES
        rendered = format_messages(activity.as_messages(), limit=0)
        assert "r0" not in rendered  # oldest evicted
        assert f"r{MAX_ENTRIES + 24}" in rendered  # newest kept

    def test_huge_result_is_truncated_and_says_so(self) -> None:
        activity = ToolActivity()
        activity.record_call("read", {"path": "big"})
        activity.record_result("read", "x" * 50_000)
        rendered = format_messages(activity.as_messages(), limit=0)
        assert len(rendered) < MAX_RESULT_CHARS + 500
        assert "truncated" in rendered
        assert "50000" in rendered

    def test_huge_args_are_truncated_and_say_so(self) -> None:
        activity = ToolActivity()
        activity.record_call("write", {"content": "y" * 50_000})
        activity.record_result("write", "ok")
        rendered = format_messages(activity.as_messages(), limit=0)
        assert len(rendered) < MAX_ARGS_CHARS + 500
        assert "truncated" in rendered

    def test_unserializable_args_do_not_raise(self) -> None:
        activity = ToolActivity()
        activity.record_call("odd", {"obj": object()})
        activity.record_result("odd", "ok")
        assert activity.as_messages()

    def test_clear_empties_the_buffer(self) -> None:
        activity = ToolActivity()
        activity.record_call("t", {})
        activity.clear()
        assert activity.as_messages() == []


@pytest.mark.asyncio
class TestReflectorSeesTheRun:
    async def test_engine_receives_tool_name_args_and_result(self, configured: Path) -> None:
        await _call_tool("web_search", {"query": "arc runtime"}, "RESULT-SENTINEL")
        sent = await _run_turn(_turn_data())

        rendered = format_messages(sent, limit=0)
        assert "web_search" in rendered
        assert '"query": "arc runtime"' in rendered
        assert "RESULT-SENTINEL" in rendered
        # The conversation is still there, and the reply stays last.
        assert "find the runtime docs" in rendered
        assert sent[-1]["content"] == "here they are"

    async def test_tool_records_sit_between_request_and_reply(self, configured: Path) -> None:
        await _call_tool("read", {"path": "x"}, "file body")
        sent = await _run_turn(_turn_data())
        roles = [m["role"] for m in sent]
        assert roles == ["user", "tool", "assistant"]

    async def test_no_tool_calls_leaves_the_transcript_untouched(self, configured: Path) -> None:
        data = _turn_data()
        sent = await _run_turn(data)
        assert sent == data["messages"]

    async def test_terminal_eval_flushes_unevaluated_tool_activity(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        st = _runtime.state()
        st.session_messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "done"},
        ]
        await _call_tool("bash", {"cmd": "ls"}, "TERMINAL-SENTINEL")

        with patch.object(st.engine, "evaluate", new_callable=AsyncMock) as mock_evaluate:
            await policy_caps.terminal_policy_eval(SimpleNamespace(data={"session_id": "s"}))
        rendered = format_messages(mock_evaluate.call_args.args[0], limit=0)
        assert "TERMINAL-SENTINEL" in rendered
        assert st.tool_activity.as_messages() == []


@pytest.mark.asyncio
class TestMemoryChannelUnchanged:
    """``agent:post_respond`` feeds memory distillation — it must stay conversation-only."""

    async def test_post_respond_payload_is_not_mutated(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        await _call_tool("web_search", {"query": "arc runtime"}, "RESULT-SENTINEL")
        data = _turn_data()
        before = copy.deepcopy(data)

        def _close(coro: Any, **_: Any) -> None:
            coro.close()

        with patch.object(policy_caps, "spawn_background", side_effect=_close):
            await policy_caps.periodic_policy_eval(SimpleNamespace(data=data))

        assert data == before

    async def test_memory_capture_still_sees_conversation_only(self, configured: Path) -> None:
        """What ``capture_respond`` would distill contains no tool plumbing."""
        from arcagent.modules.policy import capabilities as policy_caps

        await _call_tool("web_search", {"query": "arc runtime"}, "RESULT-SENTINEL")
        data = _turn_data()

        def _close(coro: Any, **_: Any) -> None:
            coro.close()

        with patch.object(policy_caps, "spawn_background", side_effect=_close):
            await policy_caps.periodic_policy_eval(SimpleNamespace(data=data))

        captured_text = "\n".join(str(m["content"]) for m in data["messages"])
        assert "RESULT-SENTINEL" not in captured_text
        assert "web_search" not in captured_text
        assert [m["role"] for m in data["messages"]] == ["user", "assistant"]


@pytest.mark.asyncio
class TestBufferLifecycle:
    async def test_buffer_cleared_after_eval(self, configured: Path) -> None:
        await _call_tool("web_search", {"query": "arc"}, "hits")
        assert _runtime.state().tool_activity.as_messages()
        await _run_turn(_turn_data())
        assert _runtime.state().tool_activity.as_messages() == []

    async def test_buffer_survives_below_the_cadence(self, tmp_path: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(workspace=ws, agent_name="t", config={"eval_interval_turns": 100})
        _runtime.state().eval_model = AsyncMock()

        await _call_tool("web_search", {"query": "arc"}, "hits")
        await policy_caps.periodic_policy_eval(SimpleNamespace(data=_turn_data()))
        assert _runtime.state().tool_activity.as_messages()  # not yet evaluated

    async def test_buffer_survives_a_restart(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(workspace=ws, agent_name="t", config={"eval_interval_turns": 100})
        await _call_tool("web_search", {"query": "arc"}, "PERSISTED-SENTINEL")
        _runtime.state().persist()

        _runtime.reset()
        _runtime.configure(workspace=ws, agent_name="t", config={"eval_interval_turns": 100})
        rendered = format_messages(_runtime.state().tool_activity.as_messages(), limit=0)
        assert "PERSISTED-SENTINEL" in rendered

    async def test_corrupt_persisted_activity_degrades_to_empty(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".policy-state.json").write_text('{"tool_activity": "nonsense"}', encoding="utf-8")
        _runtime.configure(workspace=ws, agent_name="t")
        assert _runtime.state().tool_activity.as_messages() == []


@pytest.mark.asyncio
class TestToolHookRegistration:
    async def test_tool_hooks_register_on_the_bus_events(self) -> None:
        from arcagent.capabilities.capability_loader import CapabilityLoader
        from arcagent.capabilities.capability_registry import CapabilityRegistry
        from arcagent.modules.policy import capabilities as policy_caps

        module_dir = Path(policy_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("module:policy", module_dir)], registry=reg)
        await loader.scan_and_register()

        pre = await reg.get_hooks("agent:pre_tool")
        post = await reg.get_hooks("agent:post_tool")
        assert any(h.meta.name == "record_tool_call" for h in pre)
        assert any(h.meta.name == "record_tool_result" for h in post)

    async def test_nameless_tool_event_is_ignored(self, configured: Path) -> None:
        from arcagent.modules.policy import capabilities as policy_caps

        await policy_caps.record_tool_call(SimpleNamespace(data={}))
        await policy_caps.record_tool_result(SimpleNamespace(data={}))
        assert _runtime.state().tool_activity.as_messages() == []
