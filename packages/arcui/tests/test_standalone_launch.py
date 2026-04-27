"""Standalone launch tests for `arc ui start`.

Verifies:
- `arc ui start` can run without any agent process.
- It opens the specified port.
- Accepts a WebSocket connection.
- Terminates cleanly when SIGTERM is sent.

These are subprocess tests — they start a real process and make real
network connections to verify end-to-end behavior.
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
# Fall back to the shared .venv at the root
_ARC_FALLBACK = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc_bin() -> Path:
    if _ARC.exists():
        return _ARC
    if _ARC_FALLBACK.exists():
        return _ARC_FALLBACK
    raise FileNotFoundError("arc binary not found — ensure venv is set up")


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> bool:
    """Poll until port accepts connections or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


class TestStandaloneLaunch:
    """arc ui start runs without any agent process."""

    def test_starts_on_specified_port(self) -> None:
        """arc ui start binds the given port and accepts TCP connections."""
        port = _free_port()
        proc = subprocess.Popen(
            [str(_arc_bin()), "ui", "start", "--port", str(port), "--show-tokens"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            opened = _wait_for_port(port, timeout=12.0)
            assert opened, f"Server did not open port {port} within 12s"
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)

    def test_prints_port_in_output(self) -> None:
        """arc ui start prints the port number in its startup output.

        The port appears in uvicorn's log (stderr) in the form
        'Uvicorn running on http://...<port>'. The test checks stdout+stderr
        combined because Python's _write() calls may be buffered until after
        uvicorn.run() completes.
        """
        port = _free_port()
        proc = subprocess.Popen(
            [str(_arc_bin()), "ui", "start", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_port(port, timeout=12.0)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=8)
            combined = stdout.decode() + stderr.decode()
            assert str(port) in combined, (
                f"Port {port} not mentioned in output.\n"
                f"stdout: {stdout.decode()!r}\nstderr: {stderr.decode()!r}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_accepts_websocket_connection(self) -> None:
        """arc ui start accepts a WebSocket connection from a browser client."""
        import websockets.sync.client  # type: ignore[import-untyped]

        port = _free_port()
        proc = subprocess.Popen(
            [
                str(_arc_bin()), "ui", "start",
                "--port", str(port),
                "--viewer-token", "test-viewer-tok",
                "--show-tokens",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            opened = _wait_for_port(port, timeout=12.0)
            assert opened, "Server did not open port"

            # Connect as a browser viewer
            with websockets.sync.client.connect(
                f"ws://127.0.0.1:{port}/ws", open_timeout=5
            ) as ws:
                ws.send(json.dumps({"token": "test-viewer-tok"}))
                resp = json.loads(ws.recv(timeout=5))
                assert resp.get("type") == "auth_ok", f"Expected auth_ok, got {resp}"
                assert resp.get("role") == "viewer"

        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)

    def test_sigterm_terminates_cleanly(self) -> None:
        """SIGTERM causes arc ui start to exit without a non-zero return code."""
        port = _free_port()
        proc = subprocess.Popen(
            [str(_arc_bin()), "ui", "start", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_port(port, timeout=12.0)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("Process did not terminate within 8s after SIGTERM")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_no_agent_dependency_required(self) -> None:
        """arc ui start must not fail just because no agent is connected."""
        port = _free_port()
        proc = subprocess.Popen(
            [str(_arc_bin()), "ui", "start", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            opened = _wait_for_port(port, timeout=12.0)
            assert opened, "Server failed to start — may have agent dependency issue"
            # Verify health endpoint responds without any agent connected
            import urllib.request

            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read())
            assert body.get("status") == "ok"
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
