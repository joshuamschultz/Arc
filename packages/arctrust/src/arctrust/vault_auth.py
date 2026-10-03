"""Vault login material and the renewable token a :class:`VaultTransit` holds in memory.

The bootstrap secret (an AppRole ``secret_id`` or a Kubernetes/JWT token) is
read ONCE from a source that never puts it in the config file:

* ``credential:<name>``: ``$CREDENTIALS_DIRECTORY/<name>``, the non-swappable
  ramfs systemd ``LoadCredential=`` / ``LoadCredentialEncrypted=`` mounts for
  the unit only;
* ``fd:<n>``: an inherited file descriptor, read to EOF and closed;
* ``env:<VAR>``: an environment variable, removed from ``os.environ`` once read
  so no child process inherits it;
* ``file:/abs/path``: a tmpfs-projected token (the Kubernetes service account
  token), refused when it is a symlink.

The Vault token minted from it lives only in this process's memory. It is
renewed before it expires (``auth/token/renew-self``) and re-minted from the
retained bootstrap secret when renewal is refused or the max TTL is reached.
Nothing here logs or returns either value.
"""

from __future__ import annotations

import os
import re
import stat
import threading
import time
from collections.abc import Callable
from typing import Any

from arctrust.transit_cipher import TransitUnavailableError

_CREDENTIAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_MAX_SECRET_BYTES = 16 * 1024
_MAX_TTL_S = 7 * 24 * 3600
K8S_TOKEN_SOURCE = "file:/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - a path to a projected token, not a secret


class VaultSecretSourceError(ValueError):
    """The configured bootstrap secret source is malformed or cannot be read."""


def _read_path(path: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise VaultSecretSourceError("vault secret source is not readable") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VaultSecretSourceError("vault secret source is not a regular file")
        return os.read(fd, _MAX_SECRET_BYTES + 1)
    finally:
        os.close(fd)


def _read_fd(raw: str) -> bytes:
    if not raw.isdigit():
        raise VaultSecretSourceError("fd: source needs a descriptor number")
    fd = int(raw)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
            if sum(map(len, chunks)) > _MAX_SECRET_BYTES:
                break
    except OSError:
        raise VaultSecretSourceError("vault secret descriptor is not readable") from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return b"".join(chunks)


def _read_credential(name: str) -> bytes:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not _CREDENTIAL_NAME.fullmatch(name) or not directory:
        raise VaultSecretSourceError(
            "credential: source needs a systemd LoadCredential name and CREDENTIALS_DIRECTORY"
        )
    return _read_path(os.path.join(directory, name))


def _read_env(name: str) -> bytes:
    if not _ENV_NAME.fullmatch(name):
        raise VaultSecretSourceError("env: source needs an upper-case variable name")
    value = os.environ.pop(name, "")
    return value.encode("utf-8")


def read_secret_source(source: str) -> str:
    """Read a bootstrap secret from ``credential:`` / ``fd:`` / ``env:`` / ``file:``."""
    scheme, _, rest = source.partition(":")
    if scheme == "credential":
        raw = _read_credential(rest)
    elif scheme == "fd":
        raw = _read_fd(rest)
    elif scheme == "env":
        raw = _read_env(rest)
    elif scheme == "file" and os.path.isabs(rest):
        raw = _read_path(rest)
    else:
        raise VaultSecretSourceError(
            "vault secret source must be credential:<name>, fd:<n>, env:<VAR> or file:/abs/path"
        )
    if len(raw) > _MAX_SECRET_BYTES:
        raise VaultSecretSourceError("vault secret source is too large")
    secret = raw.decode("utf-8", errors="strict").strip()
    if not secret:
        raise VaultSecretSourceError("vault secret source is empty")
    return secret


#: ``(path, json body) -> Vault response body``; raises TransitUnavailableError.
Post = Callable[[str, dict[str, Any], str | None], dict[str, Any]]


def _lease(body: dict[str, Any]) -> tuple[str | None, int, bool]:
    try:
        auth = body["auth"]
        token = auth.get("client_token")
        ttl = auth["lease_duration"]
        renewable = auth["renewable"]
        if type(ttl) is not int or not 1 <= ttl <= _MAX_TTL_S or type(renewable) is not bool:
            raise ValueError("unsafe Vault token lease")
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("invalid Vault token")
        return token, ttl, renewable
    except (KeyError, TypeError, AttributeError, ValueError):
        raise TransitUnavailableError("Vault login answer is invalid") from None


class TokenSession:
    """One in-memory Vault token: minted on demand, renewed at two-thirds of its TTL."""

    def __init__(
        self,
        *,
        login_path: str,
        login_body: Callable[[], dict[str, str]],
        post: Post,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self._login_path = login_path
        self._login_body = login_body
        self._post = post
        self._clock = clock
        self._on_event = on_event or (lambda _action, _outcome: None)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._deadline = 0.0
        self._renew_at = 0.0
        self._renewable = False

    def token(self) -> str:
        """A live token, renewing or re-minting it first when it is near expiry."""
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if self._token is not None and self._renewable and now < self._deadline:
                if self._renew():
                    return self._token
            return self._login()

    def invalidate(self) -> None:
        """Forget the token (Vault refused it); the next call mints a fresh one."""
        with self._lock:
            self._token = None

    def current(self) -> str | None:
        """The held token, if any, without minting one (for revoke on close)."""
        with self._lock:
            return self._token

    def _set(self, token: str, ttl: int, renewable: bool) -> None:
        now = self._clock()
        self._token = token
        self._renewable = renewable
        self._deadline = now + ttl
        self._renew_at = now + ttl * 2 / 3

    def _login(self) -> str:
        try:
            token, ttl, renewable = _lease(self._post(self._login_path, self._login_body(), None))
        except TransitUnavailableError:
            self._token = None
            self._on_event("custody.transit.login", "error")
            raise
        if token is None:
            self._on_event("custody.transit.login", "error")
            raise TransitUnavailableError("Vault login returned no token")
        self._set(token, ttl, renewable)
        self._on_event("custody.transit.login", "allow")
        return token

    def _renew(self) -> bool:
        token = self._token
        if token is None:
            return False
        try:
            _token, ttl, renewable = _lease(self._post("/v1/auth/token/renew-self", {}, token))
        except TransitUnavailableError:
            self._on_event("custody.transit.renew", "error")
            return False
        # A renewal clipped by the role's max TTL that gains no time is the
        # token's end of life: mint a fresh one from the bootstrap secret.
        if ttl <= self._deadline - self._clock():
            self._on_event("custody.transit.renew", "exhausted")
            return False
        self._set(token, ttl, renewable)
        self._on_event("custody.transit.renew", "allow")
        return True


__all__ = [
    "K8S_TOKEN_SOURCE",
    "TokenSession",
    "VaultSecretSourceError",
    "read_secret_source",
]
