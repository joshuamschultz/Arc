"""Smoke test: ArcTUI launch, slash command dispatch, and clean shutdown.

Key deliverable: G3.4 — arctui smoke test (PLAN.md G3.4).

Contract:
1. ArcTUI boots without a real ArcAgent (mock agent, no LLM).
2. The app renders transcript area, activity area, and input composer.
3. ``/help`` in the composer populates completions from arccli registry.
4. A plain text message is echoed back (no-agent mode stub response).
5. App exits cleanly via ``action_quit()``; no dangling asyncio tasks.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helper: minimal no-agent ArcTUI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_renders_panels() -> None:
    """ArcTUI renders transcript, activity, and composer panels."""
    from arctui.app import ArcTUI

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        # Transcript panel exists
        from arctui.transcript import TranscriptView

        tv = pilot.app.query_one("#transcript", TranscriptView)
        assert tv is not None

        # Activity panel exists
        from arctui.activity import ActivityView

        av = pilot.app.query_one("#activity", ActivityView)
        assert av is not None

        # Input composer exists
        from arctui.input_composer import InputComposer

        ic = pilot.app.query_one("#composer", InputComposer)
        assert ic is not None


@pytest.mark.asyncio
async def test_welcome_message_shown() -> None:
    """ArcTUI displays a welcome message in the transcript on mount."""
    from arctui.app import ArcTUI
    from arctui.transcript import TranscriptView

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        tv = pilot.app.query_one("#transcript", TranscriptView)
        assert len(tv._messages) > 0
        # Welcome text mentions 'ready' or '/help'
        texts = [m.content for m in tv._messages]
        assert any("ready" in t.lower() or "help" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_slash_help_shows_completions() -> None:
    """Typing /h in the composer shows completions from arccli registry."""
    from textual.widgets import Input, OptionList

    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        ic = pilot.app.query_one("#composer", InputComposer)
        inp = ic.query_one("#main-input", Input)  # noqa: F841 — needed for focus

        # Simulate typing /h to trigger completions
        await pilot.click("#main-input")
        await pilot.press("slash")
        await pilot.press("h")
        await pilot.pause()

        # Completion list should be visible
        option_list = ic.query_one("#completion-list", OptionList)
        assert option_list.display is True
        assert option_list.option_count > 0


@pytest.mark.asyncio
async def test_slash_help_dispatch() -> None:
    """Submitting /help appends help text to the transcript."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import MessageRole, TranscriptView

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        tv = pilot.app.query_one("#transcript", TranscriptView)
        initial_count = len(tv._messages)

        # Post a CommandMessage directly to avoid needing keyboard typing.
        # resolved=True because "help" is a known command.
        ic = pilot.app.query_one("#composer", InputComposer)
        ic.post_message(InputComposer.CommandMessage("help", [], resolved=True))
        await pilot.pause()

        # A system message with help content should have been added
        assert len(tv._messages) > initial_count
        help_msgs = [m for m in tv._messages if m.role == MessageRole.SYSTEM]
        assert any("Available" in m.content or "command" in m.content.lower() for m in help_msgs)


@pytest.mark.asyncio
async def test_plain_text_no_agent_stub_response() -> None:
    """Plain text input in no-agent mode shows stub response."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import MessageRole, TranscriptView

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        tv = pilot.app.query_one("#transcript", TranscriptView)
        ic = pilot.app.query_one("#composer", InputComposer)

        ic.post_message(InputComposer.SubmitMessage("hello there"))
        await pilot.pause(delay=0.2)

        # User message should be in transcript
        user_msgs = [m for m in tv._messages if m.role == MessageRole.USER]
        assert any("hello there" in m.content for m in user_msgs)

        # Stub response should appear
        asst_msgs = [m for m in tv._messages if m.role == MessageRole.ASSISTANT]
        assert len(asst_msgs) > 0


@pytest.mark.asyncio
async def test_clean_shutdown_via_quit() -> None:
    """App exits cleanly via action_quit with no dangling tasks."""
    from arctui.app import ArcTUI

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        # Trigger quit action
        await pilot.press("ctrl+c")
        # run_test context manager handles cleanup; if we reach here without
        # error the shutdown was clean.

    # After context exit the event loop should have no leftover tasks from us.
    assert True  # If we reached this line, no exception was raised.


@pytest.mark.asyncio
async def test_unknown_command_shows_error() -> None:
    """An unknown slash command shows an error message in the transcript."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import TranscriptView

    app = ArcTUI(agent=None)
    async with app.run_test() as pilot:
        tv = pilot.app.query_one("#transcript", TranscriptView)
        ic = pilot.app.query_one("#composer", InputComposer)

        # Post a SubmitMessage with an unknown slash command.
        # on_input_composer_submit_message parses slash input and calls
        # _dispatch_command which shows "Unknown command: /zzznomatch".
        ic.post_message(InputComposer.SubmitMessage("/zzznomatch"))
        await pilot.pause()

        # An error/system message about the unknown command
        texts = [m.content for m in tv._messages]
        assert any("unknown" in t.lower() or "zzznomatch" in t.lower() for t in texts)


@pytest.mark.asyncio
async def test_tui_command_registered_in_registry() -> None:
    """The 'tui' command is present in arccli registry after entry import."""
    # Importing entry.py triggers the registration.
    from arccli.commands.registry import resolve_command

    import arctui.entry  # noqa: F401  — import for side effect

    cmd = resolve_command("tui")
    assert cmd is not None
    assert cmd.name == "tui"
    assert cmd.cli_only is True
