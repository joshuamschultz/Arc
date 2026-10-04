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
from arctrust.paths import (
    activate_runtime,
    active_runtime_version,
    arc_runtime_root,
    installed_runtime_versions,
)

from arccli.commands._shared import dispatch
from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out


def _list(_: argparse.Namespace) -> None:
    """Print every installed runtime and mark the live one."""
    versions = installed_runtime_versions()
    if not versions:
        _out(f"No runtime installed under {arc_runtime_root()}.")
        return
    active = active_runtime_version()
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
