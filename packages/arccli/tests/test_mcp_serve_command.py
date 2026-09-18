"""SPEC-082 T-1113 (RED) — the ``arc mcp serve`` command that binds the socket.

arcagent is headless; the CLI is the surface that actually listens. T-1113 adds
``arc mcp serve [--stdio | --http --host H --port P] [--agent NAME]`` in
``arccli/commands/mcp.py``, registered in ``COMMAND_REGISTRY``. It loads/starts the
agent, calls ``build_door_from_agent``, and serves the built door — stdio over the
process streams (default) or the ``HttpDoor`` ASGI app via uvicorn.

These tests assert the WIRING (which builder/serve got called, with what), never
real I/O: no real agent is started and no port is bound. The command's collaborators
are patched at the ``arccli.commands.mcp`` namespace, so the handler must reference
them as module-level names (a testability requirement this contract fixes):
``_load_arcagent``, ``build_door_from_agent``, ``serve_stdio``, ``_process_streams``,
and third-party ``uvicorn``.

RED: the command is not registered and ``arccli.commands.mcp`` does not exist. Each
test asserts the command resolves in the registry FIRST (``cmd is not None``), which
fails today — so the RED reason is "command absent", not a patch/import error. It
goes GREEN when T-1113 registers the command and adds the module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arccli.commands.registry import resolve_command_and_args


def _fake_load_arcagent() -> Any:
    """A patched ``_load_arcagent`` returning a fake started-able agent triple."""
    agent = MagicMock(name="ArcAgent")
    agent.startup = AsyncMock()
    agent.shutdown = AsyncMock()
    return MagicMock(return_value=(agent, MagicMock(), MagicMock()))


def _fake_built_door() -> Any:
    """A patched ``build_door_from_agent`` returning a door with .router and .http_app."""
    built = MagicMock(name="BuiltDoor")
    built.router = MagicMock(name="DoorRouter")
    built.http_app = MagicMock(name="HttpDoorApp")
    return MagicMock(return_value=built)


class _SpyUvicornServer:
    """Captures the uvicorn Config it is handed; never binds a socket."""

    last_config: Any = None

    def __init__(self, config: Any) -> None:
        _SpyUvicornServer.last_config = config

    def run(self) -> None:  # sync run path (matches arc ui start)
        return None

    async def serve(self) -> None:  # async serve path
        return None


def test_mcp_serve_is_registered_in_the_cli() -> None:
    """``arc mcp serve`` resolves through the shared command registry (discoverable)."""
    cmd, _args = resolve_command_and_args(["mcp", "serve"])
    assert cmd is not None, "arc mcp serve is not registered in COMMAND_REGISTRY"


def test_stdio_mode_builds_the_door_and_serves_it_over_process_streams(tmp_path: Any) -> None:
    """``arc mcp serve --stdio`` loads the agent, builds the door, and drives serve_stdio."""
    cmd, args = resolve_command_and_args(
        ["mcp", "serve", "--agent", str(tmp_path), "--stdio"]
    )
    assert cmd is not None, "arc mcp serve is not registered in COMMAND_REGISTRY"

    load = _fake_load_arcagent()
    build = _fake_built_door()
    serve_stdio = AsyncMock(name="serve_stdio")
    process_streams = MagicMock(return_value=(MagicMock(name="reader"), MagicMock(name="writer")))

    with (
        patch("arccli.commands.mcp._load_arcagent", load),
        patch("arccli.commands.mcp.build_door_from_agent", build),
        patch("arccli.commands.mcp.serve_stdio", serve_stdio),
        patch("arccli.commands.mcp._process_streams", process_streams),
    ):
        cmd.handler(args)

    # The agent was loaded and handed to the builder.
    load.assert_called_once()
    build.assert_called_once()
    served_agent = build.call_args.args[0]
    assert served_agent is load.return_value[0]

    # serve_stdio was driven with the built door's router.
    serve_stdio.assert_called_once()
    assert serve_stdio.call_args.args[0] is build.return_value.router


def test_http_mode_serves_the_asgi_app_on_the_requested_port(tmp_path: Any) -> None:
    """``arc mcp serve --http --port N`` runs uvicorn with the door's ASGI app on port N."""
    cmd, args = resolve_command_and_args(
        ["mcp", "serve", "--agent", str(tmp_path), "--http", "--host", "127.0.0.1", "--port", "9911"]
    )
    assert cmd is not None, "arc mcp serve is not registered in COMMAND_REGISTRY"

    load = _fake_load_arcagent()
    build = _fake_built_door()
    _SpyUvicornServer.last_config = None

    with (
        patch("arccli.commands.mcp._load_arcagent", load),
        patch("arccli.commands.mcp.build_door_from_agent", build),
        patch("uvicorn.Server", _SpyUvicornServer),
    ):
        cmd.handler(args)

    build.assert_called_once()
    config = _SpyUvicornServer.last_config
    assert config is not None, "uvicorn.Server was never constructed for the --http path"
    assert config.app is build.return_value.http_app
    assert config.port == 9911
