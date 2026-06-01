"""Run an arcagent as a long-lived daemon.

Usage::

    python -m arcagent serve <agent_dir> [--inbound stdin|none]

The daemon:

- Loads ``<agent_dir>/arcagent.toml`` into ``ArcAgentConfig``.
- Calls ``agent.startup()`` (modules connect, telemetry starts).
- Listens for messages on the configured ``--inbound`` source.
  * ``stdin``: line-delimited prompts on stdin; agent replies on stdout.
  * ``none``: no inbound; the daemon idles waiting for SIGINT/SIGTERM
    while remaining modules (e.g. team messaging poll, telegram bot)
    do their work.
- Catches SIGINT/SIGTERM, calls ``agent.shutdown()``, and writes a
  clean-shutdown marker at ``<agent_dir>/.arc-shutdown.json`` so a
  supervisor can tell "agent exited cleanly" from "agent crashed and
  needs restart".

The CLI is intentionally thin — it composes existing public arcagent
methods without reaching for internals. This is the supported entry
point for subprocess-per-agent isolation (Gap log #8).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from arcagent.core.agent import ArcAgent
from arcagent.core.config import ArcAgentConfig

_logger = logging.getLogger("arcagent.serve")

_INBOUND_CHOICES = ("stdin", "none")
_SHUTDOWN_MARKER_NAME = ".arc-shutdown.json"


def _load_config(agent_dir: Path) -> tuple[ArcAgentConfig, Path]:
    """Resolve and parse ``<agent_dir>/arcagent.toml``."""
    config_path = agent_dir / "arcagent.toml"
    if not config_path.is_file():
        msg = (
            f"arcagent.toml not found at {config_path}. "
            "Pass an agent directory that contains arcagent.toml."
        )
        raise FileNotFoundError(msg)
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = ArcAgentConfig.model_validate(raw)
    return config, config_path


def _write_shutdown_marker(agent_dir: Path, *, status: str, reason: str) -> None:
    """Record a clean-shutdown receipt next to arcagent.toml.

    Supervisors poll for this file's presence + mtime to decide whether
    a restart is warranted. ``status`` is ``"clean"`` for normal exit
    and ``"crashed"`` for any unhandled exception path; ``reason`` is a
    short human-readable code (``"sigterm"``, ``"sigint"``, ``"eof"``,
    ``"exception"``).
    """
    payload = {
        "status": status,
        "reason": reason,
        "exit_at": time.time(),
    }
    marker = agent_dir / _SHUTDOWN_MARKER_NAME
    try:
        marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        _logger.warning("Could not write shutdown marker to %s", marker, exc_info=True)


async def _run_stdin_loop(agent: ArcAgent, shutdown_event: asyncio.Event) -> str:
    """Read line-delimited prompts from stdin and stream replies to stdout.

    Returns the shutdown reason: ``"eof"`` when stdin closes,
    ``"signal"`` when SIGINT/SIGTERM fires.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while not shutdown_event.is_set():
        read_task = asyncio.create_task(reader.readline())
        wait_task = asyncio.create_task(shutdown_event.wait())
        _done, _pending = await asyncio.wait(
            {read_task, wait_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown_event.is_set():
            read_task.cancel()
            return "signal"
        wait_task.cancel()
        raw = read_task.result()
        if not raw:
            return "eof"
        prompt = raw.decode("utf-8", errors="replace").strip()
        if not prompt:
            continue
        try:
            response = await agent.run_collected(prompt, session_key="serve:stdin")
        except Exception:  # reason: fail-open — log + continue serving
            _logger.exception("agent run failed; continuing serve loop")
            sys.stdout.write("[error] see log\n")
            sys.stdout.flush()
            continue
        text = _stringify_response(response)
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    return "signal"


async def _run_none_loop(shutdown_event: asyncio.Event) -> str:
    """Idle until SIGINT/SIGTERM. Background modules keep running."""
    await shutdown_event.wait()
    return "signal"


def _stringify_response(response: Any) -> str:
    """Coerce a ``RunResult`` (or text) into a single line of text."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response.replace("\n", " ")
    text = getattr(response, "content", None)
    if isinstance(text, str):
        return text.replace("\n", " ")
    return str(response).replace("\n", " ")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to ``event.set()`` on Unix; fall back to default on Windows."""

    def _handler() -> None:
        event.set()

    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), _handler)
        except NotImplementedError:
            # Windows: add_signal_handler is unsupported. The default
            # KeyboardInterrupt path still fires for SIGINT.
            _logger.debug("Signal handler for %s not supported on this platform", signame)


async def _serve(agent_dir: Path, inbound: str) -> int:
    config, _config_path = _load_config(agent_dir)
    agent = ArcAgent(config=config, config_path=agent_dir / "arcagent.toml")

    shutdown_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown_event)

    try:
        await agent.startup()
    except Exception:
        _write_shutdown_marker(agent_dir, status="crashed", reason="startup_failed")
        raise

    reason = "eof"
    try:
        if inbound == "stdin":
            reason = await _run_stdin_loop(agent, shutdown_event)
        else:
            reason = await _run_none_loop(shutdown_event)
    except Exception:
        _write_shutdown_marker(agent_dir, status="crashed", reason="exception")
        raise
    finally:
        try:
            await agent.shutdown()
        except Exception:  # reason: best-effort shutdown
            _logger.exception("agent.shutdown() raised during serve teardown")
            _write_shutdown_marker(agent_dir, status="crashed", reason="shutdown_failed")
            return 1

    _write_shutdown_marker(agent_dir, status="clean", reason=reason)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcagent",
        description="Run a single arcagent as a long-lived daemon.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run an agent from a directory.")
    serve.add_argument(
        "agent_dir",
        type=Path,
        help="Path to the agent directory (must contain arcagent.toml).",
    )
    serve.add_argument(
        "--inbound",
        choices=_INBOUND_CHOICES,
        default="stdin",
        help=(
            "Inbound message source. 'stdin' reads line-delimited prompts; "
            "'none' idles until SIGINT/SIGTERM (use when the agent's "
            "modules drive their own inbound flow)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args(argv)
    if args.command != "serve":
        # argparse with required=True keeps this unreachable, but be loud.
        sys.stderr.write(f"Unknown command: {args.command}\n")
        return 2
    agent_dir: Path = args.agent_dir.expanduser().resolve()
    if not agent_dir.is_dir():
        sys.stderr.write(f"Agent directory not found: {agent_dir}\n")
        return 2
    try:
        return asyncio.run(_serve(agent_dir, args.inbound))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
