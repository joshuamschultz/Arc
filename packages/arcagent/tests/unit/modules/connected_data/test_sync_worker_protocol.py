"""The sync worker's wire refuses anything it cannot authenticate (alpha-2 sync worker).

Every request carries a MAC keyed by a per-spawn secret, a direction, a timestamp
and a nonce; the peer is checked by its kernel-reported uid and pid before a byte
is read. A forged, replayed, reflected or foreign request is refused and never
reaches the handler.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.connected_data.sync_worker.protocol import (
    WORKER_REQUEST,
    WORKER_RESPONSE,
    AuthenticationError,
    Frame,
    PeerCredentials,
    ReplayGuard,
    encode,
    peer_credentials,
    request_header,
    seal,
    verify,
)
from arcagent.modules.connected_data.sync_worker.rpc import (
    RpcClient,
    RpcServer,
    RpcUnavailableError,
)

_SECRET = b"s" * 32


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    # A Unix socket path must stay under ~104 bytes; pytest's tmp_path is longer.
    path = Path(tempfile.mkdtemp(prefix="arc-sw-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _request(body: bytes = b"") -> Frame:
    return seal(Frame(request_header("ping", {}), body), _SECRET, direction=WORKER_REQUEST)


def test_a_sealed_frame_verifies() -> None:
    header = verify(_request(b"doc"), _SECRET, direction=WORKER_REQUEST)
    assert header["op"] == "ping"


@pytest.mark.parametrize(
    "forge",
    [
        lambda f: Frame(f.header, b"other body"),
        lambda f: Frame({**f.header, "op": "write"}, f.body),
        lambda f: Frame({k: v for k, v in f.header.items() if k != "mac"}, f.body),
        lambda f: seal(Frame(f.header, f.body), b"x" * 32, direction=WORKER_REQUEST),
    ],
    ids=["body-swapped", "header-edited", "no-mac", "wrong-secret"],
)
def test_a_forged_frame_is_refused(forge: Any) -> None:
    with pytest.raises(AuthenticationError):
        verify(forge(_request(b"doc")), _SECRET, direction=WORKER_REQUEST)


def test_a_request_cannot_be_reflected_as_a_reply() -> None:
    with pytest.raises(AuthenticationError):
        verify(_request(), _SECRET, direction=WORKER_RESPONSE)


def test_a_replayed_or_stale_request_is_refused() -> None:
    guard = ReplayGuard()
    header = verify(_request(), _SECRET, direction=WORKER_REQUEST)
    guard.check(header)
    with pytest.raises(AuthenticationError, match="replay"):
        guard.check(header)
    stale = {**header, "nonce": "n" * 32, "ts": header["ts"] - 3600}
    with pytest.raises(AuthenticationError, match="window"):
        guard.check(stale)


def test_peer_credentials_name_this_process() -> None:
    left, right = socket.socketpair(socket.AF_UNIX)
    try:
        creds = peer_credentials(left)
    finally:
        left.close()
        right.close()
    assert creds.uid == os.getuid()
    assert creds.pid in (os.getpid(), None)


class _Handler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, op: str, args: dict[str, Any], body: bytes) -> tuple[Any, bytes]:
        self.calls.append(op)
        return {"echo": args}, body[::-1]


async def _server(
    path: Path,
    handler: _Handler,
    refused: list[str],
    *,
    peer: Any = peer_credentials,
    pid: int | None = None,
) -> RpcServer:
    server = RpcServer(
        path=path,
        secret=lambda: _SECRET,
        request_direction=WORKER_REQUEST,
        response_direction=WORKER_RESPONSE,
        handler=handler,
        expected_pid=lambda: pid,
        on_refused=refused.append,
        peer=peer,
    )
    await server.start()
    return server


def _client(path: Path, secret: bytes = _SECRET) -> RpcClient:
    return RpcClient(
        path=path,
        secret=secret,
        request_direction=WORKER_REQUEST,
        response_direction=WORKER_RESPONSE,
        expected_pid=os.getpid(),
    )


async def test_an_authenticated_call_is_answered(sock_dir: Path) -> None:
    handler, refused = _Handler(), []
    server = await _server(sock_dir / "w.sock", handler, refused)
    try:
        mode = (sock_dir / "w.sock").stat().st_mode & 0o777
        result, body = await _client(sock_dir / "w.sock").call("ping", {"a": 1}, b"abc", timeout=5)
    finally:
        await server.close()
    assert mode == 0o600, "only this user may open the socket"
    assert result == {"echo": {"a": 1}} and body == b"cba"
    assert handler.calls == ["ping"] and refused == []


async def test_a_request_under_the_wrong_secret_never_reaches_the_handler(
    sock_dir: Path,
) -> None:
    handler, refused = _Handler(), []
    server = await _server(sock_dir / "w.sock", handler, refused)
    try:
        with pytest.raises(RpcUnavailableError):
            await _client(sock_dir / "w.sock", b"f" * 32).call("write", {}, timeout=5)
    finally:
        await server.close()
    assert handler.calls == []
    assert refused and "MAC" in refused[0]


async def test_a_replayed_request_is_refused(sock_dir: Path) -> None:
    handler, refused = _Handler(), []
    server = await _server(sock_dir / "w.sock", handler, refused)
    wire = encode(_request(b"payload"))
    try:
        for _ in range(2):
            reader, writer = await asyncio.open_unix_connection(str(sock_dir / "w.sock"))
            writer.write(wire)
            await writer.drain()
            await reader.read(1 << 16)
            writer.close()
            await writer.wait_closed()
    finally:
        await server.close()
    assert handler.calls == ["ping"], "the second, identical request was acted on"
    assert any("replay" in reason for reason in refused)


async def test_a_peer_running_as_another_user_is_refused(sock_dir: Path) -> None:
    handler, refused = _Handler(), []

    def other_user(sock: socket.socket) -> PeerCredentials:
        return PeerCredentials(uid=os.getuid() + 1, pid=os.getpid())

    server = await _server(sock_dir / "w.sock", handler, refused, peer=other_user)
    try:
        with pytest.raises(RpcUnavailableError):
            await _client(sock_dir / "w.sock").call("ping", {}, timeout=5)
    finally:
        await server.close()
    assert handler.calls == []
    assert refused == ["peer refused: peer runs as a different user"]


async def test_a_peer_that_is_not_the_expected_process_is_refused(sock_dir: Path) -> None:
    handler, refused = _Handler(), []
    server = await _server(sock_dir / "w.sock", handler, refused, pid=os.getpid() + 100_000)
    try:
        with pytest.raises(RpcUnavailableError):
            await _client(sock_dir / "w.sock").call("ping", {}, timeout=5)
    finally:
        await server.close()
    assert handler.calls == []
    assert refused == ["peer refused: peer is not the expected process"]


async def test_a_client_refuses_a_server_that_is_not_the_expected_process(
    sock_dir: Path,
) -> None:
    handler, refused = _Handler(), []
    server = await _server(sock_dir / "w.sock", handler, refused)
    client = RpcClient(
        path=sock_dir / "w.sock",
        secret=_SECRET,
        request_direction=WORKER_REQUEST,
        response_direction=WORKER_RESPONSE,
        expected_pid=os.getpid() + 100_000,
    )
    try:
        with pytest.raises(RpcUnavailableError, match="not the expected process"):
            await client.call("ping", {}, timeout=5)
    finally:
        await server.close()
    assert handler.calls == []
