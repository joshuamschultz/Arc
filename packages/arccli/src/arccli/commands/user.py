"""`arc user` — the people who can sign in to this deployment.

Every install needs this, not just the hosted one: a local operator running
`arc ui start` should be able to create an account instead of copying a bearer
token out of a terminal. The first account created is an operator, because a
deployment whose only account can look but not approve is a deployment that
cannot do anything.

Passwords are never taken as an argument — they would land in shell history and
in the process table. Interactive prompt, or `--password-stdin` for automation.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from arccli.formatting import print_table


def _store(args: argparse.Namespace):  # type: ignore[no-untyped-def]  # reason: UserStore is a lazy import
    from arctrust.users import UserStore, default_users_path

    path = getattr(args, "file", None)
    return UserStore(Path(path).expanduser() if path else default_users_path())


def _read_password(args: argparse.Namespace, *, confirm: bool = True) -> str:
    if getattr(args, "password_stdin", False):
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            _fail("no password on stdin")
        return password

    if not sys.stdin.isatty():
        _fail("no terminal to prompt on — use --password-stdin")

    password = getpass.getpass("Password: ")
    if confirm and password != getpass.getpass("Repeat password: "):
        _fail("passwords do not match")
    return password


def _parse_pairs(specs: list[str] | None) -> dict[str, str]:
    """``["telegram:4242"]`` -> ``{"telegram": "4242"}``.

    The CLI passes platform names straight through. Neither it nor the store
    knows what any of them mean; the gateway package that owns a surface does.
    """
    pairs: dict[str, str] = {}
    for spec in specs or []:
        platform, _, external_id = spec.partition(":")
        if not (platform and external_id):
            _fail(f"--pair expects PLATFORM:ID, got {spec!r}")
        pairs[platform.strip().lower()] = external_id.strip()
    return pairs


def _format_pairs(pairings: dict[str, str]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in sorted(pairings.items())) or "—"


def _fail(message: str) -> None:
    sys.stderr.write(f"Error: {message}\n")
    raise SystemExit(1)


def _add(args: argparse.Namespace) -> None:
    from arctrust.users import OPERATOR, VIEWER

    store = _store(args)
    # The first account has to be able to approve things, or the deployment is
    # inert until someone hand-edits the store.
    first = store.is_empty()
    roles: tuple[str, ...]
    if getattr(args, "operator", False) or first:
        roles = (OPERATOR,)
    else:
        roles = (VIEWER,)

    try:
        user = store.add(
            args.email,
            _read_password(args),
            handle=getattr(args, "handle", None),
            display_name=getattr(args, "name", "") or "",
            roles=roles,
            pairings=_parse_pairs(getattr(args, "pair", None)),
        )
    except ValueError as exc:
        _fail(str(exc))
        return

    sys.stdout.write(f"Created {user.email} ({', '.join(user.roles)})\n")
    sys.stdout.write(f"  Called: {user.called}\n")
    sys.stdout.write(f"  Mention: @{user.handle}\n")
    sys.stdout.write(f"  DID: {user.did}\n")
    if first:
        sys.stdout.write("  First account on this deployment, so it is an operator.\n")
    if not user.pairings:
        # Without a paired surface there is nowhere to send a password reset:
        # a deployment has no mail server of its own.
        sys.stdout.write(
            "  No chat account paired — password reset will not be possible.\n"
            f"  Add one later: arc user set {user.email} --pair <platform>:<id>\n"
        )


def _list(args: argparse.Namespace) -> None:
    users = _store(args).list()
    if not users:
        sys.stdout.write("No users yet. Create one: arc user add <email>\n")
        return
    print_table(
        ["EMAIL", "MENTION", "NAME", "ROLES", "PAIRED", "STATUS"],
        [
            [
                u.email,
                f"@{u.handle}" if u.handle else "—",
                u.display_name or "—",
                ", ".join(u.roles),
                _format_pairs(u.pairings),
                "disabled" if u.disabled else "active",
            ]
            for u in users
        ],
    )


def _passwd(args: argparse.Namespace) -> None:
    store = _store(args)
    try:
        store.set_password(args.email, _read_password(args))
    except ValueError as exc:
        _fail(str(exc))
        return
    sys.stdout.write(f"Password changed for {args.email}\n")


def _role(args: argparse.Namespace) -> None:
    store = _store(args)
    try:
        user = store.set_roles(args.email, tuple(args.roles))
    except ValueError as exc:
        _fail(str(exc))
        return
    sys.stdout.write(f"{user.email} is now {', '.join(user.roles)}\n")


def _set(args: argparse.Namespace) -> None:
    """One place to change everything about an account except its password."""
    store = _store(args)
    changed: list[str] = []
    try:
        if args.handle is not None:
            user = store.set_handle(args.email, args.handle)
            changed.append(f"mention @{user.handle}")
        if args.name is not None:
            user = store.set_display_name(args.email, args.name)
            changed.append(f"called {user.called!r}")
        for spec in args.pair or []:
            platform, _, external_id = spec.partition(":")
            user = store.set_pairing(args.email, platform, external_id or None)
            changed.append(f"{platform} {external_id}" if external_id else f"{platform} unpaired")
    except ValueError as exc:
        _fail(str(exc))
        return

    if not changed:
        _fail("nothing to change — pass --handle, --name, or --pair")
    sys.stdout.write(f"{_normalize(args.email)}: {', '.join(changed)}\n")


def _show(args: argparse.Namespace) -> None:
    user = _store(args).get(args.email)
    if user is None:
        _fail(f"no such user: {args.email}")
        return
    for key, value in user.redacted().items():
        sys.stdout.write(f"{key:>16}: {value}\n")


def _normalize(email: str) -> str:
    return email.strip().lower()


def _disable(args: argparse.Namespace) -> None:
    store = _store(args)
    try:
        user = store.set_disabled(args.email, not args.enable)
    except ValueError as exc:
        _fail(str(exc))
        return
    sys.stdout.write(f"{user.email} is now {'active' if args.enable else 'disabled'}\n")


def _remove(args: argparse.Namespace) -> None:
    store = _store(args)
    try:
        store.remove(args.email)
    except ValueError as exc:
        _fail(str(exc))
        return
    sys.stdout.write(f"Removed {args.email}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc user", description="Accounts that can sign in to this deployment."
    )
    parser.add_argument(
        "--file", default=None, help="User store path (default: ~/.arc/users.json)"
    )
    inner = parser.add_subparsers(dest="subcmd")

    add = inner.add_parser("add", help="Create an account.")
    add.add_argument("email")
    add.add_argument("--operator", action="store_true", help="Grant approval rights.")
    add.add_argument(
        "--handle", default=None, help="Mention name, e.g. josh (derived from email if omitted)."
    )
    add.add_argument("--name", default=None, help="Display name, e.g. 'Josh Schultz'.")
    add.add_argument(
        "--pair",
        action="append",
        default=None,
        metavar="PLATFORM:ID",
        help="Link a chat account, e.g. telegram:4242. Repeatable.",
    )
    add.add_argument("--password-stdin", dest="password_stdin", action="store_true")
    add.set_defaults(func=_add)

    listing = inner.add_parser("list", help="Show every account.")
    listing.set_defaults(func=_list)

    passwd = inner.add_parser("passwd", help="Change an account's password.")
    passwd.add_argument("email")
    passwd.add_argument("--password-stdin", dest="password_stdin", action="store_true")
    passwd.set_defaults(func=_passwd)

    role = inner.add_parser("role", help="Set an account's roles.")
    role.add_argument("email")
    role.add_argument("roles", nargs="+", choices=["viewer", "operator"])
    role.set_defaults(func=_role)

    setter = inner.add_parser("set", help="Change an account's name, mention, or chat links.")
    setter.add_argument("email")
    setter.add_argument("--handle", default=None, help="Mention name agents use, e.g. josh.")
    setter.add_argument("--name", default=None, help="Display name, e.g. 'Josh Schultz'.")
    setter.add_argument(
        "--pair",
        action="append",
        default=None,
        metavar="PLATFORM:ID",
        help="Link a chat account, e.g. telegram:4242. Omit the id to unpair. Repeatable.",
    )
    setter.set_defaults(func=_set)

    show = inner.add_parser("show", help="Everything about one account.")
    show.add_argument("email")
    show.set_defaults(func=_show)

    disable = inner.add_parser("disable", help="Block an account from signing in.")
    disable.add_argument("email")
    disable.set_defaults(func=_disable, enable=False)

    enable = inner.add_parser("enable", help="Let a blocked account sign in again.")
    enable.add_argument("email")
    enable.set_defaults(func=_disable, enable=True)

    remove = inner.add_parser("remove", help="Delete an account.")
    remove.add_argument("email")
    remove.set_defaults(func=_remove)

    return parser


def user_handler(args: list[str]) -> None:
    """Top-level handler for `arc user <sub> [args]`."""
    parser = _build_parser()
    if not args:
        parser.print_help()
        sys.exit(0)
    parsed = parser.parse_args(args)
    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)
    parsed.func(parsed)


__all__ = ["user_handler"]
