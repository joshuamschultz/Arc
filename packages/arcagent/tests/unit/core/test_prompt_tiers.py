"""System-prompt tiering — what makes the provider cache actually hit.

A provider caches the longest stable prefix, and the conversation sits behind
the whole system prompt. So the only thing that keeps a long session cheap is
a system prompt whose bytes do not move between turns. These tests pin that
property directly: assemble twice with a different turn query and assert the
system segments are byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import ContextConfig
from arcagent.core.module_bus import EventContext, ModuleBus
from arcagent.core.session_internal.context import ContextManager, wire_messages


@pytest.fixture()
def bus() -> ModuleBus:
    return ModuleBus()


@pytest.fixture()
def mgr(bus: ModuleBus) -> ContextManager:
    config = ContextConfig(
        max_tokens=1000,
        prune_threshold=0.70,
        compact_threshold=0.85,
        emergency_threshold=0.95,
        estimate_multiplier=1.1,
    )
    return ContextManager(config=config, telemetry=MagicMock(), bus=bus)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "identity.md").write_text("I am the intake agent.")
    (tmp_path / "context.md").write_text("Open loop: ship the cache fix.")
    return tmp_path


def _echo_query_into_recall(bus: ModuleBus) -> None:
    """Stand in for the memory module: recall differs on every turn."""

    async def inject(ctx: EventContext) -> None:
        ctx.data["sections"]["recall"] = f"recalled for: {ctx.data['query']}"

    bus.subscribe("agent:assemble_prompt", inject, priority=50)


class TestTierPlacement:
    async def test_identity_and_caller_sections_are_session_stable(
        self, mgr: ContextManager, workspace: Path
    ) -> None:
        prompt = await mgr.assemble_system_prompt(
            workspace, extra_sections={"base": "harness preamble", "strategy_react": "loop rules"}
        )
        assert "harness preamble" in prompt.session
        assert "I am the intake agent." in prompt.session
        assert "loop rules" in prompt.session
        assert "Open loop" not in prompt.session

    async def test_context_md_is_its_own_run_segment(
        self, mgr: ContextManager, workspace: Path
    ) -> None:
        prompt = await mgr.assemble_system_prompt(workspace)
        assert "Open loop: ship the cache fix." in prompt.run
        assert prompt.segments == [prompt.session, prompt.run]

    async def test_base_leads_the_session_segment(
        self, mgr: ContextManager, workspace: Path
    ) -> None:
        prompt = await mgr.assemble_system_prompt(
            workspace, extra_sections={"base": "harness preamble"}
        )
        assert prompt.session.index("harness preamble") < prompt.session.index(
            "I am the intake agent."
        )

    async def test_recall_leaves_the_system_prompt(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        _echo_query_into_recall(bus)
        prompt = await mgr.assemble_system_prompt(workspace, query="who owns payments")

        assert "recalled for: who owns payments" in prompt.turn
        assert "recalled for" not in prompt.as_text()

    async def test_unknown_section_falls_back_to_the_run_segment(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        """A module we cannot vouch for must never sit in front of the session segment."""

        async def inject(ctx: EventContext) -> None:
            ctx.data["sections"]["some_new_module"] = "who knows how often this changes"

        bus.subscribe("agent:assemble_prompt", inject)
        prompt = await mgr.assemble_system_prompt(workspace)

        assert "who knows how often this changes" in prompt.run
        assert "who knows how often this changes" not in prompt.session

    async def test_never_exceeds_the_providers_system_segment_budget(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        async def inject(ctx: EventContext) -> None:
            ctx.data["sections"].update(
                {"policy": "p", "teams": "t", "planning": "pl", "whatever": "w"}
            )

        bus.subscribe("agent:assemble_prompt", inject)
        prompt = await mgr.assemble_system_prompt(workspace, extra_sections={"base": "b"})
        assert len(prompt.segments) <= 2


class TestPrefixStability:
    async def test_system_segments_are_byte_identical_across_turns(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        """The whole point: a different turn must not move a single system byte."""
        _echo_query_into_recall(bus)

        first = await mgr.assemble_system_prompt(workspace, query="turn one")
        second = await mgr.assemble_system_prompt(workspace, query="a totally different turn")

        assert first.segments == second.segments
        assert first.turn != second.turn

    async def test_context_md_rewrite_spares_the_session_segment(
        self, mgr: ContextManager, workspace: Path
    ) -> None:
        first = await mgr.assemble_system_prompt(workspace)
        (workspace / "context.md").write_text("Open loop: something else entirely.")
        second = await mgr.assemble_system_prompt(workspace)

        assert first.session == second.session
        assert first.run != second.run


class TestSessionRecord:
    """The session stays the conversation; retrieved material sits beside it."""

    async def test_stored_content_is_only_what_the_person_said(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        _echo_query_into_recall(bus)
        prompt = await mgr.assemble_system_prompt(workspace, query="hello")

        record = prompt.session_record("hello")
        assert record["content"] == "hello"
        assert record["role"] == "user"
        assert "recalled for: hello" in record["turn_context"]

    async def test_no_turn_context_means_no_extra_field(
        self, mgr: ContextManager, workspace: Path
    ) -> None:
        prompt = await mgr.assemble_system_prompt(workspace, query="hello")
        assert prompt.session_record("hello") == {"role": "user", "content": "hello"}

    async def test_empty_sections_are_skipped(self, mgr: ContextManager, tmp_path: Path) -> None:
        prompt = await mgr.assemble_system_prompt(tmp_path, extra_sections={"base": ""})
        assert prompt.segments == []


class TestWireMessages:
    """Replay must reproduce the sent bytes exactly, or the cache stops matching."""

    async def test_stored_context_is_reattached_on_replay(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        _echo_query_into_recall(bus)
        prompt = await mgr.assemble_system_prompt(workspace, query="hello")
        record = prompt.session_record("hello")

        wire = wire_messages([record])[0]
        assert isinstance(wire.content, str)
        assert wire.content.startswith("hello")
        assert "<agent-context>" in wire.content
        assert "recalled for: hello" in wire.content

    async def test_records_without_context_pass_through_unchanged(
        self, mgr: ContextManager, workspace: Path
    ) -> None:
        wire = wire_messages([{"role": "assistant", "content": "done"}])
        assert wire[0].content == "done"

    async def test_session_bookkeeping_fields_are_ignored(self) -> None:
        """Records carry ``type``/``timestamp`` from the JSONL — not wire fields."""
        wire = wire_messages(
            [{"type": "message", "timestamp": "t", "role": "user", "content": "hi"}]
        )
        assert wire[0].role == "user"
        assert wire[0].content == "hi"


class TestBusPayloadUnchanged:
    async def test_handlers_still_see_one_flat_sections_dict(
        self, mgr: ContextManager, bus: ModuleBus, workspace: Path
    ) -> None:
        """Tiering is applied after the event — modules stay unaware of it."""
        seen: list[Any] = []

        async def handler(ctx: EventContext) -> None:
            seen.append(dict(ctx.data["sections"]))

        bus.subscribe("agent:assemble_prompt", handler)
        await mgr.assemble_system_prompt(workspace)

        assert seen[0] == {
            "identity": "I am the intake agent.",
            "context": "Open loop: ship the cache fix.",
        }
