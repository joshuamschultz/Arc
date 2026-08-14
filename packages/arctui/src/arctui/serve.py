"""Attach-or-serve resolver — the abstraction in front of the gateway router.

``arc tui`` never serves an agent itself. It resolves a gateway endpoint:

  - **Attach** — a gateway is already answering on ``{host}:{port}`` (an
    ``arc ui start`` / ``arc team serve`` you or the box already brought up):
    reuse it. Needs a viewer token (``--token``, or the loopback token
    ``arc ui start`` persists locally).
  - **Serve, then attach** — nothing is listening: spawn a detached
    ``arc ui start --no-browser`` (minting the viewer token so we own it),
    wait for ``/api/health``, then attach.

Either way the caller gets one :class:`Endpoint` to open a
:class:`~arctui.gateway_client.GatewayChatClient` against. The gateway is the
single ``ArcAgent`` owner; the TUI is a viewpoint — same as the browser
dashboard and the platform adapters.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust.paths import ui_token_file

from arctui.gateway_client import GatewayError

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420


class GatewayNeedsTokenError(GatewayError):
    """A gateway is running but no viewer token is available to attach with."""

    def __init__(self, base_url: str) -> None:
        super().__init__(
            f"a gateway is running at {base_url} but no viewer token was found. "
            "Pass --token <viewer-token>, or start it with `arc ui start` "
            "(which persists a local token for the TUI to reuse)."
        )
        self.base_url = base_url


class GatewaySpawnError(GatewayError):
    """A spawned gateway never became healthy within the timeout."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A resolved gateway to attach a chat client to.

    Attributes:
        base_url: HTTP base of the dashboard/gateway (``http://host:port``).
        token: Viewer token to authenticate the chat WebSocket.
        agent_id: Roster id/name of the agent to open a chat with.
        spawned: True when this process spawned the gateway (owns teardown).
        process: The ``Popen`` handle when ``spawned`` — else None.
    """

    base_url: str
    token: str
    agent_id: str
    spawned: bool
    process: Any | None = None


def default_token_path() -> Path:
    """Where ``arc ui start`` persists its loopback viewer token for local reuse."""
    return ui_token_file()


def write_persisted_token(token: str, *, path: Path | None = None) -> None:
    """Persist a loopback viewer token 0600 so a same-user TUI can attach."""
    target = path or default_token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(token, encoding="utf-8")
    target.chmod(0o600)


def read_persisted_token(*, path: Path | None = None) -> str | None:
    """Read the persisted loopback viewer token, or None if absent."""
    target = path or default_token_path()
    try:
        token = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def probe_health(base_url: str, *, timeout: float = 1.0) -> bool:
    """Return True if ``{base_url}/api/health`` answers 200 (auth-exempt route)."""
    import httpx

    try:
        resp = httpx.get(f"{base_url}/api/health", timeout=timeout)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _spawn_gateway(team_root: Path, host: str, port: int, token: str) -> subprocess.Popen[bytes]:
    """Spawn a detached ``arc ui start --no-browser`` serving ``team_root``."""
    cmd = [
        sys.executable,
        "-m",
        "arccli",
        "ui",
        "start",
        "--team-root",
        str(team_root),
        "--host",
        host,
        "--port",
        str(port),
        "--viewer-token",
        token,
        "--no-browser",
    ]
    # Tell the served agent where to operate its file/exec tools: the project the user
    # launched `arc tui` in. Honored only by opt-in (coding) agents, and only if the dir
    # is already trusted (folder-trust added it to allowed_paths). Agent state stays home.
    env = {**os.environ, "ARC_WORKING_DIR": str(Path.cwd().resolve())}
    return subprocess.Popen(  # noqa: S603 — fixed argv, no shell, token not logged
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def mint_token() -> str:
    """Mint a fresh viewer token for a gateway this process owns."""
    return secrets.token_hex(32)


async def ensure_gateway(
    *,
    agent_id: str,
    team_root: Path,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    token: str | None = None,
    probe: Callable[[str], bool] = probe_health,
    spawn: Callable[[Path, str, int, str], Any] = _spawn_gateway,
    read_token: Callable[[], str | None] = read_persisted_token,
    mint: Callable[[], str] = mint_token,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    wait_timeout: float = 20.0,
    poll_interval: float = 0.3,
) -> Endpoint:
    """Resolve a gateway to attach to, spawning one only if none is reachable."""
    if sleep is None:
        import asyncio

        sleep = asyncio.sleep

    base_url = f"http://{host}:{port}"

    if probe(base_url):
        tok = token or read_token()
        if not tok:
            raise GatewayNeedsTokenError(base_url)
        return Endpoint(base_url, tok, agent_id, spawned=False)

    tok = token or mint()
    process = spawn(team_root, host, port, tok)

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if probe(base_url):
            return Endpoint(base_url, tok, agent_id, spawned=True, process=process)
        await sleep(poll_interval)

    raise GatewaySpawnError(
        f"spawned gateway at {base_url} did not become healthy within {wait_timeout:.0f}s"
    )
