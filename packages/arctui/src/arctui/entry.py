"""CLI entry point for ``arc tui``.

Registers a CommandDef in the arccli registry and provides the
``main()`` function invoked by ``arc-tui`` script entry point.

The CommandDef is added lazily (at import time of this module) into
``arccli.commands.registry.COMMAND_REGISTRY``.  The handler imports
arctui lazily so that missing optional deps (Textual) do not crash
unrelated arc subcommands.

Usage::

    arc tui             # via arccli REPL
    arc-tui             # via installed script entry point
"""

from __future__ import annotations

import asyncio
import logging
import sys

_logger = logging.getLogger("arctui.entry")


def _tui_handler(args: list[str]) -> None:
    """Launch ArcTUI — arccli CommandDef handler.

    Imported lazily from the registry so Textual is only loaded when the
    user explicitly asks for the TUI.  Missing arcagent config is handled
    gracefully: the TUI boots in no-agent mode with a clear message.
    """
    main()


def _register_tui_command() -> None:
    """Add the ``tui`` CommandDef to COMMAND_REGISTRY.

    Called once at import time.  Idempotent: if ``tui`` is already
    registered (e.g. from another import path) the duplicate is skipped.
    """
    from arccli.commands.registry import COMMAND_REGISTRY, CommandDef

    # Guard against double-registration on repeated imports.
    for cmd in COMMAND_REGISTRY:
        if cmd.name == "tui":
            return

    COMMAND_REGISTRY.append(
        CommandDef(
            name="tui",
            description="Launch Arc terminal UI (Textual)",
            category="Session",
            cli_only=True,
            handler=_tui_handler,
        )
    )


# Register on module import so ``arc tui`` is available immediately.
_register_tui_command()


def _load_agent() -> object | None:
    """Attempt to load an ArcAgent from the default config path.

    Returns None (no-agent mode) if arcagent is not installed or the
    config is missing/invalid.  The TUI boots without an agent and shows
    a status message explaining what is missing.
    """
    try:
        from pathlib import Path

        from arcagent.core.agent import ArcAgent
        from arcagent.core.config import load_config

        config_path = Path("arcagent.toml")
        if not config_path.exists():
            _logger.info("No arcagent.toml found at %s; starting in no-agent mode.", config_path)
            return None

        config = load_config(config_path)
        agent = ArcAgent(config, config_path=config_path)
        return agent
    except ImportError:
        _logger.debug("arcagent not installed; starting in no-agent mode.")
        return None
    except Exception as exc:
        _logger.warning("Failed to load ArcAgent: %s; starting in no-agent mode.", exc)
        return None


async def _run_tui(agent: object | None) -> None:
    """Async entrypoint: start agent if available, then run TUI."""
    if agent is not None:
        startup = getattr(agent, "startup", None)
        if callable(startup):
            try:
                await startup()
            except Exception as exc:
                _logger.error("ArcAgent startup failed: %s", exc)
                agent = None

    from arctui.app import ArcTUI

    app = ArcTUI(agent=agent)
    await app.run_async()


def main() -> None:
    """Script entry point for ``arc-tui``.

    Loads ArcAgent from arcagent.toml if present, then runs the TUI.
    Exits with code 0 on clean shutdown, 1 on unexpected error.
    """
    try:
        agent = _load_agent()
        asyncio.run(_run_tui(agent))
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        _logger.error("ArcTUI failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
