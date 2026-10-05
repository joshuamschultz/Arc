"""The sync worker's wire: framed, authenticated messages over a local Unix socket.

Two channels use it. The main process sends store writes to the worker
(``worker.*``), and the worker asks the main process for what it must not hold
itself: a seal signature, an arcstore row, an audit write (``host.*``).

Every frame is ``>II`` (header length, body length), a JSON header and raw body
bytes; document bytes never pass through base64. Every header carries an
HMAC-SHA256 over the canonical header (its direction included) and the body's
SHA-256, keyed by a secret made fresh for each worker spawn and handed to the
child on its stdin, never in argv or the environment. A server refuses a frame
whose MAC is wrong, whose timestamp is outside the clock-skew window, or whose
nonce it has already seen. A peer is also checked by its kernel-reported
credentials: the same uid, and the expected pid where the platform reports one.

The direction is part of the MAC, so a request cannot be replayed as a reply or
carried from one channel to the other.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
#: A header is metadata: a request names a store and a method, never a document.
MAX_HEADER_BYTES = 1 << 20
#: One frame's body: a single object's raw content, bounded well above any sync limit.
MAX_BODY_BYTES = 512 << 20
#: How far a frame's timestamp may be from the receiver's clock.
CLOCK_SKEW_SECONDS = 30.0

WORKER_REQUEST = "worker.request"
WORKER_RESPONSE = "worker.response"
HOST_REQUEST = "host.request"
HOST_RESPONSE = "host.response"

_PREFIX = struct.Struct(">II")


class ProtocolError(Exception):
    """A frame that is malformed, oversized or cut short."""


class AuthenticationError(ProtocolError):
    """A frame or peer that failed authentication: refused, never acted on."""


@dataclass(frozen=True)
class Frame:
    """One message: a JSON header and raw body bytes."""

    header: dict[str, Any]
    body: bytes = b""


def _canonical(header: dict[str, Any]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _mac(secret: bytes, header: dict[str, Any], body: bytes) -> str:
    digest = hashlib.sha256(body).digest()
    return hmac.new(secret, _canonical(header) + b"\n" + digest, hashlib.sha256).hexdigest()


def request_header(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """A fresh request header: unique id, random nonce, the sender's clock."""
    return {
        "v": PROTOCOL_VERSION,
        "id": uuid.uuid4().hex,
        "op": op,
        "args": args,
        "nonce": secrets.token_hex(16),
        "ts": time.time(),
    }


def seal(frame: Frame, secret: bytes, *, direction: str) -> Frame:
    """Stamp ``frame`` with its direction and MAC."""
    header = {key: value for key, value in frame.header.items() if key != "mac"}
    header["dir"] = direction
    header["mac"] = _mac(secret, header, frame.body)
    return Frame(header, frame.body)


def verify(frame: Frame, secret: bytes, *, direction: str) -> dict[str, Any]:
    """The header without its MAC, once the MAC and direction check; else refused."""
    header = dict(frame.header)
    mac = header.pop("mac", None)
    if not isinstance(mac, str) or header.get("dir") != direction:
        raise AuthenticationError("frame is unauthenticated")
    if not hmac.compare_digest(mac, _mac(secret, header, frame.body)):
        raise AuthenticationError("frame MAC does not verify")
    if header.get("v") != PROTOCOL_VERSION:
        raise AuthenticationError("unsupported protocol version")
    return header


@dataclass
class ReplayGuard:
    """Refuse a request whose timestamp is stale or whose nonce was already seen."""

    skew_seconds: float = CLOCK_SKEW_SECONDS
    clock: Callable[[], float] = time.time
    _seen: dict[str, float] = field(default_factory=dict)

    def check(self, header: dict[str, Any]) -> None:
        now = self.clock()
        ts = header.get("ts")
        nonce = header.get("nonce")
        if not isinstance(ts, (int, float)) or abs(now - float(ts)) > self.skew_seconds:
            raise AuthenticationError("request timestamp is outside the allowed window")
        if not isinstance(nonce, str) or len(nonce) < 16:
            raise AuthenticationError("request has no nonce")
        self._prune(now)
        if nonce in self._seen:
            raise AuthenticationError("request nonce was already used (replay)")
        self._seen[nonce] = float(ts)

    def _prune(self, now: float) -> None:
        horizon = now - 2 * self.skew_seconds
        stale = [nonce for nonce, ts in self._seen.items() if ts < horizon]
        for nonce in stale:
            del self._seen[nonce]


def encode(frame: Frame) -> bytes:
    header = _canonical(frame.header)
    if len(header) > MAX_HEADER_BYTES or len(frame.body) > MAX_BODY_BYTES:
        raise ProtocolError("frame exceeds the protocol limits")
    return _PREFIX.pack(len(header), len(frame.body)) + header + frame.body


def _lengths(prefix: bytes) -> tuple[int, int]:
    header_len, body_len = _PREFIX.unpack(prefix)
    if header_len > MAX_HEADER_BYTES or body_len > MAX_BODY_BYTES:
        raise ProtocolError("frame exceeds the protocol limits")
    return header_len, body_len


def _decode_header(raw: bytes) -> dict[str, Any]:
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProtocolError("frame header is not JSON") from exc
    if not isinstance(header, dict):
        raise ProtocolError("frame header is not an object")
    return header


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """Read one frame; ``asyncio.IncompleteReadError`` when the peer hung up."""
    header_len, body_len = _lengths(await reader.readexactly(_PREFIX.size))
    header = _decode_header(await reader.readexactly(header_len))
    body = await reader.readexactly(body_len) if body_len else b""
    return Frame(header, body)


async def write_frame(writer: asyncio.StreamWriter, frame: Frame) -> None:
    writer.write(encode(frame))
    await writer.drain()


def _recv_exactly(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ProtocolError("peer closed the connection mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame_blocking(sock: socket.socket) -> Frame:
    header_len, body_len = _lengths(_recv_exactly(sock, _PREFIX.size))
    header = _decode_header(_recv_exactly(sock, header_len))
    return Frame(header, _recv_exactly(sock, body_len) if body_len else b"")


def write_frame_blocking(sock: socket.socket, frame: Frame) -> None:
    sock.sendall(encode(frame))


# -- peer credentials --------------------------------------------------------


@dataclass(frozen=True)
class PeerCredentials:
    """Who is on the other end of a Unix socket, as the kernel reports it."""

    uid: int
    #: ``None`` where the platform does not report the peer's pid.
    pid: int | None


#: macOS: ``SOL_LOCAL`` level, ``LOCAL_PEERCRED`` (struct xucred) and ``LOCAL_PEERPID``.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_LOCAL_PEERPID = 0x002
_XUCRED_SIZE = 76


def peer_credentials(sock: socket.socket) -> PeerCredentials:
    """The connected peer's uid (and pid where available) from the kernel."""
    if sys.platform.startswith("linux"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", raw)
        return PeerCredentials(uid=uid, pid=pid)
    if sys.platform == "darwin":
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
        uid = struct.unpack_from("=I", raw, 4)[0]
        pid_raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
        return PeerCredentials(uid=uid, pid=struct.unpack("i", pid_raw)[0])
    raise AuthenticationError(f"peer credentials are unavailable on {sys.platform}")


def require_peer(
    creds: PeerCredentials, *, uid: int | None = None, pid: int | None = None
) -> None:
    """Refuse a peer that is not this user, or not the expected process."""
    expected_uid = os.getuid() if uid is None else uid
    if creds.uid != expected_uid:
        raise AuthenticationError("peer runs as a different user")
    if pid is not None and creds.pid is not None and creds.pid != pid:
        raise AuthenticationError("peer is not the expected process")


PeerReader = Callable[[socket.socket], PeerCredentials]


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "HOST_REQUEST",
    "HOST_RESPONSE",
    "MAX_BODY_BYTES",
    "MAX_HEADER_BYTES",
    "PROTOCOL_VERSION",
    "WORKER_REQUEST",
    "WORKER_RESPONSE",
    "AuthenticationError",
    "Frame",
    "PeerCredentials",
    "PeerReader",
    "ProtocolError",
    "ReplayGuard",
    "encode",
    "peer_credentials",
    "read_frame",
    "read_frame_blocking",
    "request_header",
    "require_peer",
    "seal",
    "verify",
    "write_frame",
    "write_frame_blocking",
]
