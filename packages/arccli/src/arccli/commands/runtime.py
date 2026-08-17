"""``arc runtime`` — install-level version control for the framework itself.

Runtimes install side by side under ``~/.arc/runtime/<version>/`` — code, venv
and modules together — behind a ``current`` symlink. Updating is pointing the
symlink at the new version; rolling back is pointing it at the old one. Both are
a single ``os.replace`` of the link, so a reader always sees one whole runtime.

This command is the operator's only supported way to move that link. Doing it
with ``ln -sfn`` is not equivalent: it unlinks before it relinks, so a process
starting in that window finds no runtime at all.

The flip itself lives in :func:`arctrust.paths.activate_runtime` — including the
refusal to accept a version string that is anything other than a bare directory
name, which is what stops ``current`` being aimed at ``state/``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from arctrust.paths import (
    PRE_SYMLINK_PREFIX,
    activate_runtime,
    arc_runtime,
    arc_runtime_root,
)

from arccli.commands._shared import dispatch
from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out


def _is_version(entry: Path) -> bool:
    """True when *entry* is an installed runtime an operator may activate.

    Excludes the ``current`` link, a plain ``current/`` directory left by a
    pre-symlink install, and the copy ``activate_runtime`` rescues from one.
    Offering any of those would invite activating a tree with no venv in it.
    """
    return (
        entry.is_dir()
        and not entry.is_symlink()
        and entry.name != arc_runtime().name
        and not entry.name.startswith(PRE_SYMLINK_PREFIX)
    )


def _installed_versions() -> list[Path]:
    """Every installed runtime, by name."""
    root = arc_runtime_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if _is_version(p))


def _active_version() -> str | None:
    """The directory name ``current`` resolves to, or ``None`` when unset."""
    current = arc_runtime()
    if not current.is_symlink():
        return None
    return current.resolve().name


def _list(_: argparse.Namespace) -> None:
    """Print every installed runtime and mark the live one."""
    versions = _installed_versions()
    if not versions:
        _out(f"No runtime installed under {arc_runtime_root()}.")
        return
    active = _active_version()
    _print_table(
        ["Version", "Status", "Path"],
        [[p.name, "ACTIVE" if p.name == active else "", str(p)] for p in versions],
    )
    if active is None:
        _out("\nNo version is active — 'current' is not a symlink.")
        _out("Point it at one with: arc runtime activate <version>")


def _activate(args: argparse.Namespace) -> None:
    """Point ``current`` at one installed version, atomically."""
    try:
        link = activate_runtime(args.version)
    except (ValueError, FileNotFoundError) as exc:
        _err(f"arc runtime activate: {exc}")
        raise SystemExit(1) from exc
    _out(f"{link} -> {link.resolve()}")
    _out(f"active runtime: {args.version}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc runtime",
        description="Installed framework versions and the active one.",
    )
    subs = parser.add_subparsers(dest="subcmd")
    subs.add_parser("list", help="Every installed runtime version, with the active one marked")
    activate = subs.add_parser("activate", help="Point 'current' at a version (update/rollback)")
    activate.add_argument("version", help="An installed runtime version — a bare directory name")
    return parser


def runtime_handler(args: list[str]) -> None:
    """Top-level handler for ``arc runtime [subcommand]`` (registry dispatch)."""
    dispatch(_build_parser(), {"list": _list, "activate": _activate}, args)
