"""ArcTUI — main Textual application.

Single-process design (D-15): the Textual ``App`` runs in the same asyncio
event loop as ArcAgent.  There is no subprocess split or Node/Ink bridge.

Architecture
------------

┌─────────────────────────────────────────┐
│              ArcTUI (App)               │
│                                         │
│  ┌─────────────────────┬─────────────┐  │
│  │  TranscriptView     │ ActivityView│  │
│  │  (left, scrollable) │ (right)     │  │
│  ├─────────────────────┴─────────────┤  │
│  │         InputComposer             │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘

Event flow:
    User types → InputComposer submits → ArcTUI handlers
    Non-slash: _send_to_agent (Textual @work task)
        → agent.run(text, session=agent.session("tui:main"))  (one entry)
        → ArcAgent bus events → ActivityView
        → tokens appended to TranscriptView live
    Slash: _dispatch_command → registry handler or error message

Streaming:
    ``agent.run`` yields arcrun.StreamEvent objects; ``_run_stream_turn`` renders
    each TokenEvent via start_streaming/append_delta/finish_streaming on the
    TranscriptView for live token rendering.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label

from arctui.activity import ActivityView
from arctui.input_composer import InputComposer
from arctui.theme import build_tcss
from arctui.transcript import MessageRole, TranscriptView

_logger = logging.getLogger("arctui.app")


class ArcTUI(App[None]):
    """Arc terminal UI — Textual application.

    Parameters
    ----------
    agent:
        A started (``await agent.startup()`` already called) ArcAgent
        instance.  Pass ``None`` in tests to boot without a real agent.
    title:
        Optional application title shown in the header.
    """

    CSS = build_tcss()

    BINDINGS: ClassVar[list[Any]] = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_transcript", "Clear"),
    ]

    TITLE = "Arc TUI"
    SUB_TITLE = "Terminal Interface"

    def __init__(
        self,
        *,
        agent: Any = None,
        title: str = "Arc TUI",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self.title = title
        self._transcript: TranscriptView | None = None
        self._activity: ActivityView | None = None
        self._composer: InputComposer | None = None

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the three-panel layout."""
        yield Header()
        with Horizontal():
            # Left column: transcript
            with Vertical(id="transcript-column"):
                yield Label("Transcript", id="transcript-title")
                transcript = TranscriptView(id="transcript")
                self._transcript = transcript
                yield transcript
            # Right column: activity
            with Vertical(id="activity-column"):
                yield Label("Activity", id="activity-title")
                activity = ActivityView(id="activity")
                self._activity = activity
                yield activity
        # Bottom: input composer (full width)
        composer = InputComposer(id="composer")
        self._composer = composer
        yield composer
        yield Footer()

    def on_mount(self) -> None:
        """Wire event bridges and show welcome message on first mount."""
        self._wire_event_bus()
        if self._transcript is not None:
            self._transcript.add_message(
                MessageRole.SYSTEM,
                "ArcTUI ready. Type a message or /help for commands.",
            )

    # ------------------------------------------------------------------
    # Event bus bridge (arcrun → ActivityView)
    # ------------------------------------------------------------------

    def _wire_event_bus(self) -> None:
        """Register an arcrun event bridge on the agent's module bus.

        If no agent is attached (test mode), this is a no-op.  The bridge
        routes tool.start / tool.end / turn.start / turn.end / llm.call
        events to ActivityView.handle_event which crosses back to the UI
        thread via call_from_thread.

        We subscribe at the Module Bus level so we don't duplicate the
        bridge logic from create_arcrun_bridge in agent.py.
        """
        if self._agent is None:
            return

        bus = getattr(self._agent, "_bus", None)
        if bus is None:
            return

        activity = self._activity
        if activity is None:
            return

        # Subscribe to arcrun-mapped events on the Module Bus.
        # These event names are the Bus-side names after the bridge mapping
        # in create_arcrun_bridge (agent.py line ~78).
        arcrun_bus_events = [
            "agent:pre_tool",
            "agent:post_tool",
            "agent:pre_plan",
            "agent:post_plan",
        ]

        for bus_event in arcrun_bus_events:
            # Capture bus_event in the closure correctly.
            def _make_subscriber(ev: str) -> Any:
                async def _subscriber(data: dict[str, Any]) -> None:
                    # Map bus event names back to arcrun-style names for
                    # ActivityView which expects "tool.start" etc.
                    name_map = {
                        "agent:pre_tool": "tool.start",
                        "agent:post_tool": "tool.end",
                        "agent:pre_plan": "turn.start",
                        "agent:post_plan": "turn.end",
                    }
                    arcrun_name = name_map.get(ev, ev)
                    activity.handle_event(arcrun_name, dict(data))

                return _subscriber

            bus.subscribe(bus_event, _make_subscriber(bus_event), priority=999)

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def on_input_composer_submit_message(self, message: InputComposer.SubmitMessage) -> None:
        """Receive raw user text from the composer.

        For non-slash input, dispatch to the agent.
        For slash input, dispatch as a command (handles the case where
        the test posts SubmitMessage directly without a preceding
        CommandMessage from InputComposer._do_submit).
        """
        text = message.text.strip()
        if not text:
            return

        if text.startswith("/"):
            # Parse and dispatch slash command.
            parts = text.lstrip("/").split()
            if parts:
                cmd_name = parts[0]
                args = parts[1:]
                self._dispatch_command(cmd_name, args)
        else:
            # Normal prose — send to agent.
            self._send_to_agent(text)

    def on_input_composer_command_message(self, message: InputComposer.CommandMessage) -> None:
        """Receive a parsed slash command from the composer.

        Called when InputComposer._do_submit posts a CommandMessage.
        The resolved flag indicates whether the command was found in
        the registry; _dispatch_command handles both cases.
        """
        self._dispatch_command(message.command, message.args)

    # ------------------------------------------------------------------
    # Agent turn execution
    # ------------------------------------------------------------------

    @work(exclusive=True, thread=False)
    async def _send_to_agent(self, text: str) -> None:
        """Run one agent turn for *text* with per-token streaming.

        The ``@work(exclusive=True)`` decorator ensures only one turn
        runs at a time — concurrent submits queue up. Tokens are appended to
        the transcript incrementally via ``TranscriptView.append_delta``.
        """
        if self._transcript is None:
            return

        # Show the user message immediately.
        self._transcript.add_message(MessageRole.USER, text)

        if self._agent is None:
            # Test mode / no agent attached — echo back a stub response.
            self._transcript.add_message(
                MessageRole.ASSISTANT,
                "(No agent attached. Use a real ArcAgent for live responses.)",
            )
            return

        await self._run_stream_turn(text)

    async def _run_stream_turn(self, text: str) -> None:
        """Stream one agent turn via ``agent.run(text, session=...)``.

        Displays tokens incrementally via TranscriptView.  The streaming
        cursor (▋) is shown while the assistant is still typing.  Called
        from the ``@work`` task in ``_send_to_agent`` — must NOT be decorated
        with ``@work`` itself (Workers cannot be awaited).

        Args:
            text: User prompt text.
        """
        if self._transcript is None:
            return

        from arcrun import TokenEvent

        try:
            self._transcript.start_streaming(MessageRole.ASSISTANT)
            session = await self._agent.session("tui:main")
            async for event in self._agent.run(text, session=session):
                if isinstance(event, TokenEvent):
                    self._transcript.append_delta(event.text)
                # tool_start / tool_end events are handled by ActivityView
                # via the module bus bridge — no transcript update needed.
            self._transcript.finish_streaming()
        except Exception as exc:  # reason: fail-open — log + continue
            _logger.exception("Agent stream turn failed: %s", exc)
            self._transcript.finish_streaming()
            self._transcript.add_message(MessageRole.ERROR, f"Error: {exc}")

    # ------------------------------------------------------------------
    # Slash command dispatch
    # ------------------------------------------------------------------

    def _dispatch_command(self, command: str, args: list[str]) -> None:
        """Route a parsed slash command to its handler.

        Commands are executed synchronously in the UI thread for simple
        built-ins (help, clear, quit). Synchronous handlers run in a
        worker thread via Textual's run_worker.

        Shows an "Unknown command" message in the transcript for any
        command not found in the registry.
        """
        # Built-in TUI commands handled directly.
        if command == "quit" or command in ("exit", "q", "bye"):
            self.exit()
            return
        if command == "clear":
            self._action_clear_transcript()
            return
        if command == "help":
            self._show_help()
            return

        # Fall through to registry handler.
        from arccli.commands.registry import resolve_command

        cmd = resolve_command(command)
        if cmd is None:
            if self._transcript is not None:
                self._transcript.add_message(MessageRole.SYSTEM, f"Unknown command: /{command}")
            return

        if cmd.handler is not None:
            # Run potentially blocking handler via Textual worker thread.
            # run_worker accepts a callable and runs it in a thread pool.
            handler = cmd.handler
            captured_args = args

            def _run_handler() -> None:
                handler(captured_args)

            self.run_worker(_run_handler, thread=True)
        else:
            if self._transcript is not None:
                self._transcript.add_message(
                    MessageRole.SYSTEM, f"/{command}: no handler registered."
                )

    def _show_help(self) -> None:
        """Display help text from the registry in the transcript."""
        from arccli.commands.render import commands_by_category

        if self._transcript is None:
            return

        lines = ["Available commands:"]
        by_cat = commands_by_category()
        for category, cmds in by_cat.items():
            lines.append(f"  {category}:")
            for cmd in cmds:
                hint = f" {cmd.args_hint}" if cmd.args_hint else ""
                lines.append(f"    /{cmd.name}{hint}  — {cmd.description}")
        self._transcript.add_message(MessageRole.SYSTEM, "\n".join(lines))

    # ------------------------------------------------------------------
    # Actions (bound in BINDINGS)
    # ------------------------------------------------------------------

    async def action_quit(self) -> None:
        """Clean shutdown."""
        self.exit()

    def action_clear_transcript(self) -> None:
        """Clear the transcript panel."""
        self._action_clear_transcript()

    def _action_clear_transcript(self) -> None:
        if self._transcript is not None:
            self._transcript.clear()
