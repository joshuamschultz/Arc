"""`arc principal` — app principals: non-human callers of the ArcUI API (SPEC-087).

``issue``   — register an app by its own Ed25519 public key, with scopes bound to
              named agents and the people it may act for. At personal tier the
              principal is active at once; at enterprise and federal it is pending
              until an operator runs ``arc approve <id>``.
``list``    — every principal and its grants.
``show``    — one principal.
``revoke``  — stop a principal; its bearer is refused on the next request.

The bearer secret is printed once, to stdout, at issue time. Only its digest is
stored; nothing here logs it. Every store mutation is audited by the account
authority itself (``principal.<verb>`` on the operator chain).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from arctrust.authority import AccountAuthorityError
from arctrust.authority_config import AuthorityConfigError
from arctrust.canonical import canonical_json
from arctrust.monotonic import AnchorUnavailableError
from arctrust.principal_auth import format_bearer, issue_secret
from arctrust.principals import (
    WILDCARD,
    Principal,
    PrincipalStore,
    PrincipalValidationError,
    Scope,
    Tier,
    derive_app_did,
    parse_scope,
    validate_for_tier,
)
from arctrust.users import UserStoreError
from arctrust.vault_lease import VaultLeaseError

from arccli.commands.user import _account_authority

if TYPE_CHECKING:
    from arcstore.approvals import PendingApproval

#: ``PendingApproval.tool`` of a principal issue request; ``arc approve`` and the
#: arcui approvals route activate the principal when they approve such a row.
PRINCIPAL_ISSUE_TOOL = "principal_issue"

_TIERS: dict[str, Tier] = {
    "personal": "personal",
    "enterprise": "enterprise",
    "federal": "federal",
}
_EXPIRES_RE = re.compile(r"^([1-9][0-9]{0,3})d$")
_ED25519_KEY_BYTES = 32
_MAX_KEY_FILE = 4096

# Every way the account authority can be out of reach or refuse a mutation
# (PrincipalStoreError is a UserStoreError).
_AUTHORITY_ERRORS = (
    UserStoreError,
    AccountAuthorityError,
    VaultLeaseError,
    AuthorityConfigError,
    AnchorUnavailableError,
)

_TIER_REFUSALS = {
    "missing_expiry": "--expires is required at {tier} tier (e.g. --expires 30d)",
    "expiry_too_long": "expiry is beyond the {tier} tier limit (enterprise 90d, federal 30d)",
    "wildcard_agent": "a wildcard (*) agent binding is not allowed at {tier} tier",
    "wildcard_subject": "a wildcard (*) subject is not allowed at {tier} tier",
}


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"Error: {message}\n")
    raise SystemExit(1)


def _write(line: str = "") -> None:
    sys.stdout.write(line + "\n")


@contextmanager
def _principal_store() -> Iterator[tuple[PrincipalStore, Any]]:
    """The principal store and user-store factory, bound to the account authority."""
    args = argparse.Namespace()
    with _account_authority(args):
        factory = getattr(args, "user_store_factory", None)
        if factory is None:
            _fail(
                "account authority is unavailable; configure [security.accounts] "
                "(see docs/runbooks/operate/openbao-accounts.md)"
            )
        try:
            yield factory.principals(), factory
        except _AUTHORITY_ERRORS as exc:
            _fail(str(exc))


def _deployment() -> tuple[Tier, str]:
    """The deployment tier and the tenant org app DIDs are minted under."""
    from arccli.commands._accounts import accounts_config

    cfg = accounts_config()
    if cfg is None:
        _fail("account authority is unavailable; configure [security.accounts]")
    return deployment_tier(), str(cfg.tenant_id)


def _operator_did() -> str:
    from arccli.commands.operator import operator_signer_and_did

    try:
        return operator_signer_and_did()[0]
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(f"operator identity is unavailable ({type(exc).__name__})")


# --------------------------------------------------------------------- parsing


def _read_public_key(path: str) -> str:
    """The app's Ed25519 verify key from a file holding its hex encoding."""
    try:
        raw = Path(path).expanduser().read_text(encoding="ascii")[:_MAX_KEY_FILE].strip()
        key = bytes.fromhex(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        _fail(f"{path} does not hold a hex-encoded Ed25519 public key")
    if len(key) != _ED25519_KEY_BYTES:
        _fail(f"{path} does not hold a hex-encoded Ed25519 public key (32 bytes)")
    return key.hex()


def _parse_scopes(specs: list[str]) -> dict[Scope, tuple[str, ...]]:
    """``["runs:read=did:a,did:b"]`` -> ``{Scope.RUNS_READ: ("did:a", "did:b")}``."""
    grants: dict[Scope, set[str]] = {}
    for spec in specs:
        name, _, agents_text = spec.partition("=")
        try:
            scope = parse_scope(name.strip())
        except PrincipalValidationError:
            _fail(f"unknown scope {name.strip()!r}; known: {', '.join(s.value for s in Scope)}")
        agents = {agent.strip() for agent in agents_text.split(",") if agent.strip()}
        if not agents:
            _fail(f"--scope {spec!r} names no agent; use SCOPE=AGENT_DID")
        grants.setdefault(scope, set()).update(agents)
    return {scope: tuple(sorted(agents)) for scope, agents in grants.items()}


def _resolve_subjects(specs: list[str], factory: Any) -> tuple[str, ...]:
    """Map each ``--subject`` (email or user DID) to a known user's DID."""
    if not specs:
        return ()
    users = factory().list()
    by_email = {user.email: user.did for user in users}
    known_dids = {user.did for user in users}
    subjects: set[str] = set()
    for spec in specs:
        value = spec.strip()
        if value == WILDCARD or value in known_dids:
            subjects.add(value)
        elif value.lower() in by_email:
            subjects.add(by_email[value.lower()])
        else:
            _fail(f"no such user: {value}")
    return tuple(sorted(subjects))


def _parse_expiry(spec: str | None, now: datetime) -> datetime | None:
    if spec is None:
        return None
    match = _EXPIRES_RE.match(spec.strip())
    if match is None:
        _fail(f"--expires takes a number of days such as 30d, got {spec!r}")
    return now + timedelta(days=int(match.group(1)))


def _check_tier(principal: Principal, tier: Tier, now: datetime) -> None:
    try:
        validate_for_tier(principal, tier, now=now)
    except PrincipalValidationError as exc:
        template = _TIER_REFUSALS.get(exc.reason, "principal refused: {reason}")
        _fail(template.format(tier=tier, reason=exc.reason))


# ---------------------------------------------------------------------- issue


def _new_principal(args: argparse.Namespace, factory: Any, org: str) -> tuple[Principal, str]:
    """Build the pending principal and its one-time bearer (not yet stored)."""
    now = datetime.now(UTC)
    public_key = _read_public_key(args.public_key_file)
    secret, digest = issue_secret()
    principal = Principal(
        id=uuid.uuid4().hex,
        name=args.name,
        did=derive_app_did(bytes.fromhex(public_key), org=org),
        public_key=public_key,
        secret_digest=digest,
        scopes=_parse_scopes(args.scope),
        subjects=_resolve_subjects(args.subject or [], factory),
        status="pending",
        created_by=_operator_did(),
        approved_by=None,
        created_at=now,
        expires_at=_parse_expiry(args.expires, now),
        last_used_at=None,
        revoked_by=None,
        revoked_at=None,
    )
    return principal, format_bearer(principal.id, secret)


def _approval_row(principal: Principal) -> PendingApproval:
    from arcstore.approvals import PendingApproval

    scopes = {str(scope): sorted(agents) for scope, agents in principal.scopes.items()}
    return PendingApproval(
        id=f"prinissue_{uuid.uuid4().hex[:12]}",
        agent_did=principal.created_by,
        agent_label=principal.name,
        tool=PRINCIPAL_ISSUE_TOOL,
        legs=[],
        call_hash=principal.content_hash(),
        arguments={
            "principal_id": principal.id,
            "name": principal.name,
            "scopes": canonical_json(scopes).decode("utf-8"),
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


def _issue(args: argparse.Namespace) -> None:
    with _principal_store() as (store, factory):
        tier, org = _deployment()
        principal, bearer = _new_principal(args, factory, org)
        _check_tier(principal, tier, principal.created_at)
        store.request(principal)
        _write(f"Issued {principal.name}")
        _write(f"  ID:  {principal.id}")
        _write(f"  DID: {principal.did}")
        if tier == "personal":
            store.activate(principal.id, principal.created_by, principal.content_hash())
            _write("  Status: active")
        else:
            row = asyncio.run(_create_approval(_approval_row(principal)))
            _write(f"  Status: pending operator approval ({row.id})")
            _write(f"  Approve it: arc approve {row.id}")
        _write("")
        _write("Bearer token (shown once, store it now; Arc keeps only its digest):")
        _write(f"  {bearer}")


# ------------------------------------------------------------ list/show/revoke


def _when(value: datetime | None, missing: str) -> str:
    return value.isoformat(timespec="seconds") if value is not None else missing


def _describe(principal: Principal) -> None:
    """Every operator-visible field of one principal. Never the secret or its digest."""
    _write(f"{'name':>10}: {principal.name}")
    _write(f"{'id':>10}: {principal.id}")
    _write(f"{'did':>10}: {principal.did}")
    _write(f"{'status':>10}: {principal.status}")
    _write(f"{'scope':>10}  agents")
    for scope, agents in sorted(principal.scopes.items()):
        _write(f"{scope.value:>10}: {', '.join(agents)}")
    _write(f"{'subjects':>10}: {', '.join(principal.subjects) or '—'}")
    _write(f"{'issuer':>10}: {principal.created_by}")
    _write(f"{'approver':>10}: {principal.approved_by or '—'}")
    _write(f"{'issued':>10}: {_when(principal.created_at, '—')}")
    _write(f"{'expires':>10}: {_when(principal.expires_at, 'never')}")
    _write(f"{'last use':>10}: {_when(principal.last_used_at, 'never')}")
    if principal.revoked_by is not None:
        _write(f"{'revoked':>10}: {_when(principal.revoked_at, '—')} by {principal.revoked_by}")


def _list(_args: argparse.Namespace) -> None:
    with _principal_store() as (store, _):
        principals = store.list()
    if not principals:
        _write("No app principals yet. Issue one: arc principal issue --help")
        return
    for index, principal in enumerate(principals):
        if index:
            _write("")
        _describe(principal)


def _show(args: argparse.Namespace) -> None:
    with _principal_store() as (store, _):
        principal = store.get(args.id)
    if principal is None:
        _fail(f"no such principal: {args.id}")
    _describe(principal)


def _revoke(args: argparse.Namespace) -> None:
    with _principal_store() as (store, _):
        if store.get(args.id) is None:
            _fail(f"no such principal: {args.id}")
        revoked = store.revoke(args.id, _operator_did())
    _write(f"Revoked {revoked.id} ({revoked.name}); its bearer is refused from now on.")


# ------------------------------------------------------ approval-row actions


def activate_requested(principal_id: str, call_hash: str, approver_did: str) -> Principal:
    """Activate the pending principal an approved ``principal_issue`` row names.

    The store re-reads the record and refuses (audited) unless it is still pending
    and its authority fields hash to ``call_hash``, the hash the row was bound to.
    """
    with _principal_store() as (store, _):
        return store.activate(principal_id, approver_did, call_hash)


def revoke_requested(principal_id: str, actor_did: str) -> Principal:
    """Revoke the principal a denied ``principal_issue`` row names."""
    with _principal_store() as (store, _):
        return store.revoke(principal_id, actor_did)


def deployment_tier() -> Tier:
    """The deployment tier ``arc approve`` applies the two-person rule against."""
    from arccli.commands.operator import _machine_security

    tier = _TIERS.get(str(_machine_security().tier))
    if tier is None:
        _fail("deployment tier is not personal, enterprise or federal; fix [security].tier")
    return tier


# --------------------------------------------------------------------- parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc principal", description="App principals that call the ArcUI API."
    )
    inner = parser.add_subparsers(dest="subcmd")

    issue = inner.add_parser("issue", help="Register an app by its public key.")
    issue.add_argument("--name", required=True, help="Display name, e.g. billing-bot.")
    issue.add_argument(
        "--public-key-file", required=True, help="File holding the app's hex Ed25519 public key."
    )
    issue.add_argument(
        "--scope",
        action="append",
        required=True,
        metavar="SCOPE=AGENT_DID[,AGENT_DID]",
        help="Grant a scope on named agents, e.g. runs:read=<agent did>. Repeatable.",
    )
    issue.add_argument(
        "--subject",
        action="append",
        default=None,
        metavar="EMAIL|DID",
        help="A person the app may act for. Repeatable.",
    )
    issue.add_argument("--expires", default=None, metavar="Nd", help="Lifetime in days, e.g. 30d.")
    issue.set_defaults(func=_issue)

    listing = inner.add_parser("list", help="Show every app principal.")
    listing.set_defaults(func=_list)

    show = inner.add_parser("show", help="Everything about one app principal.")
    show.add_argument("id")
    show.set_defaults(func=_show)

    revoke = inner.add_parser("revoke", help="Stop an app principal now.")
    revoke.add_argument("id")
    revoke.set_defaults(func=_revoke)
    return parser


def principal_handler(args: list[str]) -> None:
    """Top-level handler for `arc principal <sub> [args]`."""
    parser = _build_parser()
    parsed = parser.parse_args(args) if args else None
    if parsed is None or parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)
    parsed.func(parsed)


__all__ = [
    "PRINCIPAL_ISSUE_TOOL",
    "activate_requested",
    "deployment_tier",
    "principal_handler",
    "revoke_requested",
]
