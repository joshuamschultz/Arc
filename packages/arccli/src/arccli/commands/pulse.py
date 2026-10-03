"""``arc pulse`` — add, edit, remove, review and approve pulse checks on the running agent.

::

    arc pulse status  --agent NAME --email EMAIL [--url URL] [--json]
    arc pulse add     --agent NAME --email EMAIL --name N --every 30|2h|1d --action TEXT
    arc pulse edit    --agent NAME --email EMAIL --name N [--every …] [--action TEXT]
    arc pulse remove  --agent NAME --email EMAIL --name N [--yes]
    arc pulse approve --agent NAME --email EMAIL [--check NAME] [--yes] [--url URL]

``add``/``edit``/``remove`` write ``pulse.md`` through the one operator-only audited
route. A write never approves: the check shows "pending approval" until ``approve``.

A pulse check runs only from an operator-approved revision. ``status`` lists every
check in the agent's ``pulse.md`` with its review state; ``approve`` shows each
pending check's diff against the last approved definition, asks for confirmation
(``--yes`` skips it), then approves exactly the definition it showed -- the request
carries the digest that was listed, so an edit in between is refused by the server.

Like ``arc agent promotion run`` the CLI never builds an offline agent: it logs in
to the serve process over ArcUI's HTTP API (hidden password prompt, no token flag)
and always logs out. The server signs the revision and audits every approval.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping
from typing import Any

from arccli.commands._operator_http import (
    DEFAULT_URL,
    OperatorCall,
    agent_name,
    fail,
    operator_session,
    server_url,
)
from arccli.commands._shared import dispatch, print_json, write
from arccli.commands.skill_improve import add_remote_arguments, confirm

_STATUS_PROG = "arc pulse status"
_APPROVE_PROG = "arc pulse approve"
_ADD_PROG = "arc pulse add"
_EDIT_PROG = "arc pulse edit"
_REMOVE_PROG = "arc pulse remove"
_EVERY_RE = re.compile(r"^(\d+)([mhd]?)$")
_UNIT_MINUTES = {"": 1, "m": 1, "h": 60, "d": 1440}


def _target(prog: str, args: argparse.Namespace) -> tuple[str, str, str]:
    """(server URL, API base path, email), validated before any credential is sent."""
    if not args.agent or not args.email:
        fail(prog, "this runs on the running agent: pass --agent <name> and --email <operator>")
    url = server_url(prog, args.url)
    return url, f"/api/agents/{agent_name(prog, args.agent)}/pulse", args.email


def _label(status: str) -> str:
    return status.replace("_", " ")


def status_handler(args: argparse.Namespace) -> None:
    """``arc pulse status``."""
    url, base, email = _target(_STATUS_PROG, args)
    with operator_session(_STATUS_PROG, url, email) as call:
        listing = call("GET", base)
    if args.json:
        print_json(listing)
        return
    checks: list[dict[str, Any]] = listing.get("checks", [])
    if not checks:
        write("No pulse checks.")
    for check in checks:
        ran = check.get("last_revision_ran")
        write(
            f"{check['name']}: {_label(check['status'])} "
            f"(every {check['interval_minutes']} min, last ran revision "
            f"{'none' if ran is None else ran})"
        )


def _pending(checks: list[dict[str, Any]], only: str | None) -> list[dict[str, Any]]:
    if only is not None and not any(check["name"] == only for check in checks):
        fail(_APPROVE_PROG, f"no pulse check named '{only}'")
    return [
        check
        for check in checks
        if not check["approved"] and (only is None or check["name"] == only)
    ]


def _approve_one(
    call: OperatorCall, base: str, check: dict[str, Any], *, assume_yes: bool
) -> None:
    write(f"--- {check['name']} ({_label(check['status'])}) ---")
    write(check.get("diff") or f"action: {check['action']}")
    if not assume_yes and not confirm(f"Approve '{check['name']}' as shown? [y/N] "):
        fail(_APPROVE_PROG, "not approved")
    result = call(
        "POST",
        f"{base}/approve",
        json={"check": check["name"], "definition_digest": check["definition_digest"]},
    )
    write(f"Approved '{check['name']}' as revision {result.get('revision')}.")


def approve_handler(args: argparse.Namespace) -> None:
    """``arc pulse approve``."""
    url, base, email = _target(_APPROVE_PROG, args)
    with operator_session(_APPROVE_PROG, url, email) as call:
        pending = _pending(call("GET", base).get("checks", []), args.check)
        if not pending:
            write("Nothing to approve.")
            return
        for check in pending:
            _approve_one(call, base, check, assume_yes=args.yes)


def _minutes(prog: str, raw: str) -> int:
    """``30`` / ``30m`` / ``2h`` / ``1d`` as minutes; anything else fails before login."""
    match = _EVERY_RE.fullmatch(raw.strip().lower())
    if match is None or int(match.group(1)) == 0:
        fail(prog, "--every must be minutes, or a number with m, h or d (for example 30, 2h, 1d)")
    return int(match.group(1)) * _UNIT_MINUTES[match.group(2)]


def add_handler(args: argparse.Namespace) -> None:
    """``arc pulse add``."""
    minutes = _minutes(_ADD_PROG, args.every)
    url, base, email = _target(_ADD_PROG, args)
    body = {"name": args.name, "interval_minutes": minutes, "action": args.action}
    with operator_session(_ADD_PROG, url, email) as call:
        call("POST", base, json=body)
    write(
        f"Added '{args.name}' (every {minutes} min). It is pending approval: run arc pulse approve."
    )


def _find(call: OperatorCall, prog: str, base: str, name: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = call("GET", base).get("checks", [])
    for check in checks:
        if check["name"] == name:
            return check
    fail(prog, f"no pulse check named '{name}'")


def edit_handler(args: argparse.Namespace) -> None:
    """``arc pulse edit``: change the interval and/or action of one check."""
    if args.every is None and args.action is None:
        fail(_EDIT_PROG, "nothing to change: pass --every and/or --action")
    minutes = None if args.every is None else _minutes(_EDIT_PROG, args.every)
    url, base, email = _target(_EDIT_PROG, args)
    with operator_session(_EDIT_PROG, url, email) as call:
        current = _find(call, _EDIT_PROG, base, args.name)
        body = {
            "interval_minutes": current["interval_minutes"] if minutes is None else minutes,
            "action": current["action"] if args.action is None else args.action,
            "definition_digest": current["definition_digest"],
        }
        call("PUT", f"{base}/{args.name}", json=body)
    write(f"Edited '{args.name}'. It is pending approval: run arc pulse approve.")


def remove_handler(args: argparse.Namespace) -> None:
    """``arc pulse remove``."""
    url, base, email = _target(_REMOVE_PROG, args)
    with operator_session(_REMOVE_PROG, url, email) as call:
        current = _find(call, _REMOVE_PROG, base, args.name)
        if not args.yes and not confirm(f"Remove pulse check '{args.name}'? [y/N] "):
            fail(_REMOVE_PROG, "not removed")
        call(
            "DELETE",
            f"{base}/{args.name}",
            json={"definition_digest": current["definition_digest"]},
        )
    write(f"Removed '{args.name}'.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc pulse", description="Add, edit, remove, review and approve pulse checks."
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    status = subs.add_parser("status", help="List pulse checks and their approval state.")
    add_remote_arguments(status, required=False)
    status.add_argument("--json", action="store_true", help="Emit JSON.")

    add = subs.add_parser("add", help="Add a pulse check (pending approval).")
    add_remote_arguments(add, required=False)
    add.add_argument("--name", required=True, help="Check name (letters, digits, - _ .).")
    add.add_argument("--every", required=True, help="Interval: minutes, or 30m / 2h / 1d.")
    add.add_argument("--action", required=True, help="What the agent should check.")

    edit = subs.add_parser("edit", help="Change a pulse check (back to pending approval).")
    add_remote_arguments(edit, required=False)
    edit.add_argument("--name", required=True, help="Check to change.")
    edit.add_argument("--every", default=None, help="New interval: minutes, or 30m / 2h / 1d.")
    edit.add_argument("--action", default=None, help="New action text.")

    remove = subs.add_parser("remove", help="Remove a pulse check.")
    add_remote_arguments(remove, required=False)
    remove.add_argument("--name", required=True, help="Check to remove.")
    remove.add_argument("--yes", action="store_true", help="Remove without asking.")

    approve = subs.add_parser("approve", help="Review and approve pending pulse checks.")
    add_remote_arguments(approve, required=False)
    approve.add_argument("--check", default=None, help="Approve only this check.")
    approve.add_argument("--yes", action="store_true", help="Approve without asking.")
    return parser


_SUBCOMMAND_MAP: Mapping[str, Callable[[argparse.Namespace], None]] = {
    "status": status_handler,
    "add": add_handler,
    "edit": edit_handler,
    "remove": remove_handler,
    "approve": approve_handler,
}


def pulse_handler(args: list[str]) -> None:
    """Top-level handler for ``arc pulse <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["DEFAULT_URL", "pulse_handler"]
