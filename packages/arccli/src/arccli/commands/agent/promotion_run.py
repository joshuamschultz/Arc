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
from typing import Any

import arcagent

from arccli.commands._operator_http import DEFAULT_URL, agent_name, operator_session, server_url
from arccli.commands._shared import print_json, write

_PROG = "arc agent promotion run"
_MAX_ITEMS = arcagent.MEMORY_PROMOTION_MAX_ITEMS
_COUNTS = ("evaluated", "promoted", "kept_private", "blocked_secret", "too_large", "deferred")
#: A backfill run may classify thousands of items in turn.
_RUN_TIMEOUT_SECONDS = 3600.0


def _max_items_arg(raw: str) -> int:
    """Argparse type for ``--max-items``: an int in 1..5000, refused before any request."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {_MAX_ITEMS}") from None
    if not 1 <= value <= _MAX_ITEMS:
        raise argparse.ArgumentTypeError(f"must be from 1 to {_MAX_ITEMS}")
    return value


def _run_on_server(url: str, email: str, name: str, max_items: int | None) -> dict[str, Any]:
    body = {} if max_items is None else {"max_items": max_items}
    with operator_session(_PROG, url, email) as call:
        path = f"/api/agents/{name}/memory/promotion/run"
        return call("POST", path, json=body, timeout=_RUN_TIMEOUT_SECONDS)


def run_promotion(args: argparse.Namespace) -> None:
    """Entry for ``arc agent promotion run``."""
    url = server_url(_PROG, args.url)
    name = agent_name(_PROG, args.target)
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
    p.add_argument("--url", default=DEFAULT_URL, help="ArcUI server URL.")
    p.add_argument("--email", required=True, help="Arc operator account email.")


__all__ = ["add_run_parser", "run_promotion"]
