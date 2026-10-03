"""``arc approve`` — mechanical operator approval for blocked agent actions (SPEC-035).

When an agent's completing call trips the Lethal-Trifecta gate, it parks a pending
request in the shared arcstore ``approvals`` directory and waits. This command is
the on-box, key-holding operator surface that resolves it — approval never travels
over agent chat (which a prompt-injected or foreign message could forge).

``approve list``             — show pending requests and standing approvals.
``approve <id>``             — mint an operator-signed grant for that request.
``approve <id> --always``    — approve it AND make its scope stand ("Always
                               allow": agent + verb + leg composition + egress
                               destination; never for a federal request).
``approve <id> --deny``      — deny it.
``approve revoke <grant>``   — revoke a standing approval; the next call prompts.

The grant is signed with the DEPLOYMENT operator key (``~/.arc/operator``, the same
key the agent's gate pins to), so only someone with on-box operator-key access can
approve. A different key produces a different approver DID and the gate rejects it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.standing_grants import (
    StandingGrant,
    StandingGrantRefusedError,
    StandingGrantStore,
    approve_always,
)
from arctrust.policy import OperatorApprovalAuthority, grant_to_wire, sign_approval_for_hash

from arccli.commands._shared import write as _write
from arccli.formatting import print_table as _print_table

if TYPE_CHECKING:
    from arcstore.backends import ArcStoreBackend


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _backend_factory() -> ArcStoreBackend:
    """Create the configured ArcStore backend for one command invocation."""
    from arcstore.backends import open_backend

    return open_backend()


async def _open_store() -> tuple[ApprovalStore, ArcStoreBackend]:
    """Open the configured arcstore approvals directory."""

    backend = _backend_factory()
    await backend.start()
    return ApprovalStore(backend), backend


def _row(a: PendingApproval) -> list[str]:
    legs = "+".join(a.legs)
    who = a.agent_label or a.agent_did.rsplit("/", 1)[-1]
    session = (a.session_id or "")[:12]
    return [a.id, a.status, who, a.tool, legs, session, (a.created_at or "")[:19]]


def _print_context(a: PendingApproval) -> None:
    """Print the SPEC-035 triage context for one request: what + why.

    ``arguments`` (WHAT the completing call acts on) and ``provenance`` (WHICH
    prior calls lit each trifecta leg, and when) are already redacted/bounded by
    the agent — printed verbatim so the operator can decide before signing.
    """
    if a.session_id:
        _write(f"  session:  {a.session_id}")
    if a.arguments:
        _write("  arguments:")
        for name, value in a.arguments.items():
            _write(f"    {name}: {value}")
    if a.provenance:
        _write("  leg provenance (what lit each trifecta leg):")
        for entry in a.provenance:
            legs = "+".join(entry.get("legs", []))
            tool = entry.get("tool", "")
            _write(f"    [{legs}] {tool}: {entry.get('args', '')} @ {entry.get('at', '')}")


def _standing_row(g: StandingGrant) -> list[str]:
    who = g.agent_label or g.agent_did.rsplit("/", 1)[-1]
    return [
        g.id,
        who,
        g.tool,
        "+".join(g.composition),
        g.destination or "(no egress)",
        g.granted_by.rsplit("/", 1)[-1],
        (g.granted_at or "")[:19],
        str(g.use_count),
    ]


def _list(_args: argparse.Namespace) -> None:
    async def _run() -> None:
        store, backend = await _open_store()
        try:
            pending = await store.list(status="pending")
            standing = await StandingGrantStore(backend).list(status="active")
        finally:
            await backend.stop()
        if not pending:
            _write("No pending approvals.")
        else:
            _print_table(
                ["ID", "STATUS", "AGENT", "TOOL", "COMPOSITION", "SESSION", "CREATED"],
                [_row(a) for a in pending],
            )
            for a in pending:
                _write(f"\n{a.id} ({a.tool}):")
                _print_context(a)
        _write("\nStanding approvals (Always allow):")
        if not standing:
            _write("  none")
            return
        _print_table(
            ["GRANT", "AGENT", "TOOL", "COMPOSITION", "DESTINATION", "GRANTED BY", "WHEN", "USES"],
            [_standing_row(g) for g in standing],
        )

    asyncio.run(_run())


def _revoke(grant_id: str) -> None:
    async def _run() -> None:
        _, backend = await _open_store()
        try:
            from arccli.commands.operator import resolve_operator_signer

            operator = OperatorApprovalAuthority(resolve_operator_signer())
            revoked = await StandingGrantStore(backend).revoke(grant_id, actor_did=operator.did)
        finally:
            await backend.stop()
        if revoked is None:
            _err(f"arc approve: no active standing approval {grant_id!r}")
            sys.exit(1)
        _write(
            f"Revoked {grant_id} — {revoked.agent_label or revoked.agent_did} will be asked "
            f"again before {revoked.tool}."
        )

    asyncio.run(_run())


async def _always(
    store: ApprovalStore,
    backend: ArcStoreBackend,
    row: PendingApproval,
    operator: OperatorApprovalAuthority,
) -> None:
    """Approve ``row`` and store its scope as a standing grant (SPEC-035 OQ-3)."""
    try:
        _, stored = await approve_always(store, StandingGrantStore(backend), row, operator)
    except StandingGrantRefusedError as exc:
        _err(f"arc approve: cannot always-allow {row.id!r}: {exc}")
        sys.exit(1)
    who = row.agent_label or "agent"
    _write(
        f"Always allowed {row.id} — {who} may {stored.tool} -> "
        f"{stored.destination or '(no egress)'} with {'+'.join(stored.composition)} "
        f"from now on. Standing approval {stored.id}; revoke with "
        f"`arc approve revoke {stored.id}`."
    )


def _resolve(args: argparse.Namespace) -> None:
    deny = bool(getattr(args, "deny", False))
    always = bool(getattr(args, "always", False))
    if deny and always:
        _err("arc approve: --deny and --always are exclusive")
        sys.exit(2)

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

            # Surface the triage context (what + why) before the operator commits.
            _write(f"{row.id} ({row.tool}):")
            _print_context(row)

            if deny:
                resolved = await store.resolve(
                    args.id, status="denied", actor_did="operator", resolved_by="operator"
                )
                _write(f"Denied {args.id}." if resolved else f"Could not deny {args.id}.")
                return

            from arccli.commands.operator import resolve_operator_signer

            operator = OperatorApprovalAuthority(resolve_operator_signer())

            # H-040 §3.3: a foreign-harness enrollment row is signed into an
            # EnrollmentGrant and the verified member is admitted to the registry,
            # rather than an ApprovalGrant over a blocked tool call.
            from arccli.commands.enroll import is_enrollment

            if is_enrollment(row):
                await _approve_enrollment(store, row, operator)
                return

            if always:
                await _always(store, backend, row, operator)
                return

            # Mint an operator-signed grant over the stored call_hash. The operator
            # key IS the authority — the agent's gate verifies + pins to its DID.
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


async def _approve_enrollment(
    store: ApprovalStore, row: PendingApproval, operator: OperatorApprovalAuthority
) -> None:
    """Sign a foreign-harness enrollment grant and admit the member (H-040 §3.3).

    The signed grant rides on the member ``Entity`` in wire form; writing it to the
    registry re-verifies it against the trust-store operator key (chokepoint 1), so
    a bad row never becomes a live member. The row is then marked ``approved`` with
    the grant recorded for the audit trail.
    """
    from arcteam.harness.enrollment import EnrollmentDenied

    from arccli.commands.enroll import sign_enrollment_from_row
    from arccli.commands.team import _build_service, _get_root, _shutdown

    grant, entity = sign_enrollment_from_row(row, operator)

    root = _get_root(argparse.Namespace(root=None))  # TeamConfig().root default
    _, registry, _, backend = await _build_service(root)
    try:
        try:
            await registry.register(entity)
        except EnrollmentDenied as exc:
            _err(f"arc approve: enrollment refused — {exc}")
            sys.exit(1)
    finally:
        await _shutdown(backend)

    from arctrust.policy import enrollment_to_wire

    resolved = await store.resolve(
        row.id,
        status="approved",
        actor_did=operator.did,
        resolved_by=operator.did,
        grant=enrollment_to_wire(grant),
    )
    if resolved is None:
        _err(f"arc approve: {row.id!r} raced out of pending; not approved")
        sys.exit(1)
    _write(f"Enrolled {entity.handle} (harness={entity.harness}, did={entity.did}).")


def _build_parser() -> argparse.ArgumentParser:
    # One positional so `arc approve list` and `arc approve <id>` share a parser
    # without a subparser swallowing the id as an unknown subcommand.
    parser = argparse.ArgumentParser(prog="arc approve", add_help=True)
    parser.add_argument(
        "target",
        nargs="?",
        help="'list', 'revoke', or an approval request id to resolve",
    )
    parser.add_argument(
        "grant_id", nargs="?", help="with 'revoke': the standing approval to revoke"
    )
    parser.add_argument("--deny", action="store_true", help="Deny instead of approve")
    parser.add_argument(
        "--always",
        action="store_true",
        help="Approve and make this scope stand (Always allow; never at federal)",
    )
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
    if ns.target == "revoke":
        if not ns.grant_id:
            _err("arc approve revoke: name the standing approval (see `arc approve list`)")
            sys.exit(2)
        _revoke(ns.grant_id)
        return
    ns.id = ns.target
    _resolve(ns)


__all__ = ["approve_handler"]
