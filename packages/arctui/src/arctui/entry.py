"""CLI entry point for ``arc tui``.

Registers a ``tui`` CommandDef in the arccli registry and provides ``main()``,
the ``arc-tui`` script entry point.

SPEC-058 Phase 3: ``arc tui`` is a *viewpoint* onto a served agent, never an
agent owner. It resolves a gateway endpoint (attach to one that's running, or
spawn ``arc ui start`` and attach), opens a :class:`GatewayChatClient`, and runs
the TUI against it. It does not construct an ``ArcAgent`` — that would grab a
second single-writer WORM lock (the collision ``arcgateway.fleet`` warns of).

Usage::

    arc tui                          # attach to a local gateway, or spawn one
    arc tui --agent employee         # pick a roster agent
    arc tui --team-root ~/.arc/work  # roster + spawn root
    arc tui --url http://host:8420 --token <viewer-token>   # attach to a remote gateway
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_logger = logging.getLogger("arctui.entry")

_DEFAULT_TEAM_ROOT = Path.home() / ".arc" / "team"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420


def _tui_handler(args: list[str]) -> None:
    """Launch ArcTUI — arccli CommandDef handler."""
    main(args)


def _register_tui_command() -> None:
    """Add the ``tui`` CommandDef to COMMAND_REGISTRY (idempotent)."""
    from arccli.commands.registry import COMMAND_REGISTRY, CommandDef

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
    """Return the value after ``flag`` in ``args`` (``--flag value`` or ``--flag=v``)."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def _resolve_team_root(args: list[str]) -> Path:
    """Team root for roster lookup + gateway spawn.

    Accepts ``--root`` (the short form used for a per-project coding folder, e.g.
    ``arc tui --root .arc/coding``) or ``--team-root``; falls back to the default.
    """
    troot = _flag_value(args, "--root") or _flag_value(args, "--team-root")
    return Path(troot).expanduser() if troot else _DEFAULT_TEAM_ROOT


async def _resolve_endpoint(args: list[str], agent_id: str, team_root: Path) -> object:
    """Resolve the gateway to attach to (spawning one only if needed)."""
    from arctui.serve import Endpoint, GatewayNeedsTokenError, ensure_gateway, read_persisted_token

    token = _flag_value(args, "--token")
    url = _flag_value(args, "--url")

    if url:
        # Explicit remote/local gateway — attach only, never spawn.
        tok = token or read_persisted_token()
        if not tok:
            raise GatewayNeedsTokenError(url)
        return Endpoint(url.rstrip("/"), tok, agent_id, spawned=False)

    host = _flag_value(args, "--host") or _DEFAULT_HOST
    port = int(_flag_value(args, "--port") or _DEFAULT_PORT)
    return await ensure_gateway(
        agent_id=agent_id,
        team_root=team_root,
        host=host,
        port=port,
        token=token,
    )


def _maybe_prompt_trust(config_path: Path, cwd: Path) -> None:
    """Offer to trust ``cwd`` for the agent (grant read/write), persisting to its toml.

    No-op when the folder is already trusted. On a non-interactive stdin the folder is
    left untrusted (secure default). Anything but an explicit yes declines. Runs before
    the gateway serves the agent so the spawn path picks the grant up immediately.
    """
    from arctui.trust import folder_is_trusted, grant_folder

    if folder_is_trusted(config_path, cwd):
        return
    if not sys.stdin.isatty():
        _logger.warning("Folder %s not trusted (non-interactive); agent has no access.", cwd)
        return
    prompt = f"Trust this folder for the agent to read/write?\n  {cwd}\n[y/N] "
    if input(prompt).strip().lower() in ("y", "yes"):
        grant_folder(config_path, cwd)
        _logger.warning("Trusted %s (added to allowed_paths); restart a running gateway.", cwd)
    else:
        _logger.warning("Folder %s NOT trusted; the agent cannot read/write it.", cwd)


async def _build_transport(
    args: list[str],
) -> tuple[object | None, str | None, str | None, str | None]:
    """Resolve agent + gateway and open a chat transport.

    Returns ``(transport, message, agent_label, gateway_label)``. On success
    ``message`` is None; on failure ``transport`` is None and ``message`` is the
    reason the TUI shows in no-agent mode.
    """
    from arctui.gateway_client import GatewayChatClient
    from arctui.roster import resolve_agent

    team_root = _resolve_team_root(args)
    res = resolve_agent(name=_flag_value(args, "--agent"), team_root=team_root)
    if res.selected is None:
        available = ", ".join(a.agent_id for a in res.candidates)
        if res.reason == "empty":
            msg = f"No agents under {team_root}. Run `arc agent create <name>` first."
        elif res.reason == "ambiguous":
            msg = f"Multiple agents found; pass --agent <id>. Available: {available}"
        else:
            msg = f"No agent named {_flag_value(args, '--agent')!r}. Available: {available}"
        return None, msg, None, None

    agent_id = res.selected.agent_id
    # Folder-trust: before the gateway serves the agent, offer to grant the launch
    # directory so the coder can read/write this project (persisted to its toml).
    _maybe_prompt_trust(res.selected.config_path, Path.cwd())
    try:
        endpoint = await _resolve_endpoint(args, agent_id, team_root)
        base_url: str = endpoint.base_url  # type: ignore[attr-defined]
        client = GatewayChatClient(
            base_url,
            endpoint.agent_id,  # type: ignore[attr-defined]
            endpoint.token,  # type: ignore[attr-defined]
        )
        await client.connect()
    except Exception as exc:  # reason: fail-open — boot no-agent with the reason
        _logger.error("Could not attach to gateway: %s", exc)
        return None, f"Could not attach to a gateway: {exc}", None, None
    return client, None, agent_id, base_url


async def _run(args: list[str]) -> None:
    """Resolve a transport, then run the TUI against it."""
    transport, message, agent_label, gateway_label = await _build_transport(args)
    if message is not None:
        _logger.info("%s Starting in no-agent mode.", message)

    from arctui.app import ArcTUI

    app = ArcTUI(
        transport=transport,  # type: ignore[arg-type]  # ChatTransport | None
        agent_label=agent_label,
        gateway_label=gateway_label,
    )
    try:
        await app.run_async()
    finally:
        if transport is not None:
            await transport.aclose()  # type: ignore[attr-defined]


def main(args: list[str] | None = None) -> None:
    """Script entry point for ``arc-tui``.

    Exits 0 on clean shutdown, 1 on unexpected error.
    """
    argv = args if args is not None else sys.argv[1:]
    try:
        asyncio.run(_run(argv))
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # reason: top-level guard — log + non-zero exit
        _logger.error("ArcTUI failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
