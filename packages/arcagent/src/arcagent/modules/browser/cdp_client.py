"""CDP connection and Chrome process management.

Manages headless Chrome lifecycle: launch subprocess, discover
WebSocket URL, send/receive CDP commands, graceful shutdown.
Abstracts the CDP transport so tool code never touches raw WebSocket.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import platform
import shutil
import signal
from pathlib import Path
from typing import Any

from arcagent.modules.browser.config import BrowserConnectionConfig
from arcagent.modules.browser.errors import CDPConnectionError

_logger = logging.getLogger(__name__)

# 50 MB — large AX trees and screenshot payloads
_WS_MAX_SIZE_BYTES = 50 * 1024 * 1024

# Per-command timeout for CDP responses (seconds)
_CDP_RECV_TIMEOUT_SECONDS = 30.0

_DEFAULT_FLAGS: list[str] = [
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-sync",
    "--disable-component-update",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--metrics-recording-only",
    "--mute-audio",
]

# Chrome flags that must never appear in user config — security risk
_BLOCKED_FLAGS: frozenset[str] = frozenset({
    "--disable-web-security",
    "--allow-file-access-from-files",
    "--disable-site-isolation-trials",
    "--allow-running-insecure-content",
})

# Typical Chrome binary locations by platform
_CHROME_PATHS: dict[str, list[str]] = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "Linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ],
}

# Track launched processes for atexit cleanup
_launched_pids: list[int] = []


def _cleanup_chrome_processes() -> None:
    """Best-effort SIGKILL for any Chrome processes still running at exit."""
    for pid in _launched_pids:
        try:
            import os

            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass  # Already exited
    _launched_pids.clear()


atexit.register(_cleanup_chrome_processes)


def _find_chrome() -> str:
    """Auto-detect Chrome binary path."""
    system = platform.system()
    candidates = _CHROME_PATHS.get(system, [])
    for path in candidates:
        if Path(path).exists():
            return path
    # Fallback: check PATH
    which = shutil.which("google-chrome") or shutil.which("chromium")
    if which:
        return which
    raise CDPConnectionError(
        message="Chrome binary not found. Set connection.chrome_path in config.",
        details={"system": system, "searched": candidates},
    )


def _validate_chrome_flags(flags: list[str]) -> None:
    """Reject blocked Chrome flags from user config."""
    for flag in flags:
        # Normalize: --flag=value → --flag
        base = flag.split("=", 1)[0]
        if base in _BLOCKED_FLAGS:
            raise CDPConnectionError(
                message=f"Chrome flag '{base}' is blocked by security policy",
                details={"flag": flag, "blocked": list(_BLOCKED_FLAGS)},
            )


class CDPClientManager:
    """Manages Chrome process and CDP WebSocket connection.

    Responsibilities:
    - Launch headless Chrome if no external cdp_url is configured
    - Discover WebSocket URL from Chrome's /json/version endpoint
    - Send CDP commands and receive results over WebSocket
    - Graceful shutdown: SIGTERM + wait, SIGKILL fallback, no zombies
    """

    def __init__(self, config: BrowserConnectionConfig) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._connected = False
        self._cmd_id = 0
        self._ws_url = ""
        self._debug_port = 0
        self._ws_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Whether the CDP WebSocket connection is active."""
        return self._connected

    @property
    def url(self) -> str:
        """The CDP WebSocket URL currently connected to."""
        return self._ws_url

    async def connect(self) -> None:
        """Launch Chrome (if needed) and establish CDP WebSocket connection."""
        if self._config.cdp_url:
            # External CDP endpoint — skip Chrome launch
            self._ws_url = self._config.cdp_url
            # Warn if external endpoint is not using wss://
            if self._ws_url.startswith("ws://") and not self._ws_url.startswith(
                "ws://127.0.0.1"
            ) and not self._ws_url.startswith("ws://localhost"):
                _logger.warning(
                    "External CDP URL uses unencrypted ws://. "
                    "Use wss:// for non-local connections."
                )
            _logger.info("Connecting to external CDP endpoint: %s", self._ws_url)
        else:
            # Launch Chrome and discover WS URL
            await self._launch_chrome()
            self._ws_url = await self._discover_ws_url()
            _logger.info("Chrome launched, CDP WebSocket: %s", self._ws_url)

        await self._connect_ws()
        self._connected = True
        await self._enable_domains()

    async def disconnect(self) -> None:
        """Close WebSocket and terminate Chrome process."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                _logger.debug("WebSocket close error (ignored)", exc_info=True)
            self._ws = None

        if self._process and self._process.returncode is None:
            pid = self._process.pid
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                _logger.warning("Chrome did not exit after SIGTERM, sending SIGKILL")
                self._process.kill()
                await self._process.wait()
            except Exception:
                _logger.debug("Chrome cleanup error (ignored)", exc_info=True)
            self._process = None
            # Remove from atexit tracking
            if pid in _launched_pids:
                _launched_pids.remove(pid)

        self._connected = False

    async def send(
        self, domain: str, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a CDP command and return the result.

        Uses an asyncio.Lock to prevent concurrent WebSocket access
        and a per-command timeout to avoid indefinite hangs.

        Args:
            domain: CDP domain (e.g. "Page", "DOM", "Input").
            method: Method name (e.g. "navigate", "getDocument").
            params: Command parameters.

        Returns:
            The ``result`` dict from the CDP response.

        Raises:
            CDPConnectionError: If not connected or command fails.
        """
        if not self._connected or not self._ws:
            raise CDPConnectionError(message="Not connected to CDP")

        async with self._ws_lock:
            self._cmd_id += 1
            cmd_id = self._cmd_id
            message = {
                "id": cmd_id,
                "method": f"{domain}.{method}",
                "params": params or {},
            }

            await self._ws.send(json.dumps(message))

            # Read responses until we get our command's result
            try:
                while True:
                    raw = await asyncio.wait_for(
                        self._ws.recv(), timeout=_CDP_RECV_TIMEOUT_SECONDS
                    )
                    response = json.loads(raw)

                    if response.get("id") == cmd_id:
                        if "error" in response:
                            error = response["error"]
                            raise CDPConnectionError(
                                message=f"CDP error: {error.get('message', 'unknown')}",
                                details={
                                    "code": error.get("code"),
                                    "domain": domain,
                                    "method": method,
                                },
                            )
                        result: dict[str, Any] = response.get("result", {})
                        return result

                    # Event or response for another command — skip
            except TimeoutError as exc:
                raise CDPConnectionError(
                    message=f"CDP command timed out: {domain}.{method}",
                    details={"timeout": _CDP_RECV_TIMEOUT_SECONDS},
                ) from exc

    async def _enable_domains(self) -> None:
        """Enable required CDP domains after connection."""
        for domain in ("Page", "DOM", "Runtime", "Accessibility"):
            await self.send(domain, "enable")
        _logger.debug("CDP domains enabled: Page, DOM, Runtime, Accessibility")

    async def _launch_chrome(self) -> None:
        """Launch headless Chrome subprocess."""
        chrome_path = self._config.chrome_path or _find_chrome()

        # Validate user-provided flags against blocklist
        _validate_chrome_flags(self._config.chrome_flags)

        flags = list(_DEFAULT_FLAGS)
        if not self._config.headless:
            flags = [f for f in flags if not f.startswith("--headless")]

        port = self._config.remote_debugging_port
        if port == 0:
            # Let the OS assign a port
            import socket

            with socket.socket() as s:
                s.bind(("", 0))
                port = s.getsockname()[1]

        flags.append(f"--remote-debugging-port={port}")
        flags.extend(self._config.chrome_flags)

        cmd = [chrome_path, *flags, "about:blank"]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._debug_port = port
        # Track for atexit cleanup
        if self._process.pid:
            _launched_pids.append(self._process.pid)
        _logger.info("Chrome launched (pid=%d, port=%d)", self._process.pid, port)

    async def _discover_ws_url(self) -> str:
        """Discover CDP WebSocket URL for a page target.

        Uses /json (page targets) instead of /json/version (browser target)
        because Page, DOM, and Accessibility domains are only available on
        page-level targets.
        """
        import httpx

        port = self._debug_port
        url = f"http://127.0.0.1:{port}/json"
        timeout = self._config.startup_timeout_seconds

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_error: Exception | None = None

        async with httpx.AsyncClient() as client:
            while loop.time() < deadline:
                try:
                    resp = await client.get(url, timeout=2.0)
                    targets = resp.json()
                    # Find the first "page" target (the about:blank tab)
                    for target in targets:
                        if target.get("type") == "page":
                            ws_url: str = target.get("webSocketDebuggerUrl", "")
                            if ws_url:
                                return ws_url
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(0.2)

        raise CDPConnectionError(
            message=f"Failed to discover CDP page target within {timeout}s",
            details={"port": port, "last_error": str(last_error)},
        )

    async def _connect_ws(self) -> None:
        """Establish WebSocket connection to CDP endpoint."""
        try:
            import websockets

            self._ws = await websockets.connect(
                self._ws_url,
                max_size=_WS_MAX_SIZE_BYTES,
            )
        except ImportError as err:
            raise CDPConnectionError(
                message="websockets package not installed. Add 'websockets' to dependencies."
            ) from err
        except Exception as exc:
            raise CDPConnectionError(
                message=f"WebSocket connection failed: {exc}",
                details={"url": self._ws_url},
            ) from exc
