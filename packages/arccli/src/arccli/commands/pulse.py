"""``arc pulse`` — review and approve pulse checks on the running agent.

::

    arc pulse status  --agent NAME --email EMAIL [--url URL] [--json]
    arc pulse approve --agent NAME --email EMAIL [--check NAME] [--yes] [--url URL]

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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc pulse", description="Review and approve pulse checks on the running agent."
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    status = subs.add_parser("status", help="List pulse checks and their approval state.")
    add_remote_arguments(status, required=False)
    status.add_argument("--json", action="store_true", help="Emit JSON.")

    approve = subs.add_parser("approve", help="Review and approve pending pulse checks.")
    add_remote_arguments(approve, required=False)
    approve.add_argument("--check", default=None, help="Approve only this check.")
    approve.add_argument("--yes", action="store_true", help="Approve without asking.")
    return parser


_SUBCOMMAND_MAP: Mapping[str, Callable[[argparse.Namespace], None]] = {
    "status": status_handler,
    "approve": approve_handler,
}


def pulse_handler(args: list[str]) -> None:
    """Top-level handler for ``arc pulse <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["DEFAULT_URL", "pulse_handler"]
