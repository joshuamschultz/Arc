"""``arc stop`` — operator kill switch for a running agent run (no SSH required).

Agents run too long and, since the CLI is a separate process from the agent, it
can't reach a live run directly. This command writes a ``pending`` cancel request
to the shared arcstore ``cancellations`` directory; the target agent's run-control
watcher observes it and cooperatively stops the matching run (``RunHandle.cancel``).

``stop list``                 — show pending cancel requests.
``stop <run_id>``             — request cancellation of the run with that id.
``stop <run_id> --reason ...`` — attach an operator reason (carried into the audit).
``stop --session <key>``      — target by session key instead of run id.

The request is attributed to the DEPLOYMENT operator DID (derived from the on-box
``~/.arc/operator`` key, the same identity ``arc approve`` uses), so the kill switch
is auditable end to end (ASI09/ASI10).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any

from arcstore.cancellations import CancelRequest, CancelStore

from arccli.commands._shared import write as _write
from arccli.formatting import print_table as _print_table


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


async def _open_store() -> tuple[CancelStore, Any]:
    """Open the shared arcstore ``cancellations`` directory (same arcui.db agents use)."""
    from arcstore import store_db_path
    from arcstore.backends.sqlite import SqliteBackend

    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    return CancelStore(backend), backend


def _operator_did() -> str:
    """The canonical deployment operator DID (on-box ~/.arc/operator key).

    The same identity ``arc approve`` mints under — so a cancel is attributed to the
    real operator, not a bare surface label.
    """
    from arctrust.policy import OperatorApprovalAuthority

    from arccli.commands.operator import resolve_operator_signer

    return OperatorApprovalAuthority(resolve_operator_signer()).did


def _row(r: CancelRequest) -> list[str]:
    target = r.run_id or f"session:{r.session_key}"
    who = r.agent_label or "-"
    return [r.id, r.status, who, target, (r.reason or "")[:32], (r.created_at or "")[:19]]


def _list(_args: argparse.Namespace) -> None:
    async def _run() -> None:
        store, backend = await _open_store()
        try:
            pending = await store.list(status="pending")
        finally:
            await backend.stop()
        if not pending:
            _write("No pending cancel requests.")
            return
        _print_table(
            ["ID", "STATUS", "AGENT", "TARGET", "REASON", "CREATED"],
            [_row(r) for r in pending],
        )

    asyncio.run(_run())


def _request(args: argparse.Namespace) -> None:
    run_id = "" if args.target in (None, "list") else args.target
    session = getattr(args, "session", "") or ""
    if not run_id and not session:
        _err("arc stop: give a <run_id> or --session <key> to cancel")
        sys.exit(1)

    async def _run() -> None:
        operator_did = _operator_did()
        store, backend = await _open_store()
        try:
            req = await store.create(
                CancelRequest(
                    id=uuid.uuid4().hex[:16],
                    run_id=run_id,
                    session_key=session,
                    requested_by=operator_did,
                    reason=getattr(args, "reason", "") or "",
                )
            )
        finally:
            await backend.stop()
        target = run_id or f"session {session}"
        _write(f"Requested cancel of {target} (request {req.id}). The agent will stop it shortly.")

    asyncio.run(_run())


def _build_parser() -> argparse.ArgumentParser:
    # One positional so `arc stop list` and `arc stop <run_id>` share a parser
    # without a subparser swallowing the id as an unknown subcommand (mirrors approve).
    parser = argparse.ArgumentParser(prog="arc stop", add_help=True)
    parser.add_argument(
        "target", nargs="?", help="'list' to show pending requests, or a run id to cancel"
    )
    parser.add_argument("--session", default="", help="Cancel by session key instead of run id")
    parser.add_argument("--reason", default="", help="Operator reason (recorded in the audit)")
    return parser


def stop_handler(args: list[str]) -> None:
    parser = _build_parser()
    ns = parser.parse_args(args)
    if ns.target == "list":
        _list(ns)
        return
    if ns.target is None and not ns.session:
        parser.print_help()
        return
    _request(ns)


__all__ = ["stop_handler"]
