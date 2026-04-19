"""InputComposer — multi-line input widget with slash-aware autocomplete.

Responsibilities:
- Accept user text input, including multi-line paste handling.
- Detect ``/``-prefixed input and show a completion popup from the
  ``SlashCommandCompleter``.
- Maintain an in-session command history (up/down arrow navigation).
- Emit a ``SubmitMessage`` Textual message when the user presses Enter
  on a non-empty line.
- Emit a ``CommandMessage`` for slash-prefixed input (known OR unknown)
  so the app can dispatch or show an "unknown command" error.

Design constraints:
- History is in-memory only (no disk persistence) — avoids secrets leaking
  to history files at this layer (vault/secret handling is in SecretInputModal).
- Max history depth: 200 entries per session (memory cap).
- The completion popup is a simple ``SelectionList``-style overlay that the
  app mounts/unmounts; it lives as a sibling widget, not inside InputComposer.
- Autocomplete is triggered only when the first non-whitespace character is
  ``/``.  Normal prose input does NOT trigger completions (avoids noise).
"""

from __future__ import annotations

from collections import deque
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from arctui.command_completer import Completion, SlashCommandCompleter

MAX_HISTORY = 200

# Minimum characters after ``/`` before completions appear.
_MIN_COMPLETION_CHARS = 1


class InputComposer(Static):
    """Input bar with slash-command awareness.

    Attributes
    ----------
    BINDINGS:
        Keyboard shortcuts handled inside the composer.

    Messages
    --------
    InputComposer.SubmitMessage
        Posted when the user submits a non-empty line.
    InputComposer.CommandMessage
        Posted when the submitted line starts with ``/``, whether or not
        the command resolves to a known CommandDef.  The app decides how
        to handle unknown commands (e.g. show an error message).
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("ctrl+j", "submit", "Submit", priority=True),
        Binding("up", "history_prev", "Previous", show=False),
        Binding("down", "history_next", "Next", show=False),
        Binding("escape", "close_completions", "Close", show=False),
    ]

    # ------------------------------------------------------------------
    # Messages emitted by this widget
    # ------------------------------------------------------------------

    class SubmitMessage(Message):
        """User pressed Enter with non-empty input.

        Attributes
        ----------
        text:
            The raw submitted text (with or without leading ``/``).
        """

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CommandMessage(Message):
        """User submitted a slash-prefixed line.

        Posted for both known and unknown commands so the app can dispatch
        the handler or display an "unknown command" error.

        Attributes
        ----------
        command:
            Canonical command name for known commands, or the raw token
            for unknown ones.  No leading slash.
        args:
            Remaining words after the command name.
        resolved:
            True if the command was found in the registry.
        """

        def __init__(self, command: str, args: list[str], *, resolved: bool) -> None:
            super().__init__()
            self.command = command
            self.args = args
            self.resolved = resolved

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._completer = SlashCommandCompleter()
        self._history: deque[str] = deque(maxlen=MAX_HISTORY)
        self._history_pos: int = -1          # -1 means "not browsing history"
        self._history_draft: str = ""         # save draft while browsing
        self._completions_visible: bool = False
        self._current_completions: list[Completion] = []

    def compose(self) -> ComposeResult:
        """Render the input line and completion overlay."""
        yield Input(
            placeholder="Type a message or /command…",
            id="main-input",
        )
        # Completion popup; hidden until triggered.
        option_list = OptionList(id="completion-list")
        option_list.display = False
        yield option_list

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Show or hide completions as the user types."""
        text = event.value
        self._update_completions(text)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in the Input widget (single-line submit path)."""
        self._do_submit(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Fill in selected completion."""
        idx = event.option_index
        if idx < len(self._current_completions):
            comp = self._current_completions[idx]
            inp = self.query_one("#main-input", Input)
            inp.value = f"/{comp.text} "
            inp.cursor_position = len(inp.value)
        self._hide_completions()

    # ------------------------------------------------------------------
    # Textual actions (bound to keys above)
    # ------------------------------------------------------------------

    def action_submit(self) -> None:
        """Ctrl+J submit path (works in multi-line scenarios)."""
        inp = self.query_one("#main-input", Input)
        self._do_submit(inp.value)

    def action_history_prev(self) -> None:
        """Navigate to older history entry."""
        inp = self.query_one("#main-input", Input)
        if not self._history:
            return
        if self._history_pos == -1:
            # Starting to browse — save current draft
            self._history_draft = inp.value
            self._history_pos = len(self._history) - 1
        elif self._history_pos > 0:
            self._history_pos -= 1
        inp.value = self._history[self._history_pos]
        inp.cursor_position = len(inp.value)
        self._hide_completions()

    def action_history_next(self) -> None:
        """Navigate to newer history entry."""
        inp = self.query_one("#main-input", Input)
        if self._history_pos == -1:
            return
        if self._history_pos < len(self._history) - 1:
            self._history_pos += 1
            inp.value = self._history[self._history_pos]
        else:
            # Reached newest — restore draft
            self._history_pos = -1
            inp.value = self._history_draft
        inp.cursor_position = len(inp.value)

    def action_close_completions(self) -> None:
        """Close the completion popup without selecting."""
        self._hide_completions()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _do_submit(self, raw: str) -> None:
        """Process a submitted line.

        For non-slash input: posts only SubmitMessage (routed to agent).
        For slash input: posts SubmitMessage AND CommandMessage.
            CommandMessage.resolved=True  → known command, dispatch handler.
            CommandMessage.resolved=False → unknown, app shows error.
        """
        text = raw.strip()
        if not text:
            return

        # Save to history (avoid duplicating last entry)
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_pos = -1
        self._history_draft = ""

        # Clear the input
        inp = self.query_one("#main-input", Input)
        inp.value = ""
        self._hide_completions()

        # Post SubmitMessage for all callers
        self.post_message(self.SubmitMessage(text))

        # For slash-prefixed input, always post a CommandMessage so the
        # app can dispatch known commands or show errors for unknown ones.
        if text.startswith("/"):
            parts = text.lstrip("/").split()
            if parts:
                cmd_name = parts[0]
                args = parts[1:]
                cmd = self._completer.resolve_exact(cmd_name)
                if cmd is not None:
                    # Known command: use canonical name.
                    self.post_message(self.CommandMessage(cmd.name, args, resolved=True))
                else:
                    # Unknown command: forward raw name so app can show error.
                    self.post_message(self.CommandMessage(cmd_name, args, resolved=False))

    def _update_completions(self, text: str) -> None:
        """Refresh the completion popup based on current input *text*."""
        if not text.startswith("/"):
            self._hide_completions()
            return

        after_slash = text[1:]
        if len(after_slash) < _MIN_COMPLETION_CHARS:
            self._hide_completions()
            return

        completions = self._completer.resolve(text)
        if not completions:
            self._hide_completions()
            return

        self._current_completions = completions
        option_list = self.query_one("#completion-list", OptionList)
        option_list.clear_options()
        for comp in completions:
            hint = f"  {comp.args_hint}" if comp.args_hint else ""
            label = f"/{comp.text}{hint}  — {comp.description}"
            option_list.add_option(Option(label))
        option_list.display = True
        self._completions_visible = True

    def _hide_completions(self) -> None:
        """Hide the completion popup."""
        if self._completions_visible:
            option_list = self.query_one("#completion-list", OptionList)
            option_list.display = False
            self._completions_visible = False
        self._current_completions = []
