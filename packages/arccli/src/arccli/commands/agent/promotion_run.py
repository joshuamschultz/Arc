"""``arc agent promotion run`` — "Run now" from the CLI (SPEC-083 COMP-029, REQ-512).

::

    arc agent promotion run <agent-dir-or-name> [--max-items N] [--json]
                            [--url URL] --email EMAIL

Runs memory promotion ONCE NOW on the RUNNING agent — a backfill over the memory
it already holds; afterwards the nightly sweep sends only new or changed items.
The CLI never builds a second, offline agent: like ``arc queue`` it talks to the
serve process (``arc ui`` / ``arc.service``) over ArcUI's account-authenticated
HTTP API — login, ``POST /api/agents/{id}/memory/promotion/run``, logout.

Safety, all checked before any credential leaves the box:

* the password comes from a hidden prompt only (no password/token flag);
* plain HTTP only to loopback — a remote URL must be HTTPS;
* ``--max-items`` must be 1..5000 and the agent name one safe path segment
  (it becomes part of the request path);
* a non-operator session is refused before the run request; logout always runs.

The server writes the ``memory.promotion.manual_run`` audit record (actor, agent,
cap — never content).
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import arcagent
import httpx

from arccli.commands._shared import err, print_json, write

_PROG = "arc agent promotion run"
_DEFAULT_URL = "http://127.0.0.1:8420"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_ITEMS = arcagent.MEMORY_PROMOTION_MAX_ITEMS
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_COUNTS = ("evaluated", "promoted", "kept_private", "blocked_secret", "too_large", "deferred")
#: Login/logout are quick; a backfill run may classify thousands of items in turn.
_AUTH_TIMEOUT_SECONDS = 10.0
_RUN_TIMEOUT_SECONDS = 3600.0


def _fail(message: str) -> NoReturn:
    err(f"{_PROG}: {message}")
    sys.exit(1)


def _max_items_arg(raw: str) -> int:
    """Argparse type for ``--max-items``: an int in 1..5000, refused before any request."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {_MAX_ITEMS}") from None
    if not 1 <= value <= _MAX_ITEMS:
        raise argparse.ArgumentTypeError(f"must be from 1 to {_MAX_ITEMS}")
    return value


def _agent_name(target: str) -> str:
    """The roster name ArcUI routes on: an agent directory's ``[agent].name``, else ``target``."""
    config = Path(target).expanduser() / "arcagent.toml" if target else None
    name: object = target
    if config is not None and config.is_file():
        try:
            name = tomllib.loads(config.read_text(encoding="utf-8")).get("agent", {}).get("name")
        except (OSError, tomllib.TOMLDecodeError):
            _fail(f"cannot read {config}")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        _fail("agent name must be one path segment of letters, digits, '.', '_' or '-'")
    return name


def _server_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        _fail("server URL must contain only scheme and host")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in _LOOPBACK):
        _fail("a remote server requires HTTPS")
    return raw.rstrip("/")


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError:
        _fail("operator server is unavailable")
    try:
        body = response.json()
    except ValueError:
        _fail("operator server returned an invalid response")
    if not isinstance(body, dict):
        _fail("operator server returned an invalid response")
    if response.status_code >= 400:
        _fail(str(body.get("error", "request refused")))
    return body


def _login(client: httpx.Client, email: str, password: str) -> str:
    credentials = {"email": email, "password": password}
    login = _request(client, "POST", "/api/auth/login", json=credentials)
    token = login.get("token")
    if not isinstance(token, str) or not token:
        _fail("operator session is unavailable")
    client.headers["Authorization"] = f"Bearer {token}"
    return str(login.get("role", ""))


def _run_on_server(url: str, email: str, name: str, max_items: int | None) -> dict[str, Any]:
    password = getpass.getpass("Arc account password: ")
    if not password:
        _fail("account password is required")
    body = {} if max_items is None else {"max_items": max_items}
    with httpx.Client(
        base_url=url, timeout=_AUTH_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    ) as client:
        role = _login(client, email, password)
        try:
            if role != "operator":
                _fail("an operator account is required")
            path = f"/api/agents/{name}/memory/promotion/run"
            return _request(client, "POST", path, json=body, timeout=_RUN_TIMEOUT_SECONDS)
        finally:
            try:
                client.post("/api/auth/logout")
            except httpx.HTTPError:
                pass


def run_promotion(args: argparse.Namespace) -> None:
    """Entry for ``arc agent promotion run``."""
    url = _server_url(args.url)
    name = _agent_name(args.target)
    result = _run_on_server(url, args.email, name, args.max_items)
    wire = {"status": str(result.get("status", ""))}
    wire.update({count: result.get(count, 0) for count in _COUNTS})
    if args.json:
        print_json(wire)
        return
    write(f"Promotion run on {name}: {wire['status']}")
    for count in _COUNTS:
        write(f"  {count.replace('_', ' ')}: {wire[count]}")


def add_run_parser(verbs: Any) -> None:
    """Register ``run`` on the ``arc agent promotion`` verbs."""
    # No password/token flag: a credential on argv is shell history (D-555).
    p = verbs.add_parser("run", help="Run promotion now on the running agent (backfill).")
    p.add_argument("target", help="Agent directory or roster name.")
    p.add_argument("--max-items", type=_max_items_arg, default=None, help="Cap for this run only.")
    p.add_argument("--json", action="store_true", help="Emit JSON.")
    p.add_argument("--url", default=_DEFAULT_URL, help="ArcUI server URL.")
    p.add_argument("--email", required=True, help="Arc operator account email.")


__all__ = ["add_run_parser", "run_promotion"]
