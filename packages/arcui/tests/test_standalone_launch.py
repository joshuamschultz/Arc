"""End-to-end smoke for ``arc ui start``.

One subprocess, multiple assertions. Replaces an earlier suite that booted
a separate subprocess per assertion — each of those was opening a real
browser tab on a random ephemeral port because the parent's pytest
``webbrowser.open`` mock does not propagate to subprocesses.

Strategy
--------
* Use the canonical port (``8420``) so the test exercises the same address
  developers see in production. If 8420 is already bound (e.g. the
  developer is running ``arc ui start`` themselves), skip rather than fail.
* Pass ``--no-browser`` so even on loopback no browser tab is launched.
* Spin up the server **once** and run every assertion (port open, health,
  WS auth_ok, clean SIGTERM) against the same process.

What this test is NOT
---------------------
* Frontend rendering / CSS / DOM — a Playwright suite (Phase 8.4) is the
  right tool for that. This is a process-level smoke for the launcher.
* Subprocess port-allocator / signal-handler unit coverage — those live
  in :mod:`arccli.tests.test_ui_start_launcher` and exercise the helpers
  directly without spawning anything.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent.parent / "arccli" / ".venv" / "bin" / "arc"
_ARC_FALLBACK = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"

_PORT = 8420
_HOST = "127.0.0.1"


def _arc_bin() -> Path:
    if _ARC.exists():
        return _ARC
    if _ARC_FALLBACK.exists():
        return _ARC_FALLBACK
    raise FileNotFoundError("arc binary not found — ensure venv is set up")


def _port_busy(port: int, host: str = _HOST) -> bool:
    """True if something is actively listening on (host, port).

    A simple ``bind`` would also flag TIME_WAIT sockets left behind by a
    previous test run as busy. uvicorn itself binds with SO_REUSEADDR, so
    we match its semantics: only refuse if the port is in active LISTEN.
    """
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _wait_for_port(port: int, host: str = _HOST, timeout: float = 12.0) -> bool:
    """Poll until the port accepts connections or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def test_arc_ui_start_smoke() -> None:
    """End-to-end: ``arc ui start`` boots, accepts HTTP + WS, exits clean.

    Single subprocess. Single test. Multiple assertions in lock-step so
    the boot cost is paid once.
    """
    if _port_busy(_PORT):
        pytest.skip(
            f"port {_PORT} already in use — kill the running `arc ui start` "
            f"before re-running this test"
        )

    import websockets.sync.client  # type: ignore[import-untyped]

    proc = subprocess.Popen(
        [
            str(_arc_bin()),
            "ui",
            "start",
            "--port",
            str(_PORT),
            "--viewer-token",
            "test-viewer-tok",
            "--no-browser",
            "--show-tokens",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # 1. Port opens within the boot budget.
        assert _wait_for_port(_PORT), f"server did not bind {_PORT} within 12s"

        # 2. /api/health responds 200 with status ok (no agent attached).
        import urllib.request

        with urllib.request.urlopen(
            f"http://{_HOST}:{_PORT}/api/health", timeout=3
        ) as resp:
            body = json.loads(resp.read())
        assert body == {"status": "ok"}

        # 3. /ws first-message auth round-trips with the supplied token.
        with websockets.sync.client.connect(
            f"ws://{_HOST}:{_PORT}/ws", open_timeout=5
        ) as ws:
            ws.send(json.dumps({"token": "test-viewer-tok"}))
            resp = json.loads(ws.recv(timeout=5))
            assert resp.get("type") == "auth_ok", f"expected auth_ok, got {resp}"
            assert resp.get("role") == "viewer"

        # 4. SIGTERM exits cleanly within the shutdown budget.
        proc.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("process did not terminate within 8s after SIGTERM")

        # 5. uvicorn logged the real bind address — guards against false
        # passes where the subprocess crashed and a stale TIME_WAIT entry
        # tricked the port poll. uvicorn writes startup banners to stderr.
        combined = stdout.decode() + stderr.decode()
        assert f"{_HOST}:{_PORT}" in combined, (
            f"server banner missing port {_PORT} — process likely crashed.\n"
            f"stdout: {stdout!r}\nstderr: {stderr!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
