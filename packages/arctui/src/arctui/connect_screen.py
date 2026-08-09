"""SPEC-064 T-030 — the modal screens behind ``/connect`` and ``/connections``.

``arc connector add`` collects credentials with ``getpass``, which reads the
controlling terminal that Textual has taken over, so routing ``/connect`` to the
arccli registry handler would hang on a prompt nobody can see. The TUI therefore
owns this one verb and collects into masked inputs instead (D-586) — everything
after collection is :class:`arcagent.connections.Connections`, the one seam the
CLI and the web drive too.

**Order of the flow is the point.** Pick a bundle, name the instance, and see what
is about to happen — the credentials it will ask for, the approval mode it will
run under, and any prerequisite this machine lacks — *before* the first field
appears. An unmet prerequisite ends the flow with the instruction to run: a host
change is the operator's decision and the TUI never makes it (REQ-262).

**What happens to a credential here.** It is typed into a ``password=True`` input,
read once, handed to the install, and the field is cleared. It never reaches the
transcript, a log line, an exception message, or the composer's command history —
which is the reason a modal exists rather than ``/connect <extension> <token>``.
Only fields the bundle declares sensitive are masked: a base URL typed behind dots
is a typo nobody can see until the probe fails, and there is no secret there to
protect.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from arcagent.connections import CatalogEntry, Connections, ConnectorPlan, ExtensionError
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList

from arctui import connect
from arctui.theme import (
    COLOR_ACCENT_BLUE,
    COLOR_BG_BORDER,
    COLOR_BG_PANEL,
    COLOR_TEXT_MUTED,
)

_logger = logging.getLogger("arctui.connect_screen")

#: DOM ids accept letters, digits, ``_`` and ``-`` only; a manifest may declare a
#: secret name outside that set, and an invalid id would raise instead of render.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

#: How much of a catalog entry's ``error`` fits on one hint line. A pydantic
#: validation reason runs to many lines and would push the picker off the screen.
_HINT_REASON_CHARS = 90


def _one_line(reason: str) -> str:
    """Collapse a multi-line refusal onto one readable line."""
    collapsed = " ".join(reason.split())
    if len(collapsed) <= _HINT_REASON_CHARS:
        return collapsed
    return f"{collapsed[:_HINT_REASON_CHARS]}…"


_MODAL_CSS = f"""
ConnectScreen, ConnectionsScreen {{
    align: center middle;
}}

#connect-dialog, #connections-dialog {{
    width: 80%;
    max-width: 100;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    border: solid {COLOR_BG_BORDER};
    background: {COLOR_BG_PANEL};
}}

#connect-title, #connections-title {{
    color: {COLOR_ACCENT_BLUE};
    text-style: bold;
    padding-bottom: 1;
}}

#connect-plan, #connect-status, #connections-status, #connect-hint {{
    color: {COLOR_TEXT_MUTED};
    padding: 1 0;
}}

#connect-bundles, #connections-list {{
    height: auto;
    max-height: 12;
    border: solid {COLOR_BG_BORDER};
}}

#connect-actions, #connections-actions {{
    height: auto;
    padding-top: 1;
}}
"""


@dataclass(frozen=True)
class ConnectOutcome:
    """What the transcript should say once the modal closes.

    Carries coordinates only — the extension, the instance, the step that refused.
    A value the operator typed is never part of it.
    """

    ok: bool
    lines: tuple[str, ...]


class ConnectScreen(ModalScreen[ConnectOutcome | None]):
    """Walk the operator from "which bundle" to a probed, persisted connection."""

    CSS: ClassVar[str] = _MODAL_CSS

    BINDINGS: ClassVar[list[Any]] = [("escape", "cancel", "Cancel")]

    def __init__(self, connections: Connections) -> None:
        super().__init__()
        self._connections = connections
        self._bundles: tuple[CatalogEntry, ...] = ()
        self._plan: ConnectorPlan | None = None
        self._fields: list[tuple[str, Input]] = []

    def compose(self) -> ComposeResult:
        """The whole flow in one dialog — steps reveal themselves rather than replace it."""
        with Vertical(id="connect-dialog"):
            yield Label("Connect an account", id="connect-title")
            yield Label("", id="connect-hint")
            yield OptionList(id="connect-bundles")
            yield Input(placeholder="name for this connected account", id="connect-instance")
            yield Label("", id="connect-plan")
            yield Vertical(id="connect-secrets")
            yield Label("", id="connect-status")
            with Horizontal(id="connect-actions"):
                yield Button("Review", id="connect-review", variant="primary")
                yield Button("Connect", id="connect-install", variant="success", disabled=True)
                yield Button("Cancel", id="connect-cancel")

    def on_mount(self) -> None:
        """Fill the picker from the extension search path."""
        try:
            found = self._connections.catalog()
        except ExtensionError as exc:
            self.dismiss(ConnectOutcome(False, (f"Could not read the bundles: {exc.message}",)))
            return
        except Exception as exc:  # reason: a picker that cannot list has nothing to offer
            _logger.exception("could not list extension bundles")
            self.dismiss(
                ConnectOutcome(False, (f"Could not read the bundles: {type(exc).__name__}.",))
            )
            return

        # A bundle whose manifest will not parse cannot be installed, so it is named
        # in the hint rather than offered as a row an operator can choose and fail on.
        self._bundles = tuple(entry for entry in found if not entry.error)
        options = self.query_one("#connect-bundles", OptionList)
        for entry in self._bundles:
            options.add_option(f"{entry.name}  {entry.version}  —  {entry.description}")
        self.query_one("#connect-hint", Label).update(self._hint(found))
        if self._bundles:
            # Pre-select the first row: an OptionList highlights nothing until it is
            # focused, and "Review" with nothing highlighted is a dead button.
            options.highlighted = 0
            options.focus()
        else:
            self.query_one("#connect-review", Button).disabled = True

    def _hint(self, found: tuple[CatalogEntry, ...]) -> str:
        """Say where the bundles came from, and name any that would not parse."""
        roots = (
            ", ".join(str(root) for root in self._connections.world.extension_roots)
            or "(no extensions directory)"
        )
        lines = [f"Bundles on the search path: {roots}"]
        if not self._bundles:
            lines.append("Nothing to connect yet — put a bundle on the search path.")
        lines += [f"unreadable: {e.name} — {_one_line(e.error)}" for e in found if e.error]
        return "\n".join(lines)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the three buttons. Cancel closes without a transcript line."""
        if event.button.id == "connect-cancel":
            self.dismiss(None)
        elif event.button.id == "connect-review":
            await self._review()
        elif event.button.id == "connect-install":
            self._start_install()

    def action_cancel(self) -> None:
        """Escape closes the modal, leaving nothing installed and nothing said."""
        self.dismiss(None)

    async def _review(self) -> None:
        """Show what the install will do, then ask for the credentials it declared."""
        choice = self._chosen_bundle()
        instance = self.query_one("#connect-instance", Input).value.strip()
        if choice is None:
            self._status("Pick a bundle first.")
            return
        if not instance:
            self._status("Name this connected account first.")
            return

        try:
            plan = self._connections.plan(choice.name, instance)
        except ExtensionError as exc:
            self.dismiss(
                ConnectOutcome(False, (f"Could not connect {choice.name}: {exc.message}",))
            )
            return
        except Exception as exc:  # reason: the detail belongs in the log, not on screen
            _logger.exception("could not plan a connection to %s", choice.name)
            self.dismiss(
                ConnectOutcome(
                    False,
                    (
                        f"Could not connect {choice.name}: {type(exc).__name__}. "
                        "Nothing was installed; see the log for detail.",
                    ),
                )
            )
            return

        if plan.unsatisfied_host:
            self.dismiss(ConnectOutcome(False, connect.host_refusal(plan)))
            return

        self._plan = plan
        self.query_one("#connect-plan", Label).update(
            "\n".join(connect.plan_summary(plan, self._connections.world.env_file))
        )
        await self._ask_for_credentials(plan)

    async def _ask_for_credentials(self, plan: ConnectorPlan) -> None:
        """Mount one field per declared value, masking the ones that are credentials.

        The manifest decides which, per field. Masking a base URL protects nothing
        and hides the only thing an operator can check before the probe runs.
        """
        container = self.query_one("#connect-secrets", Vertical)
        for declared in plan.secrets:
            field = Input(
                placeholder=declared.prompt or f"Value for {declared.name}",
                password=declared.sensitive,
                id=f"connect-secret-{_ID_UNSAFE.sub('-', declared.name)}",
            )
            self._fields.append((declared.name, field))
            await container.mount(field)

        self.query_one("#connect-review", Button).disabled = True
        self.query_one("#connect-bundles", OptionList).disabled = True
        self.query_one("#connect-instance", Input).disabled = True
        self.query_one("#connect-install", Button).disabled = False
        self._status("Fill the fields, then Connect. Credentials are hidden and not shown again.")

    def _start_install(self) -> None:
        """Read the fields once, clear them, and hand the values to the worker."""
        if self._plan is None:
            return
        values = {name: field.value for name, field in self._fields}
        for _name, field in self._fields:
            field.value = ""
        self.query_one("#connect-install", Button).disabled = True
        self._status(f"Connecting to {self._plan.extension} — opening the connection…")
        self._run_install(self._plan, values)

    @work(exclusive=True)
    async def _run_install(self, plan: ConnectorPlan, values: dict[str, str]) -> None:
        """Drive the shared install path off the UI's critical path.

        An install opens a live connection, so it is the one step that can take
        seconds. Running it as a Textual worker keeps the modal responsive and the
        status line honest about what is happening.
        """
        try:
            report = await self._connections.install(plan, values)
        except ExtensionError as exc:
            self._finish(
                ConnectOutcome(False, (f"Could not connect {plan.extension}: {exc.message}",))
            )
            return
        except Exception as exc:  # reason: the detail belongs in the log, not on screen
            _logger.exception("connector install failed for %s", plan.extension)
            self._finish(
                ConnectOutcome(
                    False,
                    (
                        f"Could not connect {plan.extension}: {type(exc).__name__}. "
                        "Nothing was installed; see the log for detail.",
                    ),
                )
            )
            return
        self._finish(ConnectOutcome(True, connect.install_summary(report, self._connections)))

    def _finish(self, outcome: ConnectOutcome) -> None:
        """Close with a result, unless the operator already closed the screen."""
        if self.is_running:
            self.dismiss(outcome)

    def _chosen_bundle(self) -> CatalogEntry | None:
        index = self.query_one("#connect-bundles", OptionList).highlighted
        if index is None or index >= len(self._bundles):
            return None
        return self._bundles[index]

    def _status(self, message: str) -> None:
        self.query_one("#connect-status", Label).update(message)


class ConnectionsScreen(ModalScreen[None]):
    """List what this agent already has connected, and prove one is live."""

    CSS: ClassVar[str] = _MODAL_CSS

    BINDINGS: ClassVar[list[Any]] = [("escape", "cancel", "Close")]

    def __init__(self, connections: Connections) -> None:
        super().__init__()
        self._connections = connections
        self._instances: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="connections-dialog"):
            yield Label("Connected accounts", id="connections-title")
            yield OptionList(id="connections-list")
            yield Label("", id="connections-status")
            with Horizontal(id="connections-actions"):
                yield Button("Probe", id="connections-probe", variant="primary")
                yield Button("Close", id="connections-close")

    def on_mount(self) -> None:
        """Read the agent's own config — the same blocks the connector runtime binds."""
        try:
            configured = self._connections.installed()
        except ExtensionError as exc:
            self._status(exc.message)
            return

        self._instances = tuple(sorted(configured))
        options = self.query_one("#connections-list", OptionList)
        for name in self._instances:
            block = configured[name]
            options.add_option(f"{name}  ·  {block.extension}  ·  approval {block.approval}")
        if not self._instances:
            self._status("Nothing connected yet. Use /connect to add one.")
            self.query_one("#connections-probe", Button).disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connections-close":
            self.dismiss(None)
        elif event.button.id == "connections-probe":
            self._start_probe()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _start_probe(self) -> None:
        index = self.query_one("#connections-list", OptionList).highlighted
        if index is None or index >= len(self._instances):
            return
        instance = self._instances[index]
        self.query_one("#connections-probe", Button).disabled = True
        self._status(f"Probing '{instance}' — opening the connection…")
        self._run_probe(instance)

    @work(exclusive=True)
    async def _run_probe(self, instance: str) -> None:
        """Probing opens a live connection, so it runs off the UI's critical path."""
        try:
            result = await self._connections.probe(instance)
        except ExtensionError as exc:
            self._settle(f"'{instance}': {exc.message}")
            return
        except Exception as exc:  # reason: a probe reports failures, it does not raise them
            _logger.exception("probe failed for instance %s", instance)
            self._settle(f"'{instance}' could not be probed: {type(exc).__name__}.")
            return
        verdict = "is reachable" if result.reachable else "did not answer"
        tools = ", ".join(spec.name for spec in result.tools) or "(none served)"
        self._settle(f"'{instance}' {verdict}. {result.detail}\n  tools: {tools}")

    def _settle(self, message: str) -> None:
        if not self.is_running:
            return
        self._status(message)
        self.query_one("#connections-probe", Button).disabled = False

    def _status(self, message: str) -> None:
        self.query_one("#connections-status", Label).update(message)


__all__ = ["ConnectOutcome", "ConnectScreen", "ConnectionsScreen"]
