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
from pathlib import Path

_logger = logging.getLogger("arctui.entry")


def _tui_handler(args: list[str]) -> None:
    """Launch ArcTUI — arccli CommandDef handler.

    Imported lazily from the registry so Textual is only loaded when the
    user explicitly asks for the TUI.  Missing arcagent config is handled
    gracefully: the TUI boots in no-agent mode with a clear message.
    """
    main(args)


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


def _flag_value(args: list[str], flag: str) -> str | None:
    """Return the value after ``flag`` in ``args`` (``--flag value``), or None."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def _resolve_agent_config(
    args: list[str], *, cwd: Path, team_root: Path | None = None
) -> tuple[Path | None, str | None]:
    """Resolve which agent config to load (SPEC-058 T-765, REQ-141/143).

    Selects from the arc roster (~/.arc/team, or ``--team-root``) with an optional
    ``--agent <id>``; falls back to a ``arcagent.toml`` in ``cwd`` when launched
    from inside an agent dir with no roster. Returns ``(config_path, None)`` to
    load, or ``(None, message)`` when the operator must choose / create an agent.
    """
    from arctui.roster import resolve_agent

    name = _flag_value(args, "--agent")
    troot_arg = _flag_value(args, "--team-root")
    troot = Path(troot_arg) if troot_arg else team_root
    res = resolve_agent(name=name, team_root=troot)

    if res.selected is not None:
        return res.selected.config_path, None
    if res.reason == "empty":
        cwd_toml = cwd / "arcagent.toml"
        if cwd_toml.is_file():
            return cwd_toml, None
        return None, "No agents found. Run `arc agent create <name>` to create one."
    available = ", ".join(a.agent_id for a in res.candidates)
    if res.reason == "ambiguous":
        return None, f"Multiple agents found; pass --agent <id>. Available: {available}"
    if res.reason == "unknown":
        return None, f"No agent named {name!r}. Available: {available}"
    return None, None


def _apply_folder_trust(config: object, cwd: Path) -> None:
    """Prompt for folder trust and, on confirmation, grant it session-scoped (REQ-142).

    Runs before ``ArcAgent`` startup so the grant is visible to the runtime. The
    grant is in-memory only (never written to arcagent.toml). Non-interactive
    stdin → do NOT grant (secure default). Anything but an explicit yes declines.
    """
    from arctui.trust import folder_needs_trust, grant_folder

    if not folder_needs_trust(config, cwd):  # type: ignore[arg-type]  # ArcAgentConfig
        return
    if not sys.stdin.isatty():
        _logger.warning("Folder %s not trusted (non-interactive); agent cannot access it.", cwd)
        return
    prompt = f"Trust this folder for the agent to read/write?\n  {cwd}\n[y/N] "
    answer = input(prompt).strip().lower()
    if answer in ("y", "yes"):
        grant_folder(config, cwd)  # type: ignore[arg-type]  # ArcAgentConfig
        _logger.warning("Trusted %s for this session (read/write granted).", cwd)
    else:
        _logger.warning("Folder %s NOT trusted; the agent cannot read/write it this session.", cwd)


def _load_agent(args: list[str] | None = None) -> object | None:
    """Load the resolved ArcAgent (or None for no-agent mode) — see _resolve_agent_config.

    Returns None if arcagent is not installed, no agent is resolvable, or the
    config is invalid; the TUI boots without an agent and shows the reason.
    """
    try:
        from arcagent.core.agent import ArcAgent
        from arcagent.core.config import load_config

        config_path, message = _resolve_agent_config(args or [], cwd=Path.cwd())
        if config_path is None:
            _logger.info("%s; starting in no-agent mode.", message or "No agent resolved")
            return None

        config = load_config(config_path)
        _apply_folder_trust(config, Path.cwd())
        return ArcAgent(config, config_path=config_path)
    except ImportError:
        _logger.debug("arcagent not installed; starting in no-agent mode.")
        return None
    except Exception as exc:  # reason: fail-open — log + continue
        _logger.warning("Failed to load ArcAgent: %s; starting in no-agent mode.", exc)
        return None


async def _run_tui(agent: object | None) -> None:
    """Async entrypoint: start agent if available, then run TUI."""
    if agent is not None:
        startup = getattr(agent, "startup", None)
        if callable(startup):
            try:
                await startup()
            except Exception as exc:  # reason: fail-open — log + continue
                _logger.error("ArcAgent startup failed: %s", exc)
                agent = None

    from arctui.app import ArcTUI

    app = ArcTUI(agent=agent)
    await app.run_async()


def main(args: list[str] | None = None) -> None:
    """Script entry point for ``arc-tui``.

    Resolves the agent from the roster (or ``--agent``/cwd), then runs the TUI.
    Exits with code 0 on clean shutdown, 1 on unexpected error.
    """
    try:
        agent = _load_agent(args if args is not None else sys.argv[1:])
        asyncio.run(_run_tui(agent))
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # reason: fail-open — log + continue
        _logger.error("ArcTUI failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
