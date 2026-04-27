"""Arc CLI entry point — slash-command REPL.

Two modes:
1. One-shot:  ``arc <command> [args…]`` — dispatches via registry and exits.
2. REPL:      ``arc`` with no arguments — starts interactive slash-command loop.

SDD §3.11 — Centralized slash-command registry.
"""

from __future__ import annotations

import sys


def _out(msg: str = "") -> None:
    """Write a line to stdout (avoids T201 lint on print statements)."""
    sys.stdout.write(msg + "\n")


def _err(msg: str) -> None:
    """Write a line to stderr."""
    sys.stderr.write(msg + "\n")


# ---------------------------------------------------------------------------
# One-shot dispatch
# ---------------------------------------------------------------------------


def _dispatch_oneshot(argv: list[str]) -> None:
    """Dispatch a single command from argv and exit.

    Handles ``arc <command> [args…]`` invocation.
    """
    from arccli.commands.registry import resolve_command

    raw_cmd = argv[0]
    args = argv[1:]

    cmd = resolve_command(raw_cmd)
    if cmd is None:
        _err(f"arc: unknown command '{raw_cmd}'. Run 'arc' for help.")
        sys.exit(1)

    if cmd.handler is None:
        _err(f"arc: command '{cmd.name}' has no handler registered.")
        sys.exit(1)

    try:
        cmd.handler(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        _err(f"arc: error in '{cmd.name}': {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


def _build_completer():  # type: ignore[return]
    """Build a prompt_toolkit WordCompleter from the command registry."""
    try:
        from prompt_toolkit.completion import WordCompleter

        from arccli.commands.render import autocomplete_dict

        words = autocomplete_dict()
        # Provide both /name and name for completion
        all_words = list(words.keys()) + [f"/{k}" for k in words.keys()]
        return WordCompleter(all_words, ignore_case=True, sentence=True)
    except ImportError:
        return None


def _run_repl() -> None:
    """Start the interactive slash-command REPL."""
    from arccli.commands.registry import resolve_command

    # Try to use prompt_toolkit for rich readline-like editing; fall back to
    # plain input() if not available (e.g., non-interactive CI environments).
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory

        completer = _build_completer()
        session: PromptSession = PromptSession(  # type: ignore[type-arg]
            history=InMemoryHistory(),
            completer=completer,
            complete_while_typing=False,
        )

        def _prompt() -> str:
            return session.prompt("arc> ")  # type: ignore[no-any-return]

    except ImportError:

        def _prompt() -> str:
            return input("arc> ")

    # Print welcome banner
    from arccli.commands.render import commands_by_category

    _out("Arc REPL — type /help for commands, /quit to exit.\n")
    by_cat = commands_by_category()
    for category, cmds in by_cat.items():
        names = "  ".join(f"/{c.name}" for c in cmds)
        _out(f"  {category}: {names}")
    _out()

    while True:
        try:
            raw = _prompt().strip()
        except (EOFError, KeyboardInterrupt):
            _out("\nBye.")
            break

        if not raw:
            continue

        # Split into command token and argument list
        parts = raw.split()
        raw_cmd = parts[0]
        args = parts[1:]

        cmd = resolve_command(raw_cmd)
        if cmd is None:
            _out(f"Unknown command '{raw_cmd}'. Type /help for available commands.")
            continue

        if cmd.handler is None:
            _out(f"Command '{cmd.name}' has no handler. This is a bug — please report it.")
            continue

        try:
            cmd.handler(args)
        except SystemExit:
            break
        except KeyboardInterrupt:
            _out("\n(interrupted)")
        except Exception as exc:
            _out(f"Error in '{cmd.name}': {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Primary entry point — dispatched by ``arc`` console script.

    With arguments: one-shot command dispatch via registry.
    Without arguments: interactive REPL.
    """
    # sys.argv[0] is the script name; real args start at [1].
    args = sys.argv[1:]

    if args:
        _dispatch_oneshot(args)
    else:
        _run_repl()


if __name__ == "__main__":
    main()
