"""`arc callback` — the origins work-event callbacks may be sent to (SPEC-088 COMP-018).

``add``     — allow callbacks to one ``scheme://host[:port]`` origin. At personal
              tier the entry is active at once; at enterprise it is pending until
              an operator runs ``arc approve <id>``. At federal the CLI refuses: its
              only identity is the deployment key, not a person, so the second
              approver could never be checked against the requester.
``list``    — every entry, its origin and status.
``remove``  — stop callbacks to an entry's origin from the next delivery check.
``key``     — mint the work-events signing key if absent and print its key id. ``arc
              init`` mints it on a new deployment; the deploy scripts skip ``arc
              init`` on a box that already has config, so a deployment initialised
              before SPEC-088 needs this once before any callback is sent.

The allowlist is a sealed, anchored record in the account authority; every
change is audited by the store itself (``work_event.callback_allowlist.<verb>``
on the operator chain) and refused when that audit cannot be written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, NoReturn

from arctrust.authority import AccountAuthorityError
from arctrust.authority_config import AuthorityConfigError
from arctrust.callback_allowlist import (
    CALLBACK_ALLOWLIST_TOOL,
    AllowlistEntry,
    CallbackAllowlistStore,
    NewCallbackEntry,
)
from arctrust.classification import Classification
from arctrust.monotonic import AnchorUnavailableError
from arctrust.users import UserStoreError
from arctrust.vault_lease import VaultLeaseError

from arccli.commands._shared import write as _write
from arccli.commands.user import _account_authority

if TYPE_CHECKING:
    from arcstore.approvals import PendingApproval

# Every way the account authority can be out of reach or refuse a mutation
# (CallbackAllowlistError is a UserStoreError; its message names the reason).
_AUTHORITY_ERRORS = (
    UserStoreError,
    AccountAuthorityError,
    VaultLeaseError,
    AuthorityConfigError,
    AnchorUnavailableError,
)


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"Error: {message}\n")
    raise SystemExit(1)


@contextmanager
def _callback_store() -> Iterator[CallbackAllowlistStore]:
    """The callback allowlist store, bound to the account authority at the deployment tier."""
    args = argparse.Namespace()
    with _account_authority(args):
        factory = getattr(args, "user_store_factory", None)
        if factory is None:
            _fail(
                "account authority is unavailable; configure [security.accounts] "
                "(see docs/runbooks/operate/openbao-accounts.md)"
            )
        try:
            yield factory.callbacks()
        except _AUTHORITY_ERRORS as exc:
            _fail(str(exc))


def _tier() -> str:
    from arccli.commands._accounts import AccountsConfigError, deployment_tier

    try:
        return deployment_tier()
    except AccountsConfigError as exc:
        _fail(str(exc))


def _operator_did() -> str:
    from arccli.commands.operator import operator_signer_and_did

    try:
        return operator_signer_and_did()[0]
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(f"operator identity is unavailable ({type(exc).__name__})")


def _clearance(name: str) -> Classification:
    try:
        return Classification[name.strip().upper()]
    except KeyError:
        _fail(f"unknown clearance {name!r}; known: {', '.join(Classification.__members__)}")


# ------------------------------------------------------------------------ add


def _approval_row(entry: AllowlistEntry) -> PendingApproval:
    from arcstore.approvals import PendingApproval

    return PendingApproval(
        id=f"cbapprove_{uuid.uuid4().hex[:12]}",
        agent_did=entry.created_by,
        agent_label=entry.origin,
        tool=CALLBACK_ALLOWLIST_TOOL,
        legs=[],
        call_hash=entry.content_hash(),
        arguments={
            "entry_id": entry.id,
            "origin": entry.origin,
            "allow_private": str(entry.allow_private).lower(),
            "clearance": entry.clearance.name,
        },
    )


async def _create_approval(row: PendingApproval) -> PendingApproval:
    from arcstore.approvals import ApprovalStore
    from arcstore.backends import open_backend

    backend = open_backend()
    await backend.start()
    try:
        return await ApprovalStore(backend).create(row)
    finally:
        await backend.stop()


def _add(args: argparse.Namespace) -> None:
    clearance = _clearance(args.clearance)
    with _callback_store() as store:
        if _tier() == "federal":
            _fail(
                "federal callback origins are added in the dashboard (Settings > Apps), "
                "signed in as an operator; a second operator approves them"
            )
        request = NewCallbackEntry(
            origin=args.origin, allow_private=args.allow_private, clearance=clearance
        )
        entry = store.request(request, _operator_did())
    _write(f"Added {entry.id} {entry.origin}")
    if entry.status == "active":
        _write("  Status: active")
        return
    row = asyncio.run(_create_approval(_approval_row(entry)))
    _write(f"  Status: pending operator approval ({row.id})")
    _write(f"  Approve it: arc approve {row.id}")


# ---------------------------------------------------------------- list/remove


def _line(entry: AllowlistEntry) -> str:
    parts = [entry.id, entry.origin, entry.status]
    if entry.allow_private:
        parts.append("allow_private")
    if entry.clearance != Classification.UNCLASSIFIED:
        parts.append(entry.clearance.name)
    return "  ".join(parts)


def _list(_args: argparse.Namespace) -> None:
    with _callback_store() as store:
        entries = store.list()
    if not entries:
        _write("No callback origins yet. Add one: arc callback add <origin>")
        return
    for entry in entries:
        _write(_line(entry))


def _remove(args: argparse.Namespace) -> None:
    with _callback_store() as store:
        removed = store.remove(args.id, _operator_did())
    _write(f"Removed {removed.id} ({removed.origin}); callbacks to it are refused from now on.")


# ------------------------------------------------------------------------ key


def _key(_args: argparse.Namespace) -> None:
    from arctrust.operator_resolver import machine_security
    from arctrust.paths import operator_root
    from arctrust.signer import SignerError
    from arctrust.work_event_signer import provision_work_event_key, work_event_signer_for

    arc_dir = operator_root()
    try:
        security = machine_security(arc_dir)
        provision_work_event_key(security, base=arc_dir)
    except (OSError, SignerError, ValueError) as exc:
        _fail(f"work-events key not provisioned ({type(exc).__name__}: {exc})")
    signer = work_event_signer_for(security, base=arc_dir)
    if signer is None:
        _fail("work-events key was minted but custody cannot serve it; see the arc log")
    _write(f"Work-events key ready  key_id={signer.key_id}  algorithm={signer.algorithm}")
    _write("  Restart arc so the callback dispatcher starts: arc restart")


# ------------------------------------------------------ approval-row actions


def activate_requested(entry_id: str, call_hash: str, approver_did: str) -> AllowlistEntry:
    """Activate the pending entry an approved ``work_event_callback`` row names.

    The store re-reads the record and refuses (audited) unless the entry is still
    pending and its authority fields hash to ``call_hash``, the hash the row binds.
    """
    with _callback_store() as store:
        return store.activate(entry_id, approver_did, call_hash)


def remove_requested(entry_id: str, actor_did: str) -> AllowlistEntry:
    """Remove the entry a denied ``work_event_callback`` row names."""
    with _callback_store() as store:
        return store.remove(entry_id, actor_did)


# --------------------------------------------------------------------- parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc callback", description="Origins work-event callbacks may be sent to."
    )
    inner = parser.add_subparsers(dest="subcmd")

    add = inner.add_parser("add", help="Allow callbacks to an origin (scheme://host[:port]).")
    add.add_argument("origin", help="e.g. https://hooks.example.com")
    add.add_argument(
        "--allow-private",
        action="store_true",
        help="Allow the origin to resolve to a private or loopback address.",
    )
    add.add_argument(
        "--clearance",
        default=Classification.UNCLASSIFIED.name,
        metavar="LEVEL",
        help="Highest classification this origin may receive (default UNCLASSIFIED).",
    )
    add.set_defaults(func=_add)

    listing = inner.add_parser("list", help="Show every callback origin.")
    listing.set_defaults(func=_list)

    remove = inner.add_parser("remove", help="Stop callbacks to an origin now.")
    remove.add_argument("id")
    remove.set_defaults(func=_remove)

    key = inner.add_parser("key", help="Mint the work-events signing key if absent.")
    key.set_defaults(func=_key)
    return parser


def callback_handler(args: list[str]) -> None:
    """Top-level handler for `arc callback <sub> [args]`."""
    parser = _build_parser()
    parsed = parser.parse_args(args) if args else None
    if parsed is None or parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)
    parsed.func(parsed)


__all__ = ["activate_requested", "callback_handler", "remove_requested"]
