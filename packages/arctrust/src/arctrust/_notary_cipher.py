"""Reference transit cipher child: AES-256-GCM with a key the caller never reads.

The child half of :func:`arctrust.transit_cipher.notary_encrypt` /
:func:`~arctrust.transit_cipher.notary_decrypt`. A separate OS process reads the
key file, encrypts or decrypts the payload on stdin, and writes the result to
stdout. The calling process handles only plaintext, associated data and
ciphertext, never the key.

Run as ``python -I _notary_cipher.py <key_path> encrypt|decrypt`` with
stdin = 4-byte big-endian AAD length || AAD || payload.

* ``encrypt``: payload is plaintext; output is nonce(12) || ciphertext || tag.
  A missing key file is minted (32 random bytes, ``O_EXCL``, ``0600``).
* ``decrypt``: payload is nonce || ciphertext || tag; output is plaintext. A
  missing key is never minted.

Exit codes: 0 ok, 2 usage, 3 authentication failed, 4 key unavailable/unsafe.

It imports only the stdlib and ``cryptography`` (never ``arctrust``), so it can
run as a standalone script with a fast start.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_BYTES = 32
_NONCE_BYTES = 12
_EXIT_USAGE = 2
_EXIT_DECRYPT = 3
_EXIT_KEY = 4


class _KeyUnavailableError(Exception):
    pass


def _read_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise _KeyUnavailableError from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise _KeyUnavailableError
        if info.st_uid != os.getuid():
            raise _KeyUnavailableError
        key = os.read(fd, _KEY_BYTES + 1)
    finally:
        os.close(fd)
    if len(key) != _KEY_BYTES:
        raise _KeyUnavailableError
    return key


def _mint_or_read_key(path: Path) -> bytes:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return _read_key(path)
    except OSError:
        raise _KeyUnavailableError from None
    try:
        os.write(fd, os.urandom(_KEY_BYTES))
        os.fsync(fd)
    finally:
        os.close(fd)
    return _read_key(path)


def _split(framed: bytes) -> tuple[bytes, bytes]:
    if len(framed) < 4:
        raise ValueError("frame too short")
    size = int.from_bytes(framed[:4], "big")
    if len(framed) < 4 + size:
        raise ValueError("frame truncated")
    return framed[4 : 4 + size], framed[4 + size :]


def main(argv: list[str]) -> int:
    """Encrypt or decrypt the framed stdin payload under the key at ``argv[1]``."""
    if len(argv) != 3 or argv[2] not in ("encrypt", "decrypt"):
        return _EXIT_USAGE
    path, action = Path(argv[1]), argv[2]
    try:
        aad, payload = _split(sys.stdin.buffer.read())
    except ValueError:
        return _EXIT_USAGE
    try:
        key = _mint_or_read_key(path) if action == "encrypt" else _read_key(path)
    except _KeyUnavailableError:
        return _EXIT_KEY
    cipher = AESGCM(key)
    if action == "encrypt":
        nonce = os.urandom(_NONCE_BYTES)
        out = nonce + cipher.encrypt(nonce, payload, aad)
    else:
        try:
            out = cipher.decrypt(payload[:_NONCE_BYTES], payload[_NONCE_BYTES:], aad)
        except (InvalidTag, ValueError):
            return _EXIT_DECRYPT
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — process entry point
    raise SystemExit(main(sys.argv))
