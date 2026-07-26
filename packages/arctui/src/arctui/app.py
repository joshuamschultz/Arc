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

arctui is a *viewpoint* onto a served agent (SPEC-058 Phase 3): it drives turns
through a :class:`~arctui.transport.ChatTransport` (the gateway's ``/ws/chat``
route) and never constructs an ``ArcAgent`` of its own.

Event flow:
    User types → InputComposer submits → ArcTUI handlers
    Non-slash: _send_to_agent (Textual @work task)
        → transport.send_turn(text) → TurnEvent stream
        → reply text appended to TranscriptView
    Slash: _dispatch_command → registry handler or error message

Streaming:
    ``transport.send_turn`` yields ``TurnEvent`` items; ``_run_stream_turn``
    renders each via start_streaming/append_delta/finish_streaming on the
    TranscriptView. The web transport is block-at-turn (one "message" event per
    turn); a future streaming transport yields many without any render change.
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
from arctui.transport import ChatTransport

_logger = logging.getLogger("arctui.app")


class ArcTUI(App[None]):
    """Arc terminal UI — Textual application.

    Parameters
    ----------
    transport:
        A connected ``ChatTransport`` (its ``connect()`` already awaited)
        driving a served agent. Pass ``None`` in tests to boot without one.
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
        transport: ChatTransport | None = None,
        title: str = "Arc TUI",
        agent_label: str | None = None,
        gateway_label: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._transport = transport
        self.title = title
        self._agent_label = agent_label
        self._gateway_label = gateway_label
        self._turns = 0
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
        """Show the welcome message and status line on first mount."""
        self._refresh_status()
        if self._transcript is not None:
            self._transcript.add_message(
                MessageRole.SYSTEM,
                "ArcTUI ready. Type a message or /help for commands.",
            )

    def _refresh_status(self) -> None:
        """Render the attach context into the header sub-title (agent · gateway · turns)."""
        if self._agent_label is None:
            self.sub_title = "no agent attached"
            return
        parts = [f"◆ {self._agent_label}"]
        if self._gateway_label is not None:
            parts.append(self._gateway_label)
        parts.append(f"turns {self._turns}")
        self.sub_title = "  ·  ".join(parts)

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
        """Drive one agent turn for *text* through the transport.

        The ``@work(exclusive=True)`` decorator ensures only one turn runs at
        a time — concurrent submits queue up. Reply text is appended to the
        transcript as each ``TurnEvent`` arrives.
        """
        if self._transcript is None:
            return

        # Show the user message immediately.
        self._transcript.add_message(MessageRole.USER, text)

        if self._transport is None:
            # Test mode / no transport attached — echo back a stub response.
            self._transcript.add_message(
                MessageRole.ASSISTANT,
                "(No agent attached. Launch with `arc tui` to reach a served agent.)",
            )
            return

        self._turns += 1
        self._refresh_status()
        await self._run_stream_turn(text)

    async def _run_stream_turn(self, text: str) -> None:
        """Drive one turn via ``transport.send_turn(text)`` and render it.

        Renders each ``TurnEvent``: "message" appends reply text (the streaming
        cursor ▋ shows while the turn is open), "error" surfaces a failure,
        "done" closes the turn. Called from the ``@work`` task in
        ``_send_to_agent`` — must NOT be decorated with ``@work`` (Workers
        cannot be awaited).

        Args:
            text: User prompt text.
        """
        if self._transcript is None or self._transport is None:
            return

        try:
            self._transcript.start_streaming(MessageRole.ASSISTANT)
            async for event in self._transport.send_turn(text):
                if event.kind == "message":
                    self._transcript.append_delta(event.text)
                elif event.kind == "error":
                    self._transcript.finish_streaming()
                    self._transcript.add_message(MessageRole.ERROR, event.text)
                    return
                elif event.kind == "done":
                    break
            self._transcript.finish_streaming()
        except Exception as exc:  # reason: fail-open — log + continue
            _logger.exception("Agent turn failed: %s", exc)
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
