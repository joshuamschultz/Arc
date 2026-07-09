"""Tests for arcteam.nats_server — managed JetStream broker bootstrap."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from arcteam import nats_server
from arcteam.nats_server import (
    ManagedNatsServer,
    NatsServerUnavailableError,
    broker_listening,
    ensure_nats_server,
    parse_host_port,
)


class _StubProc:
    """Minimal subprocess.Popen stand-in for terminate_sync tests."""

    def __init__(self, poll_result: int | None, pid: int = 999999) -> None:
        self._poll = poll_result
        self.pid = pid
        self.terminated = False
        self.waited = False

    def poll(self) -> int | None:
        return self._poll

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


class TestParseHostPort:
    def test_full_url(self) -> None:
        assert parse_host_port("nats://10.0.0.5:4300") == ("10.0.0.5", 4300)

    def test_defaults(self) -> None:
        assert parse_host_port("nats://") == ("127.0.0.1", 4222)


class TestBrokerListening:
    async def test_true_when_socket_open(self) -> None:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            assert await broker_listening("127.0.0.1", port) is True
        finally:
            server.close()
            await server.wait_closed()

    async def test_false_when_nothing_listening(self) -> None:
        # Bind then release a port so it is almost certainly free.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        assert await broker_listening("127.0.0.1", free_port, timeout=0.2) is False


class TestEnsureNatsServer:
    async def test_reuses_existing_broker(self, tmp_path: Path) -> None:
        # An already-listening broker is reused: ensure returns None and never
        # spawns anything.
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            managed = await ensure_nats_server(
                url=f"nats://127.0.0.1:{port}", store_dir=tmp_path / "js"
            )
            assert managed is None
        finally:
            server.close()
            await server.wait_closed()

    async def test_raises_when_binary_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Free port + no nats-server on PATH → actionable error, not a traceback.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        monkeypatch.setattr(nats_server.shutil, "which", lambda _name: None)
        with pytest.raises(NatsServerUnavailableError) as exc:
            await ensure_nats_server(
                url=f"nats://127.0.0.1:{free_port}", store_dir=tmp_path / "js"
            )
        assert "nats-server" in str(exc.value)
        assert "PATH" in str(exc.value)


class TestTerminateSync:
    def test_noop_when_process_exited(self) -> None:
        # A finished broker (poll() returns a code) must not be terminated again.
        proc = _StubProc(poll_result=0)
        server = ManagedNatsServer(
            process=proc,  # type: ignore[arg-type]
            url="nats://127.0.0.1:4222",
            store_dir=Path("/tmp/x"),
        )
        server.terminate_sync()
        assert proc.terminated is False

    def test_terminates_live_process(self) -> None:
        proc = _StubProc(poll_result=None, pid=4242)
        server = ManagedNatsServer(
            process=proc,  # type: ignore[arg-type]
            url="nats://127.0.0.1:4222",
            store_dir=Path("/tmp/x"),
        )
        server.terminate_sync()
        assert proc.terminated is True
        assert proc.waited is True
