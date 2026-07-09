"""Tests for the bare-``arc`` REPL entry (arccli.main).

F1: with a non-TTY stdin (pipe, CI, ``arc < file``) the REPL must not enter the
raw-mode prompt_toolkit session — that crashes with a ``KeyError`` from
``add_reader``. It must print help and exit 0 cleanly instead.

The arccli package is installed editable against the MAIN checkout, so these
tests front-load the worktree ``packages/arccli/src`` to exercise local edits.
"""

from __future__ import annotations

import pathlib
import sys

_WT_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
if _WT_SRC not in sys.path:
    sys.path.insert(0, _WT_SRC)

import arccli.main as main_mod


class _FakeStdin:
    """Minimal stdin stand-in whose ``isatty`` is configurable."""

    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestReplNonTty:
    """Non-interactive stdin must print help and return, never enter the REPL."""

    def test_non_tty_prints_help_and_does_not_build_prompt_session(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(main_mod.sys, "stdin", _FakeStdin(tty=False))

        # If the REPL wrongly enters the interactive path it will construct a
        # PromptSession — make that a hard failure so the test is deterministic
        # rather than depending on prompt_toolkit's non-tty crash.
        import prompt_toolkit

        def _boom(*_args, **_kwargs):
            raise AssertionError("PromptSession must not be built on non-tty stdin")

        monkeypatch.setattr(prompt_toolkit, "PromptSession", _boom)

        # Must not raise, and must return cleanly (exit 0 == normal return).
        main_mod._run_repl()

        out = capsys.readouterr().out
        # Help output lists slash commands (e.g. /help, /version).
        assert "/help" in out or "/version" in out

    def test_tty_still_enters_interactive_session(self, monkeypatch) -> None:
        # With a real TTY the REPL must still build the interactive session —
        # the non-tty guard must not change interactive behavior.
        monkeypatch.setattr(main_mod.sys, "stdin", _FakeStdin(tty=True))

        import prompt_toolkit

        entered = {"built": False}

        def _sentinel(*_args, **_kwargs):
            entered["built"] = True
            raise KeyboardInterrupt  # bail out of the REPL immediately

        monkeypatch.setattr(prompt_toolkit, "PromptSession", _sentinel)

        try:
            main_mod._run_repl()
        except KeyboardInterrupt:
            pass
        assert entered["built"], "TTY stdin must still enter the interactive REPL"
