"""Modal renderers for ArcTUI approval workflow.

Modals block the current agent turn until the user responds.  The calling
coroutine awaits ``modal.result`` which is resolved when the user presses
an action key inside the modal.

Available modals:
    ApprovalModal   — Yes/No gate for tool calls requiring human approval.
    ClarifyModal    — Free-text clarification from the user.
    SudoModal       — Elevated-privilege acknowledgment (shows what is requested).
    SecretInputModal — Masked text entry for passwords / API keys.

All modals follow the same contract:
  1. ``push_screen(modal)`` from the app.
  2. ``await screen_result`` returns the typed result.
  3. The modal ``dismiss()``es itself after the user responds.

Security notes:
- Approval prompts are logged as audit events by the calling layer, not here.
- SecretInputModal uses ``password=True`` on the Input widget; the value
  is returned as a plain string so the caller can hand it to the vault.
  It MUST NOT be logged or stored in session history (ASI-09).
- System prompt content is never surfaced in these modals (LLM-07).
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class ApprovalModal(ModalScreen[bool]):
    """Ask the user to approve or deny a pending tool call.

    Parameters
    ----------
    tool_name:
        Name of the tool awaiting approval.
    description:
        Human-readable description of what the tool will do.

    Resolves to ``True`` if the user approves, ``False`` if denied.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("y", "approve", "Yes"),
        Binding("n", "deny", "No"),
        Binding("escape", "deny", "Cancel"),
    ]

    def __init__(self, tool_name: str, description: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tool_name = tool_name
        self._description = description

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-container"):
            yield Static("TOOL APPROVAL REQUIRED", classes="modal-title")
            yield Label(f"Tool: {self._tool_name}", classes="modal-body")
            yield Label(self._description, classes="modal-body")
            yield Label("Press Y to approve, N to deny.", classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button("Yes [Y]", id="approve", variant="success")
                yield Button("No [N]", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks."""
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        """Keyboard shortcut: approve."""
        self.dismiss(True)

    def action_deny(self) -> None:
        """Keyboard shortcut: deny / cancel."""
        self.dismiss(False)


class ClarifyModal(ModalScreen[str]):
    """Prompt the user for a clarifying text response.

    Parameters
    ----------
    prompt:
        The question to show the user.

    Resolves to the user-entered string, or empty string if cancelled.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, prompt: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-container"):
            yield Static("CLARIFICATION NEEDED", classes="modal-title")
            yield Label(self._prompt, classes="modal-body")
            yield Input(placeholder="Your answer…", id="clarify-input")
            with Horizontal(classes="modal-buttons"):
                yield Button("Submit", id="submit", variant="primary")
                yield Button("Cancel", id="cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle submit / cancel buttons."""
        if event.button.id == "submit":
            inp = self.query_one("#clarify-input", Input)
            self.dismiss(inp.value)
        else:
            self.dismiss("")

    def action_cancel(self) -> None:
        """Keyboard shortcut: cancel."""
        self.dismiss("")


class SudoModal(ModalScreen[bool]):
    """Elevated-privilege acknowledgment modal.

    Shown when the agent requests an action that requires operator-level
    confirmation (e.g. writing outside the workspace, running a shell
    command, deleting data).

    Parameters
    ----------
    action_description:
        Plain-text description of the privileged action.

    Resolves to ``True`` if the user grants elevation, ``False`` otherwise.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("y", "grant", "Grant"),
        Binding("n", "deny", "Deny"),
        Binding("escape", "deny", "Cancel"),
    ]

    def __init__(self, action_description: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._description = action_description

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-container"):
            yield Static("ELEVATED PRIVILEGE REQUIRED", classes="modal-title")
            yield Label(self._description, classes="modal-body")
            yield Label("Press Y to grant, N to deny.", classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button("Grant [Y]", id="grant", variant="warning")
                yield Button("Deny [N]", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks."""
        self.dismiss(event.button.id == "grant")

    def action_grant(self) -> None:
        """Keyboard shortcut: grant elevation."""
        self.dismiss(True)

    def action_deny(self) -> None:
        """Keyboard shortcut: deny elevation."""
        self.dismiss(False)


class SecretInputModal(ModalScreen[str]):
    """Masked text entry for passwords and API keys.

    The entered value is returned as a plain string.  The caller is
    responsible for routing it to the vault; it MUST NOT be logged
    or stored in session messages (security note in module docstring).

    Parameters
    ----------
    prompt:
        The label shown above the masked input.

    Resolves to the entered secret string, or empty string if cancelled.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, prompt: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-container"):
            yield Static("SECRET INPUT", classes="modal-title")
            yield Label(self._prompt, classes="modal-body")
            yield Input(placeholder="", password=True, id="secret-input")
            with Horizontal(classes="modal-buttons"):
                yield Button("Submit", id="submit", variant="primary")
                yield Button("Cancel", id="cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle submit / cancel buttons."""
        if event.button.id == "submit":
            inp = self.query_one("#secret-input", Input)
            self.dismiss(inp.value)
        else:
            self.dismiss("")

    def action_cancel(self) -> None:
        """Keyboard shortcut: cancel."""
        self.dismiss("")
