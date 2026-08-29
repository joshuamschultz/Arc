"""Workpad module capabilities — the self-managing context.md path.

Verifies:
  1. Hooks register via CapabilityLoader at the expected events/priorities.
  2. ``track_runs`` counts non-automated runs, accumulates + bounds the
     transcript, and fires the rewrite every ``every_n_runs`` runs.
  3. ``perform_maintenance`` rewrites context.md, sanitizes model output
     (ASI-06), and leaves the file untouched on empty output.
  4. ``drain_on_shutdown`` cancels in-flight tasks; unconfigured use raises.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core import turn_context
from arcagent.modules.workpad import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    # Default to a real (person-driven) turn; background-run tests flip this off.
    turn_context.set_interactive(True)
    yield
    _runtime.reset()
    turn_context.set_interactive(False)


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _runtime.configure(
        workspace=workspace,
        agent_name="test",
        agent_did="did:arc:test-workpad",
        config={"every_n_runs": 3},
    )
    return workspace


def _model(content: str) -> Any:
    model = MagicMock()
    model.invoke = AsyncMock(return_value=SimpleNamespace(content=content))
    return model


def _post_respond(user: str, assistant: str, *, automated: bool = False) -> Any:
    return SimpleNamespace(
        data={
            "messages": [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ],
            "session_id": "s1",
            "automated": automated,
        }
    )


async def _drain(st: _runtime._State) -> None:
    await asyncio.gather(*list(st.background_tasks), return_exceptions=True)


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_hooks_register(self) -> None:
        from arcagent.modules.workpad import capabilities as caps

        reg = CapabilityRegistry()
        loader = CapabilityLoader(
            scan_roots=[("module:workpad", Path(caps.__file__).parent)], registry=reg
        )
        await loader.scan_and_register()

        respond = await reg.get_hooks("agent:post_respond")
        shutdown = await reg.get_hooks("agent:shutdown")
        assert any(h.meta.name == "track_runs" for h in respond)
        assert any(h.meta.name == "drain_on_shutdown" for h in shutdown)

    async def test_priorities(self) -> None:
        from arcagent.modules.workpad.capabilities import drain_on_shutdown, track_runs

        assert track_runs._arc_capability_meta.priority == 120  # type: ignore[attr-defined]
        assert drain_on_shutdown._arc_capability_meta.priority == 60  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestTrackRuns:
    async def test_automated_run_skipped(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        await track_runs(_post_respond("hi", "yo", automated=True))
        st = _runtime.state()
        assert st.run_count == 0
        assert st.transcript == []

    async def test_background_run_does_not_count_toward_the_cadence(
        self, configured: Path
    ) -> None:
        # A pulse/proactive/consolidation self-wake is non-interactive: it must
        # not advance the workpad cadence or reset the idle clock.
        from arcagent.modules.workpad.capabilities import track_runs

        turn_context.set_interactive(False)
        await track_runs(_post_respond("pulse check", "nothing to do"))
        st = _runtime.state()
        assert st.run_count == 0
        assert st.transcript == []

    async def test_accumulates_transcript_and_counts(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        await track_runs(_post_respond("remember the invoice", "noted"))
        st = _runtime.state()
        assert st.run_count == 1
        assert st.transcript == ["[user] remember the invoice", "[assistant] noted"]

    async def test_does_not_fire_before_threshold(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        _runtime.state().eval_model = _model("# cockpit\n")
        await track_runs(_post_respond("a", "b"))  # run 1 of every_n_runs=3
        st = _runtime.state()
        assert st.background_tasks == set()
        assert not (configured / "context.md").exists()

    async def test_fires_and_rewrites_context_at_threshold(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        model = _model("Updated: 2026-07-12 | OPEN PROJECTS: 1\n\n## OPEN PROJECTS\n- ship it")
        _runtime.state().eval_model = model
        for _ in range(3):  # every_n_runs=3 → fires on the 3rd run
            await track_runs(_post_respond("do x", "ok"))
        st = _runtime.state()
        await _drain(st)

        context = (configured / "context.md").read_text(encoding="utf-8")
        assert "## OPEN PROJECTS" in context
        assert "ship it" in context
        model.invoke.assert_awaited_once()
        # Window drained for the next cycle.
        assert st.transcript == []

    async def test_no_model_skips_without_firing(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        # No eval model and no llm_config → get_eval_model returns None.
        for _ in range(3):
            await track_runs(_post_respond("x", "y"))
        st = _runtime.state()
        assert st.background_tasks == set()
        assert not (configured / "context.md").exists()

    async def test_transcript_trimmed_to_budget(self, tmp_path: Path) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            agent_did="did:arc:test-workpad",
            config={"every_n_runs": 100, "max_transcript_chars": 1000},
        )
        big = "z" * 400
        for _ in range(10):
            await track_runs(_post_respond(big, big))
        st = _runtime.state()
        total = sum(len(line) for line in st.transcript)
        assert total <= 1000 + len(big)  # bounded; last line may straddle the cap

    async def test_a_single_oversized_message_cannot_defeat_the_budget(
        self, tmp_path: Path
    ) -> None:
        """One giant turn (a coding agent's file/tool dump) must still be bounded.

        Dropping oldest lines leaves the last line whole, so a lone over-budget
        message sailed straight into the workpad rewrite — the path that made a
        few-thousand-char budget resend a ~190k-token transcript every run.
        """
        from arcagent.modules.workpad.capabilities import track_runs

        ws = tmp_path / "ws"
        ws.mkdir()
        _runtime.configure(
            workspace=ws,
            agent_name="t",
            agent_did="did:arc:test-workpad",
            config={"every_n_runs": 100, "max_transcript_chars": 1000},
        )
        await track_runs(_post_respond("go", "z" * 200_000))
        st = _runtime.state()
        total = sum(len(line) for line in st.transcript)
        assert total <= 1000 + 100, total


@pytest.mark.asyncio
class TestPerformMaintenance:
    async def test_rewrites_full_file(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import perform_maintenance

        (configured / "context.md").write_text("# old cockpit\n", encoding="utf-8")
        st = _runtime.state()
        wrote = await perform_maintenance(st, _model("# new cockpit\n- open loop"), "activity")
        assert wrote is True
        assert (configured / "context.md").read_text() == "# new cockpit\n- open loop\n"

    async def test_empty_output_leaves_file_untouched(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import perform_maintenance

        (configured / "context.md").write_text("# keep me\n", encoding="utf-8")
        st = _runtime.state()
        wrote = await perform_maintenance(st, _model("   \n  "), "activity")
        assert wrote is False
        assert (configured / "context.md").read_text() == "# keep me\n"

    async def test_sanitizes_malicious_output(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import perform_maintenance

        st = _runtime.state()
        # Zero-width + Unicode-tag instruction smuggling must be stripped (ASI-06).
        poisoned = "## OPEN\n- item​\U000e0041\U000e0042"
        await perform_maintenance(st, _model(poisoned), "activity")
        content = (configured / "context.md").read_text()
        assert "​" not in content
        assert "\U000e0041" not in content
        assert "- item" in content

    async def test_passes_current_file_and_activity_to_model(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import perform_maintenance

        (configured / "context.md").write_text("EXISTING COCKPIT", encoding="utf-8")
        model = _model("# rewritten")
        st = _runtime.state()
        await perform_maintenance(st, model, "user asked about the deadline")
        messages = model.invoke.call_args.args[0]
        assert messages[0].role == "system"
        assert "CONTEXT FILE MAINTENANCE" in messages[0].content
        assert "EXISTING COCKPIT" in messages[1].content
        assert "user asked about the deadline" in messages[1].content


@pytest.mark.asyncio
class TestShutdownAndContract:
    async def test_drain_cancels_tasks(self, configured: Path) -> None:
        from arcagent.modules.workpad.capabilities import drain_on_shutdown

        async def _never() -> None:
            await asyncio.sleep(3600)

        st = _runtime.state()
        task: asyncio.Task[None] = asyncio.create_task(_never())
        st.background_tasks.add(task)
        await drain_on_shutdown(SimpleNamespace(data={}))
        assert task.cancelled()

    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.workpad.capabilities import track_runs

        # No agent DID is bound after reset() → state() fails closed rather than
        # hand back another agent's cockpit.
        with pytest.raises(RuntimeError, match="no agent DID bound"):
            await track_runs(_post_respond("x", "y"))


class TestMaintainerInput:
    """The maintainer's user turn and the prompt that governs it.

    The rewrite is a single-shot call whose whole reply becomes context.md, so
    two things it cannot get from anywhere else have to be in the input: what
    day it is, and the authority to delete.
    """

    def test_render_input_supplies_todays_date(self) -> None:
        from datetime import date

        from arcagent.modules.workpad.capabilities import _render_input

        # Without this the model invents a date, and every staleness judgement
        # in the prompt is then anchored to a year that never happened.
        assert date.today().isoformat() in _render_input("# CONTEXT", "did a thing")

    def test_prompt_makes_the_user_word_final(self) -> None:
        from arcprompt import load_stock

        prompt = load_stock("arcagent", "context_maintainer_system").lower()

        # An explicit "remove this" must outrank the judgement tests below it.
        # Without that precedence a finished-but-undecided item satisfies no
        # removal test and is kept forever, however many times it is asked for.
        assert "the user's word is final" in prompt
        assert "outranks" in prompt

        # ...but not so far that an unmade decision goes with it.
        assert "decisions outstanding" in prompt

    def test_prompt_asks_for_the_file_and_nothing_else(self) -> None:
        from arcprompt import load_stock

        prompt = load_stock("arcagent", "context_maintainer_system")

        # The reply IS the file. A prompt that also asks for a change report
        # gets one — written into the cockpit, loaded on every later turn.
        assert "written to `context.md` verbatim" in prompt
        assert "session report" in prompt.lower()
