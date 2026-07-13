"""``arc memory`` — operator maintenance over an agent's glass-box memory files.

Currently one subcommand:

    arc memory dedup [--apply] <workspace> [<workspace> ...]

``dedup`` merges memory cards whose slugs diverged before slug canonicalization
landed (one real thing became several files: "Custom ERP.md", "custom-erp.md"). The
merge algorithm itself lives in :mod:`arcmemory.hygiene` — all memory logic stays in
arcmemory so a deployment can swap the whole package out. This module is a THIN CLI:
it discovers workspaces, calls :func:`arcmemory.hygiene.dedup_workspace`, and renders
the report. Dry-run by default; ``--apply`` writes. After applying, restart each
agent (recovery rebuilds its index) so stale surface rows drop.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from arcmemory.hygiene import DedupReport, dedup_workspace, discover_workspaces

from arccli.commands._shared import dispatch, err
from arccli.commands._shared import write as _out


def _dedup(args: argparse.Namespace) -> None:
    """Merge duplicate memory cards into their canonical-slug files.

    Dry-run by default (reports what would merge, writes nothing). ``--apply``
    performs the merge and deletes the variant files. Idempotent: a second run
    finds nothing to merge.
    """
    apply: bool = args.apply
    mode = "APPLY" if apply else "dry-run"
    total_groups = 0
    total_deleted = 0
    n_workspaces = 0

    for raw in args.workspaces:
        root = Path(raw).expanduser()
        workspaces = discover_workspaces(root)
        if not workspaces:
            err(f"  no memory workspace found under {root}")
            continue
        for workspace in workspaces:
            n_workspaces += 1
            report = dedup_workspace(workspace, apply=apply)
            _render_workspace(report, mode)
            total_groups += report.groups
            total_deleted += sum(s.files_deleted for s in report.stores)

    verb = "merged" if apply else "to merge"
    _out(
        f"\n{total_groups} duplicate group(s) {verb}, "
        f"{total_deleted} file(s) {'deleted' if apply else 'to delete'} "
        f"across {n_workspaces} workspace(s)."
    )
    if apply and total_groups:
        _out("Restart each affected agent so its index rebuilds and stale surface rows drop.")


def _render_workspace(report: DedupReport, mode: str) -> None:
    """Print one workspace's per-store merge plan/outcome."""
    _out(f"\n{report.workspace}  ({mode})")
    if report.groups == 0:
        _out("  no duplicates.")
        return
    for store_report in report.stores:
        if not store_report.merges:
            continue
        _out(
            f"  {store_report.store}: {len(store_report.merges)} group(s), "
            f"{store_report.files_deleted} file(s) to delete"
        )
        for merge in store_report.merges:
            _out(f"    merge {merge.sources} -> {merge.canonical}.md")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc memory",
        description="Operator maintenance over an agent's glass-box memory files.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    dedup_p = subs.add_parser(
        "dedup",
        help="Merge pre-canonicalization duplicate memory cards into their canonical file.",
    )
    dedup_p.add_argument(
        "workspaces",
        nargs="+",
        metavar="<workspace>",
        help="Dir containing memory/ (or a root to search for nested workspaces).",
    )
    dedup_p.add_argument(
        "--apply",
        action="store_true",
        help="Perform the merge and delete variants (default: dry-run).",
    )
    return parser


_SUBCOMMANDS = {"dedup": _dedup}


def memory_handler(args: list[str]) -> None:
    """Entry point for ``arc memory <subcommand>`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMANDS, args)


__all__ = ["memory_handler"]
