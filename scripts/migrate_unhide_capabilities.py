#!/usr/bin/env python3
"""One-time migration: un-hide agent-authored capability folders.

Arc moved agent-authored capabilities from the hidden ``workspace/.capabilities``
to the visible ``workspace/capabilities`` (same untrusted/AST-gated trust
treatment — only the name changed, so you can see and edit them). The loader and
the create-tool/create-skill writers now use the visible name.

This script renames any existing ``<...>/workspace/.capabilities`` directory to
``<...>/workspace/capabilities`` so your current tools/skills are not lost when
you load the updated Arc. Run it ONCE.

Usage:
    python scripts/migrate_unhide_capabilities.py [ROOT] [--dry-run]

    ROOT       Directory to scan (default: current directory). Point it at the
               folder that contains your agent(s), e.g. the team root.
    --dry-run  Show what would change without touching the filesystem.

Safe to re-run: already-migrated agents are skipped. If a visible
``capabilities`` folder already exists alongside a hidden one, the hidden
folder's contents are merged in and the empty ``.capabilities`` is removed
(never overwriting an existing file — those are reported and left in place).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _find_hidden_dirs(root: Path) -> list[Path]:
    """Every ``.capabilities`` directory whose parent is named ``workspace``."""
    return sorted(
        p for p in root.rglob(".capabilities") if p.is_dir() and p.parent.name == "workspace"
    )


def _merge_into(src: Path, dst: Path, *, dry_run: bool) -> list[str]:
    """Move src's children into dst; return paths skipped due to collisions."""
    skipped: list[str] = []
    for child in src.iterdir():
        target = dst / child.name
        if target.exists():
            skipped.append(str(target))
            continue
        if not dry_run:
            shutil.move(str(child), str(target))
    if not dry_run and not any(src.iterdir()) and not skipped:
        src.rmdir()
    return skipped


def migrate(root: Path, *, dry_run: bool) -> int:
    """Rename/merge hidden capability dirs under root. Returns exit code."""
    hidden = _find_hidden_dirs(root)
    if not hidden:
        print(f"No 'workspace/.capabilities' folders found under {root}. Nothing to do.")
        return 0

    had_collision = False
    for src in hidden:
        dst = src.with_name("capabilities")
        if not dst.exists():
            print(f"{'[dry-run] ' if dry_run else ''}rename: {src} -> {dst}")
            if not dry_run:
                src.rename(dst)
            continue
        print(f"{'[dry-run] ' if dry_run else ''}merge:  {src} -> {dst} (target exists)")
        skipped = _merge_into(src, dst, dry_run=dry_run)
        for s in skipped:
            had_collision = True
            print(f"  ! kept existing, did NOT overwrite: {s}")

    if had_collision:
        print(
            "\nSome files existed in both locations and were left untouched — "
            "review the '!' lines above and reconcile by hand."
        )
        return 1
    print("\nDone." if not dry_run else "\nDry run complete — no changes made.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="Directory to scan (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing files")
    args = parser.parse_args()
    return migrate(Path(args.root).expanduser().resolve(), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
