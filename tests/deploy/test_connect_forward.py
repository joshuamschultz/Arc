"""The CONNECT bridge must never look healthy while forwarding nothing.

Its listener binds whether or not the target exists, so the naive version reports
``active`` to systemd and fails every request. That shipped: the target host left
the tailnet, the unit stayed green for weeks, and every turn on the agent behind
it died with a protocol error naming nothing. Health was measuring the socket,
not the path — the same mistake as a fleet whose ``/health`` returned 200 while
every message failed.

Driven against a real loopback CONNECT proxy, so the handshake, the refusal, and
the byte-splice are exercised rather than described.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "connect-forward.py"


def _load_module() -> Any:
    """Import the deploy script by path — it ships as a file, not a package."""
    spec = importlib.util.spec_from_file_location("arc_connect_forward", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


forward = _load_module()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _serve(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any],
) -> AsyncIterator[tuple[str, int]]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    async with server:
        yield host, int(port)


@pytest.fixture
async def accepting_proxy() -> AsyncIterator[tuple[str, int]]:
    """A CONNECT proxy that accepts and echoes whatever the client sends."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            while chunk := await reader.read(4096):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async for address in _serve(handle):
        yield address


@pytest.fixture
async def refusing_proxy() -> AsyncIterator[tuple[str, int]]:
    """A CONNECT proxy that is up but cannot reach the target — the real failure.

    This is the state the live bridge was in: the proxy answered, the host behind
    it was gone. Nothing about the local socket reveals that.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await writer.drain()
        writer.close()

    async for address in _serve(handle):
        yield address


async def test_it_refuses_to_start_when_the_target_is_unreachable(
    refusing_proxy: tuple[str, int],
) -> None:
    """No listener at all, rather than a listener that fails every request.

    Binding first is what let systemd call this active. Refusing before the bind
    means "the unit is up" and "the path works" stop being separable.
    """
    listen = ("127.0.0.1", _free_port())

    with pytest.raises(forward.TargetUnreachableError):
        await forward.serve(listen, refusing_proxy, "gone-host:4000")

    with socket.socket() as probe:
        assert probe.connect_ex(listen) != 0, "it bound a port it cannot serve"


async def test_it_refuses_to_start_when_the_proxy_itself_is_down() -> None:
    """A dead proxy is the same failure, and must not degrade to a bound port."""
    listen = ("127.0.0.1", _free_port())

    with pytest.raises(forward.TargetUnreachableError):
        await forward.serve(listen, ("127.0.0.1", _free_port()), "any-host:4000")

    with socket.socket() as probe:
        assert probe.connect_ex(listen) != 0


async def test_a_reachable_target_is_served_and_bytes_flow(
    accepting_proxy: tuple[str, int],
) -> None:
    """The happy path: real CONNECT handshake, real splice, bytes come back.

    Paired with the refusals above so "refuses to start" cannot be satisfied by a
    bridge that never works at all.
    """
    listen = ("127.0.0.1", _free_port())
    server_task = asyncio.create_task(forward.serve(listen, accepting_proxy, "live-host:4000"))
    try:
        for _ in range(100):  # let the probe and bind complete
            await asyncio.sleep(0.01)
            with socket.socket() as probe:
                if probe.connect_ex(listen) == 0:
                    break
        else:
            raise AssertionError("the bridge never started listening")

        reader, writer = await asyncio.open_connection(*listen)
        writer.write(b"ping through the tunnel")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(64), timeout=5) == b"ping through the tunnel"
        writer.close()
    finally:
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)


async def test_the_target_is_required_and_never_defaulted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An absent target must exit non-zero, not fall back to a built-in host.

    The original defect was a hard-coded hostname that outlived the machine it
    named. A default is what let that survive a redeploy unnoticed.
    """
    monkeypatch.delenv("ARC_FORWARD_TARGET", raising=False)

    assert forward.main() == 2
    assert "ARC_FORWARD_TARGET is required" in caplog.text


async def test_the_watcher_stops_the_server_when_the_target_disappears(
    refusing_proxy: tuple[str, int],
) -> None:
    """A target that dies later must take the bridge down with it.

    Startup probing alone leaves the weeks-long failure intact: the host left the
    tailnet long after the unit started. Exiting hands the verdict to systemd,
    whose restart loop then reflects reality.
    """
    monkey_interval = forward.HEALTH_INTERVAL
    forward.HEALTH_INTERVAL = 0.01
    try:
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        async with server:
            await asyncio.wait_for(
                forward.watch(refusing_proxy, "gone-host:4000", server), timeout=5
            )
            assert not server.is_serving(), "the watcher left a dead bridge serving"
    finally:
        forward.HEALTH_INTERVAL = monkey_interval
