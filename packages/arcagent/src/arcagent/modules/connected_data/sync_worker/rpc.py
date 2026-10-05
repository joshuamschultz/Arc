"""One authenticated request/response server and client over the sync-worker wire.

Both channels use it: the main process calls the worker, and the worker calls
back into the main process. A server checks the peer's kernel credentials before
it reads a byte, then each frame's MAC, direction, timestamp and nonce. Anything
that fails is refused and reported through ``on_refused``; the connection is
closed without a reply, so a forger learns nothing. A client checks the server's
credentials and the reply's MAC, direction and request id.

A client opens one connection per call. The socket is local, so a connect costs
microseconds, and no connection outlives a restart of the process behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from arcagent.modules.connected_data.sync_worker.protocol import (
    AuthenticationError,
    Frame,
    PeerReader,
    ProtocolError,
    ReplayGuard,
    peer_credentials,
    read_frame,
    read_frame_blocking,
    request_header,
    require_peer,
    seal,
    verify,
    write_frame,
    write_frame_blocking,
)

_logger = logging.getLogger("arcagent.modules.connected_data.sync_worker.rpc")

#: A handler answers ``(op, args, body)`` with ``(result, body)``.
Handler = Callable[[str, dict[str, Any], bytes], Awaitable[tuple[Any, bytes]]]


class RpcUnavailableError(Exception):
    """The other process could not be reached, timed out or hung up."""


class RpcConnectError(RpcUnavailableError):
    """The connection could not be made, so nothing was sent and a retry is safe."""


class RemoteError(Exception):
    """The other process answered with a typed error."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message") or error.get("type") or "remote error"))
        self.error = error

    @property
    def kind(self) -> str:
        return str(self.error.get("type", "internal"))


class RpcError(Exception):
    """Raised by a handler to answer with a typed error rather than ``internal``."""

    def __init__(self, kind: str, message: str = "", **fields: Any) -> None:
        super().__init__(message or kind)
        self.wire = {"type": kind, "message": message, **fields}


class RpcServer:
    """Serve authenticated requests on a Unix socket only this user can open."""

    def __init__(
        self,
        *,
        path: Path,
        secret: Callable[[], bytes | None],
        request_direction: str,
        response_direction: str,
        handler: Handler,
        expected_pid: Callable[[], int | None] = lambda: None,
        on_refused: Callable[[str], None] = lambda reason: None,
        peer: PeerReader = peer_credentials,
        guard: ReplayGuard | None = None,
    ) -> None:
        self._path = path
        self._secret = secret
        self._request = request_direction
        self._response = response_direction
        self._handler = handler
        self._expected_pid = expected_pid
        self._on_refused = on_refused
        self._peer = peer
        self._guard = guard or ReplayGuard()
        self._server: asyncio.AbstractServer | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(self._path))
        self._path.chmod(0o600)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            try:
                require_peer(self._peer(sock), pid=self._expected_pid())
            except (AuthenticationError, OSError) as exc:
                self._on_refused(f"peer refused: {exc}")
                return
            while True:
                try:
                    frame = await read_frame(reader)
                except asyncio.IncompleteReadError:
                    return
                header = self._authenticate(frame)
                if header is None:
                    return
                await write_frame(writer, await self._answer(header, frame.body))
        except ProtocolError as exc:
            self._on_refused(f"malformed frame: {exc}")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _authenticate(self, frame: Frame) -> dict[str, Any] | None:
        secret = self._secret()
        if secret is None:
            self._on_refused("no secret is bound")
            return None
        try:
            header = verify(frame, secret, direction=self._request)
            self._guard.check(header)
        except AuthenticationError as exc:
            self._on_refused(str(exc))
            return None
        return header

    async def _answer(self, header: dict[str, Any], body: bytes) -> Frame:
        reply: dict[str, Any] = {"v": header["v"], "id": header.get("id", "")}
        out = b""
        args = header.get("args")
        try:
            result, out = await self._handler(
                str(header.get("op", "")), args if isinstance(args, dict) else {}, body
            )
            reply.update(ok=True, result=result)
        except RpcError as exc:
            reply.update(ok=False, error=exc.wire)
        except Exception as exc:  # reason: one failed call is answered, never fatal to the server
            _logger.warning("sync-worker call %s failed", header.get("op"), exc_info=True)
            reply.update(ok=False, error={"type": "internal", "name": type(exc).__name__})
        secret = self._secret() or b""
        return seal(Frame(reply, out), secret, direction=self._response)


class RpcClient:
    """Call one authenticated server; every failure to get an answer is ``RpcUnavailableError``."""

    def __init__(
        self,
        *,
        path: Path,
        secret: bytes,
        request_direction: str,
        response_direction: str,
        expected_pid: int | None = None,
        peer: PeerReader = peer_credentials,
        connect_timeout: float = 5.0,
    ) -> None:
        self._path = path
        self._secret = secret
        self._request = request_direction
        self._response = response_direction
        self._expected_pid = expected_pid
        self._peer = peer
        self._connect_timeout = connect_timeout

    async def call(
        self, op: str, args: dict[str, Any], body: bytes = b"", *, timeout: float
    ) -> tuple[Any, bytes]:
        header = request_header(op, args)
        request = seal(Frame(header, body), self._secret, direction=self._request)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._path)), self._connect_timeout
            )
        except (OSError, TimeoutError) as exc:
            raise RpcConnectError(f"cannot connect: {type(exc).__name__}") from exc
        try:
            self._check_server(writer.get_extra_info("socket"))
            await write_frame(writer, request)
            reply = await asyncio.wait_for(read_frame(reader), timeout)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ProtocolError) as exc:
            raise RpcUnavailableError(f"no answer: {type(exc).__name__}") from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return self._result(header, reply)

    def call_blocking(
        self, op: str, args: dict[str, Any], body: bytes = b"", *, timeout: float
    ) -> tuple[Any, bytes]:
        """The same call from a thread that is not running an event loop."""
        header = request_header(op, args)
        request = seal(Frame(header, body), self._secret, direction=self._request)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._connect_timeout)
                sock.connect(str(self._path))
                sock.settimeout(timeout)
                self._check_server(sock)
                write_frame_blocking(sock, request)
                reply = read_frame_blocking(sock)
        except (OSError, ProtocolError) as exc:
            raise RpcUnavailableError(f"no answer: {type(exc).__name__}") from exc
        return self._result(header, reply)

    def _check_server(self, sock: socket.socket) -> None:
        try:
            require_peer(self._peer(sock), pid=self._expected_pid)
        except (AuthenticationError, OSError) as exc:
            raise RpcUnavailableError(f"server refused: {exc}") from exc

    def _result(self, request: dict[str, Any], reply: Frame) -> tuple[Any, bytes]:
        try:
            header = verify(reply, self._secret, direction=self._response)
        except AuthenticationError as exc:
            raise RpcUnavailableError(f"reply refused: {exc}") from exc
        if header.get("id") != request["id"]:
            raise RpcUnavailableError("reply answers a different request")
        if header.get("ok") is True:
            return header.get("result"), reply.body
        error = header.get("error")
        raise RemoteError(error if isinstance(error, dict) else {"type": "internal"})


__all__ = [
    "Handler",
    "RemoteError",
    "RpcClient",
    "RpcError",
    "RpcServer",
    "RpcUnavailableError",
]
