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
            roles=roles,
            telegram_user_id=getattr(args, "telegram", None),
        )
    except ValueError as exc:
        _fail(str(exc))
        return

    sys.stdout.write(f"Created {user.email} ({', '.join(user.roles)})\n")
    sys.stdout.write(f"  DID: {user.did}\n")
    if first:
        sys.stdout.write("  First account on this deployment, so it is an operator.\n")
    if not user.telegram_user_id:
        # Without a paired channel there is no way to reset this password, since
        # a deployment has no mail server.
        sys.stdout.write(
            "  No Telegram paired — password reset will not be possible.\n"
            f"  Add one later: arc user telegram {user.email} <id from @userinfobot>\n"
        )


def _list(args: argparse.Namespace) -> None:
    users = _store(args).list()
    if not users:
        sys.stdout.write("No users yet. Create one: arc user add <email>\n")
        return
    print_table(
        ["EMAIL", "ROLES", "TELEGRAM", "STATUS"],
        [
            [
                u.email,
                ", ".join(u.roles),
                u.telegram_user_id or "—",
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


def _telegram(args: argparse.Namespace) -> None:
    store = _store(args)
    value = None if args.telegram_user_id in ("", "none") else args.telegram_user_id
    try:
        user = store.set_telegram(args.email, value)
    except ValueError as exc:
        _fail(str(exc))
        return
    sys.stdout.write(
        f"{user.email} paired to Telegram {user.telegram_user_id}\n"
        if value
        else f"{user.email} unpaired from Telegram (password reset now impossible)\n"
    )


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
        "--telegram", default=None, help="Telegram user id, for password reset."
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

    telegram = inner.add_parser("telegram", help="Pair or unpair a Telegram account.")
    telegram.add_argument("email")
    telegram.add_argument("telegram_user_id", help="Numeric id, or 'none' to unpair.")
    telegram.set_defaults(func=_telegram)

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
