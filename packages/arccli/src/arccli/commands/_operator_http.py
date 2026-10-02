"""Operator calls to the RUNNING serve process over ArcUI's HTTP API.

Shared by the CLI commands that act on a running agent (``arc agent promotion
run``, ``arc skill improve``, ``arc skill evals run|regen``). The CLI never
builds a second, offline agent: it logs in with an operator account, makes its
calls, and always logs out.

Safety, all checked before any credential leaves the box:

* the password comes from a hidden prompt only (no password/token flag);
* plain HTTP only to loopback — a remote URL must be HTTPS;
* the agent name (and any other path segment) must be one safe segment;
* a non-operator session is refused before any control request.
"""

from __future__ import annotations

import getpass
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import httpx

from arccli.commands._shared import err

DEFAULT_URL = "http://127.0.0.1:8420"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
#: Login/logout are quick; each control call passes its own, longer timeout.
_AUTH_TIMEOUT_SECONDS = 10.0

#: ``call(method, path, json=..., timeout=..., params=...)`` → the JSON object reply.
OperatorCall = Callable[..., dict[str, Any]]


def fail(prog: str, message: str) -> NoReturn:
    err(f"{prog}: {message}")
    sys.exit(1)


def safe_segment(prog: str, value: str, what: str) -> str:
    """``value`` when it is one safe URL path segment; otherwise refuse."""
    if not _SAFE_SEGMENT.fullmatch(value):
        fail(prog, f"{what} must be one path segment of letters, digits, '.', '_' or '-'")
    return value


def agent_name(prog: str, target: str) -> str:
    """The roster name ArcUI routes on: an agent directory's ``[agent].name``, else ``target``."""
    config = Path(target).expanduser() / "arcagent.toml" if target else None
    name: object = target
    if config is not None and config.is_file():
        try:
            name = tomllib.loads(config.read_text(encoding="utf-8")).get("agent", {}).get("name")
        except (OSError, tomllib.TOMLDecodeError):
            fail(prog, f"cannot read {config}")
    if not isinstance(name, str):
        fail(prog, "agent name must be one path segment of letters, digits, '.', '_' or '-'")
    return safe_segment(prog, name, "agent name")


def server_url(prog: str, raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        fail(prog, "server URL must contain only scheme and host")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in _LOOPBACK):
        fail(prog, "a remote server requires HTTPS")
    return raw.rstrip("/")


def _request(
    prog: str, client: httpx.Client, method: str, path: str, **kwargs: Any
) -> dict[str, Any]:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError:
        fail(prog, "operator server is unavailable")
    try:
        body = response.json()
    except ValueError:
        fail(prog, "operator server returned an invalid response")
    if not isinstance(body, dict):
        fail(prog, "operator server returned an invalid response")
    if response.status_code >= 400:
        fail(prog, str(body.get("error") or body.get("reason") or "request refused"))
    return body


def _login(prog: str, client: httpx.Client, email: str, password: str) -> str:
    credentials = {"email": email, "password": password}
    login = _request(prog, client, "POST", "/api/auth/login", json=credentials)
    token = login.get("token")
    if not isinstance(token, str) or not token:
        fail(prog, "operator session is unavailable")
    client.headers["Authorization"] = f"Bearer {token}"
    return str(login.get("role", ""))


@contextmanager
def operator_session(prog: str, url: str, email: str) -> Iterator[OperatorCall]:
    """Log in as an operator; yield a call function; always log out."""
    password = getpass.getpass("Arc account password: ")
    if not password:
        fail(prog, "account password is required")
    with httpx.Client(
        base_url=url, timeout=_AUTH_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    ) as client:
        role = _login(prog, client, email, password)
        try:
            if role != "operator":
                fail(prog, "an operator account is required")

            def call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
                return _request(prog, client, method, path, **kwargs)

            yield call
        finally:
            try:
                client.post("/api/auth/logout")
            except httpx.HTTPError:
                pass


__all__ = [
    "DEFAULT_URL",
    "OperatorCall",
    "agent_name",
    "fail",
    "operator_session",
    "safe_segment",
    "server_url",
]
