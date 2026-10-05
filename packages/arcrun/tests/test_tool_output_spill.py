"""An over-threshold tool result is saved whole and stays readable (SPEC-070)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from packages.arcrun.tests.conftest import LLMResponse, MockModel, ToolCall

import arcrun
from arcrun.executor import execute_tool_call
from arcrun.loop import _build_state
from arcrun.spill import CHARS_PER_TOKEN, MAX_READ_TOKENS, SpillStore, prune_spills
from arcrun.types import Tool

HANDLE = re.compile(r"spill_[0-9a-f]{32}")
FOOTER = re.compile(r"\n\[read_tool_output: tokens (\d+)-(\d+) of (\d+)\. (.*)\]$", re.DOTALL)


def _document(tokens: int) -> str:
    """Distinct text per position, so a wrong chunk cannot match by accident."""
    out: list[str] = []
    size, i = 0, 0
    while size < tokens * CHARS_PER_TOKEN:
        line = f"line {i:07d} é ✓\n"
        out.append(line)
        size += len(line)
        i += 1
    return "".join(out)[: tokens * CHARS_PER_TOKEN]


def _big_tool(text: str) -> Tool:
    async def execute(params: dict[str, Any], ctx: object) -> str:
        return text

    return Tool(
        name="big",
        description="Returns a large result",
        input_schema={"type": "object", "properties": {}},
        execute=execute,
    )


def _state(tmp_path: Path, text: str, **kwargs: Any) -> Any:
    return _build_state(
        arcrun.StaticProvider([_big_tool(text)]), "sys", "go", work_dir=tmp_path, **kwargs
    )


def _words(message: Any) -> str:
    return arcrun.content_text(message.content[0].content)


async def _call(built: Any, name: str, args: dict[str, Any], call_id: str = "tc") -> str:
    state, sandbox = built
    message, _ = await execute_tool_call(
        ToolCall(id=call_id, name=name, arguments=args), state, sandbox
    )
    return _words(message)


def _end(built: Any, call_id: str = "tc") -> Any:
    state, _ = built
    return next(
        e
        for e in state.event_bus.events
        if e.type == "tool.end" and e.data["tool_call_id"] == call_id
    )


def _handle(shown: str) -> str:
    match = HANDLE.search(shown)
    assert match is not None
    return match.group(0)


class TestSpill:
    @pytest.mark.asyncio
    async def test_over_threshold_shows_head_and_marker_and_saves_whole(self, tmp_path):
        text = _document(30_000)
        built = _state(tmp_path, text)

        shown = await _call(built, "big", {})

        handle = _handle(shown)
        assert shown.startswith(text[: 2000 * CHARS_PER_TOKEN])
        assert text[2000 * CHARS_PER_TOKEN :][:200] not in shown
        assert (
            f"Full output is 30000 tokens and was saved as {handle}. "
            "Read more with read_tool_output(handle, offset, limit) or search it "
            "with search_tool_output(handle, query)."
        ) in shown
        saved = next((tmp_path / "spill").rglob(handle))
        assert saved.read_text(encoding="utf-8") == text

    @pytest.mark.asyncio
    async def test_chunks_reassemble_byte_identical(self, tmp_path):
        text = _document(30_000)
        built = _state(tmp_path, text)
        handle = _handle(await _call(built, "big", {}))

        rebuilt, offset, reads = "", 0, 0
        while True:
            reply = await _call(
                built,
                "read_tool_output",
                {"handle": handle, "offset": offset, "limit": 7001},
                call_id=f"r{reads}",
            )
            footer = FOOTER.search(reply)
            assert footer is not None
            rebuilt += reply[: footer.start()]
            offset = int(footer.group(2))
            reads += 1
            if footer.group(4) == "End of output.":
                break

        assert rebuilt.encode("utf-8") == text.encode("utf-8")
        assert reads == 5

    @pytest.mark.asyncio
    async def test_search_finds_text_deep_in_the_spill(self, tmp_path):
        text = _document(30_000) + "NEEDLE-ZEBRA-42" + _document(500)
        built = _state(tmp_path, text)
        shown = await _call(built, "big", {})
        assert "NEEDLE-ZEBRA-42" not in shown

        found = await _call(
            built, "search_tool_output", {"handle": _handle(shown), "query": "needle-zebra"}
        )

        assert "NEEDLE-ZEBRA-42" in found
        assert "@token 30000" in found

    @pytest.mark.asyncio
    async def test_under_threshold_untouched(self, tmp_path):
        text = _document(19_999)
        built = _state(tmp_path, text)

        shown = await _call(built, "big", {})

        assert shown == text
        assert not (tmp_path / "spill").exists()
        assert "spilled" not in _end(built).data

    @pytest.mark.asyncio
    async def test_trace_event_reports_spill_size_and_handle(self, tmp_path):
        built = _state(tmp_path, _document(30_000))
        shown = await _call(built, "big", {})

        data = _end(built).data
        assert data["spilled"] is True
        assert data["spill_tokens"] == 30_000
        assert data["spill_handle"] == _handle(shown)
        assert not any(k in data for k in ("truncated", "original_tokens"))

    @pytest.mark.asyncio
    async def test_threshold_is_configurable_and_none_disables(self, tmp_path):
        built = _state(tmp_path, _document(500), tool_result_spill_tokens=100)
        assert "was saved as" in await _call(built, "big", {})

        off = _state(tmp_path, _document(50_000), tool_result_spill_tokens=None)
        assert len(await _call(off, "big", {})) == 50_000 * CHARS_PER_TOKEN
        assert "read_tool_output" not in off[0].registry.names()

    @pytest.mark.asyncio
    async def test_no_work_dir_passes_result_whole(self):
        text = _document(50_000)
        built = _build_state(arcrun.StaticProvider([_big_tool(text)]), "sys", "go")
        assert await _call(built, "big", {}) == text

    @pytest.mark.asyncio
    async def test_read_backstop_clamps_and_continues_instead_of_dropping(self, tmp_path):
        built = _state(tmp_path, _document(MAX_READ_TOKENS + 5000))
        handle = _handle(await _call(built, "big", {}))

        first = await _call(
            built, "read_tool_output", {"handle": handle, "limit": 10_000_000}, "r1"
        )

        footer = FOOTER.search(first)
        assert footer is not None
        assert int(footer.group(2)) == MAX_READ_TOKENS
        assert footer.group(4) == f"Next offset: {MAX_READ_TOKENS}."
        assert "was saved as" not in first  # a read is never re-spilled

    @pytest.mark.asyncio
    async def test_read_tools_are_read_only_and_registered_with_the_run(self, tmp_path):
        state, _ = _state(tmp_path, "x")
        for name in ("read_tool_output", "search_tool_output"):
            tool = state.registry.get(name)
            assert tool is not None
            assert tool.classification == "read_only"

    @pytest.mark.asyncio
    async def test_operator_allowlist_still_reaches_the_run_own_output(self, tmp_path):
        built = _state(
            tmp_path, _document(30_000), sandbox=arcrun.SandboxConfig(allowed_tools=["big"])
        )
        handle = _handle(await _call(built, "big", {}))

        reply = await _call(built, "read_tool_output", {"handle": handle}, "r1")

        assert not reply.startswith("Error")

    @pytest.mark.asyncio
    async def test_real_loop_model_searches_the_saved_output(self, tmp_path):
        text = _document(30_000) + "ANSWER=7"

        class Reader(MockModel):
            async def invoke(self, messages, tools=None, **kwargs):
                if tools and any(t.name == "select_strategy" for t in tools):
                    return await super().invoke(messages, tools, **kwargs)
                tool_messages = [m for m in messages if m.role == "tool"]
                last = _words(tool_messages[-1]) if tool_messages else ""
                if "Full output" in last:
                    args = {"handle": _handle(last), "query": "ANSWER="}
                    call = ToolCall("c2", "search_tool_output", args)
                    return LLMResponse(tool_calls=[call], stop_reason="tool_use")
                if "match(es)" in last:
                    return LLMResponse(content=last, stop_reason="end_turn")
                return LLMResponse(tool_calls=[ToolCall("c1", "big", {})], stop_reason="tool_use")

        result = await arcrun.run(
            Reader([]), arcrun.StaticProvider([_big_tool(text)]), "sys", "go", work_dir=tmp_path
        )

        assert "ANSWER=7" in result.content


class TestRetention:
    def test_prune_keeps_only_the_newest_runs(self, tmp_path):
        for i in range(4):
            store = SpillStore(tmp_path, f"run-{i}")
            store.spill("x")
            os.utime(store.directory, (1000 + i, 1000 + i))

        assert prune_spills(tmp_path, keep_runs=2) == 2
        assert sorted(p.name for p in (tmp_path / "spill").iterdir()) == ["run-2", "run-3"]

    def test_prune_with_no_spills_is_a_noop(self, tmp_path):
        assert prune_spills(tmp_path, keep_runs=1) == 0
