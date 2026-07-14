"""``arc approve`` — mechanical operator approval for blocked agent actions (SPEC-035).

When an agent's completing call trips the Lethal-Trifecta gate, it parks a pending
request in the shared arcstore ``approvals`` directory and waits. This command is
the on-box, key-holding operator surface that resolves it — approval never travels
over agent chat (which a prompt-injected or foreign message could forge).

``approve list``            — show pending requests.
``approve <id>``            — mint an operator-signed grant for that request.
``approve <id> --deny``     — deny it.

The grant is signed with the DEPLOYMENT operator key (``~/.arc/operator``, the same
key the agent's gate pins to), so only someone with on-box operator-key access can
approve. A different key produces a different approver DID and the gate rejects it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from arcstore.approvals import ApprovalStore, PendingApproval
from arctrust.policy import OperatorApprovalAuthority, grant_to_wire, sign_approval_for_hash

from arccli.commands._shared import write as _write
from arccli.formatting import print_table as _print_table


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


async def _open_store() -> tuple[ApprovalStore, Any]:
    """Open the shared arcstore ``approvals`` directory (same arcui.db agents use)."""
    from arcstore import store_db_path
    from arcstore.backends.sqlite import SqliteBackend

    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    return ApprovalStore(backend), backend


def _row(a: PendingApproval) -> list[str]:
    legs = "+".join(a.legs)
    who = a.agent_label or a.agent_did.rsplit("/", 1)[-1]
    return [a.id, a.status, who, a.tool, legs, (a.created_at or "")[:19]]


def _list(_args: argparse.Namespace) -> None:
    async def _run() -> None:
        store, backend = await _open_store()
        try:
            pending = await store.list(status="pending")
        finally:
            await backend.stop()
        if not pending:
            _write("No pending approvals.")
            return
        _print_table(
            ["ID", "STATUS", "AGENT", "TOOL", "COMPOSITION", "CREATED"],
            [_row(a) for a in pending],
        )

    asyncio.run(_run())


def _resolve(args: argparse.Namespace) -> None:
    deny = bool(getattr(args, "deny", False))

    async def _run() -> None:
        store, backend = await _open_store()
        try:
            row = await store.get(args.id)
            if row is None:
                _err(f"arc approve: no request {args.id!r}")
                sys.exit(1)
            if row.status != "pending":
                _err(f"arc approve: request {args.id!r} is already {row.status}")
                sys.exit(1)

            if deny:
                resolved = await store.resolve(
                    args.id, status="denied", actor_did="operator", resolved_by="operator"
                )
                _write(f"Denied {args.id}." if resolved else f"Could not deny {args.id}.")
                return

            # Mint an operator-signed grant over the stored call_hash. The operator
            # key IS the authority — the agent's gate verifies + pins to its DID.
            from arccli.commands.operator import resolve_operator_signer

            operator = OperatorApprovalAuthority(resolve_operator_signer())
            grant = sign_approval_for_hash(row.call_hash, operator)
            resolved = await store.resolve(
                args.id,
                status="approved",
                actor_did=operator.did,
                resolved_by=operator.did,
                grant=grant_to_wire(grant),
            )
            if resolved is None:
                _err(f"arc approve: {args.id!r} raced out of pending; not approved")
                sys.exit(1)
            who = row.agent_label or "agent"
            _write(f"Approved {args.id} — {who} may proceed with {row.tool}.")
        finally:
            await backend.stop()

    asyncio.run(_run())


def _build_parser() -> argparse.ArgumentParser:
    # One positional so `arc approve list` and `arc approve <id>` share a parser
    # without a subparser swallowing the id as an unknown subcommand.
    parser = argparse.ArgumentParser(prog="arc approve", add_help=True)
    parser.add_argument(
        "target", nargs="?", help="'list' to show pending, or an approval request id to resolve"
    )
    parser.add_argument("--deny", action="store_true", help="Deny instead of approve")
    return parser


def approve_handler(args: list[str]) -> None:
    parser = _build_parser()
    ns = parser.parse_args(args)
    if ns.target is None:
        parser.print_help()
        return
    if ns.target == "list":
        _list(ns)
        return
    ns.id = ns.target
    _resolve(ns)


__all__ = ["approve_handler"]
