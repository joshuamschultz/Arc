"""``arc schedule`` — list schedules and re-approve legacy ones on the running agent.

::

    arc schedule list    --agent NAME --email EMAIL [--url URL] [--json]
    arc schedule approve ID --agent NAME --email EMAIL [--yes] [--url URL]

A schedule created before the control authority has no signed revision and never
fires. ``approve`` registers its CURRENT definition through the authority (the
operator signs it; the server audits it). Like ``arc pulse`` the CLI never builds
an offline agent: it logs in to the serve process over ArcUI's HTTP API.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from arccli.commands._operator_http import (
    DEFAULT_URL,
    agent_name,
    fail,
    operator_session,
    server_url,
)
from arccli.commands._shared import dispatch, print_json, write
from arccli.commands.skill_improve import add_remote_arguments, confirm

_LIST_PROG = "arc schedule list"
_APPROVE_PROG = "arc schedule approve"
_NEEDS_APPROVAL = "Needs approval — re-create or approve"


def _target(prog: str, args: argparse.Namespace) -> tuple[str, str, str]:
    """(server URL, API base path, email), validated before any credential is sent."""
    if not args.agent or not args.email:
        fail(prog, "this runs on the running agent: pass --agent <name> and --email <operator>")
    url = server_url(prog, args.url)
    return url, f"/api/agents/{agent_name(prog, args.agent)}/schedules", args.email


def _state(row: dict[str, Any]) -> str:
    if row.get("approval") is None:
        return _NEEDS_APPROVAL
    return "on" if row.get("enabled") else "off"


def _timing(row: dict[str, Any]) -> str:
    return str(row.get("expression") or row.get("every_seconds") or row.get("at") or "")


def list_handler(args: argparse.Namespace) -> None:
    """``arc schedule list``."""
    url, base, email = _target(_LIST_PROG, args)
    with operator_session(_LIST_PROG, url, email) as call:
        rows: list[dict[str, Any]] = call("GET", base).get("schedules", [])
    if args.json:
        print_json({"schedules": rows})
        return
    if not rows:
        write("No schedules.")
    for row in rows:
        write(f"{row['id']}: {row.get('type', '')} {_timing(row)} [{_state(row)}]")


def approve_handler(args: argparse.Namespace) -> None:
    """``arc schedule approve``."""
    url, base, email = _target(_APPROVE_PROG, args)
    with operator_session(_APPROVE_PROG, url, email) as call:
        rows: list[dict[str, Any]] = call("GET", base).get("schedules", [])
        row = next((r for r in rows if r.get("id") == args.schedule_id), None)
        if row is None:
            fail(_APPROVE_PROG, f"no schedule with id '{args.schedule_id}'")
        if row.get("approval") is not None:
            fail(_APPROVE_PROG, "schedule is already approved")
        write(f"{row['id']}: {row.get('type', '')} {_timing(row)}")
        if not args.yes and not confirm(f"Approve '{row['id']}' as shown? [y/N] "):
            fail(_APPROVE_PROG, "not approved")
        result = call("POST", f"{base}/{quote(args.schedule_id, safe=':')}/approve")
    revision = (result.get("approval") or {}).get("revision")
    write(f"Approved '{args.schedule_id}' as revision {revision}.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc schedule", description="List and re-approve schedules on the running agent."
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    listing = subs.add_parser("list", help="List schedules and their approval state.")
    add_remote_arguments(listing, required=False)
    listing.add_argument("--json", action="store_true", help="Emit JSON.")

    approve = subs.add_parser("approve", help="Approve a legacy schedule's current definition.")
    approve.add_argument("schedule_id", help="Schedule id to approve.")
    add_remote_arguments(approve, required=False)
    approve.add_argument("--yes", action="store_true", help="Approve without asking.")
    return parser


_SUBCOMMAND_MAP: Mapping[str, Callable[[argparse.Namespace], None]] = {
    "list": list_handler,
    "approve": approve_handler,
}


def schedule_handler(args: list[str]) -> None:
    """Top-level handler for ``arc schedule <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["DEFAULT_URL", "schedule_handler"]
