"""Validate a candidate skill folder.

Usage:
    python validate.py <path-to-skill-folder>

Runs frontmatter + 7-section + filler checks. Exits 0 on no errors,
1 if errors are present, 2 on usage error. Warnings (filler, missing
tool dependencies) print but do not fail the run — those are
deployment-tier decisions.
"""
# ruff: noqa: T201 — CLI tool; print is the right primitive here.

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate.py <skill-folder>")
        return 2
    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"not a folder: {folder}")
        return 2
    from arcagent.core.skill_validator import validate_skill_folder

    result = validate_skill_folder(folder, scan_root="workspace")
    for err in result.errors:
        print(f"ERROR [{err.code}] {err.detail}")
    for warn in result.warnings:
        print(f"WARN  [{warn.code}] {warn.detail}")
    if result.errors:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
