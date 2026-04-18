#!/usr/bin/env python3
"""LOC budget checker for M1 Acceptance Gate G1.5 and G1.6.

Counts non-blank, non-comment lines of code (NCLOC) for two budgets:

    Budget A (G1.5): arcagent core ≤ 3,500 NCLOC total
        Directory: packages/arcagent/src/arcagent/core/

    Budget B (G1.6): arcgateway core files ≤ 1,200 NCLOC total
        Files:
            packages/arcgateway/src/arcgateway/runner.py
            packages/arcgateway/src/arcgateway/session.py
            packages/arcgateway/src/arcgateway/executor.py
            packages/arcgateway/src/arcgateway/adapters/base.py

NCLOC definition:
    A line counts if it is:
    - Not blank (has at least one non-whitespace character), AND
    - Not a pure comment line (first non-whitespace character is not '#')

    Inline comments (``x = 1  # explanation``) still count — the code
    statement is real code. Only standalone comment lines are excluded.
    Docstrings count as code (they are string literals, not comments).

Exit codes:
    0 — all budgets satisfied
    1 — one or more budgets exceeded (see printed table for details)

Usage::

    python scripts/check_loc_budgets.py
    python scripts/check_loc_budgets.py --root /path/to/arc

Run from any directory; the --root flag overrides auto-detection.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Budget definitions
# ---------------------------------------------------------------------------

_ARCAGENT_CORE_BUDGET = 3_500
_ARCGATEWAY_CORE_BUDGET = 1_200

# arcgateway core files for Budget B (G1.6)
_ARCGATEWAY_CORE_FILES = [
    "runner.py",
    "session.py",
    "executor.py",
    "adapters/base.py",
]


# ---------------------------------------------------------------------------
# NCLOC counter
# ---------------------------------------------------------------------------


def count_ncloc(path: Path) -> int:
    """Count non-blank, non-comment lines in a Python source file.

    Args:
        path: Path to a Python source file.

    Returns:
        Integer count of qualifying lines, or 0 if the file cannot be read.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0

    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Table printer
# ---------------------------------------------------------------------------

_COL_FILE = 50
_COL_LOC = 8


def _row(label: str, count: int, budget: int | None = None) -> str:
    """Format one table row.

    Args:
        label: File or directory label.
        count: NCLOC count.
        budget: Optional budget cap — adds OK/OVER marker.

    Returns:
        Formatted string row.
    """
    label_cell = label.ljust(_COL_FILE)[:_COL_FILE]
    count_cell = str(count).rjust(_COL_LOC)
    if budget is not None:
        status = "OK" if count <= budget else f"OVER by {count - budget}"
        return f"  {label_cell}  {count_cell}  ({status})"
    return f"  {label_cell}  {count_cell}"


def _separator() -> str:
    return "  " + "-" * (_COL_FILE + _COL_LOC + 4)


# ---------------------------------------------------------------------------
# Budget A — arcagent core
# ---------------------------------------------------------------------------


def check_arcagent_core(root: Path) -> tuple[int, list[tuple[str, int]], bool]:
    """Count NCLOC in arcagent/core/ and check against budget.

    Args:
        root: Repository root directory.

    Returns:
        Tuple of (total_count, per_file_rows, passed).
        per_file_rows is a list of (relative_label, count) pairs.
    """
    core_dir = root / "packages" / "arcagent" / "src" / "arcagent" / "core"

    if not core_dir.exists():
        print(f"WARNING: arcagent core directory not found: {core_dir}", file=sys.stderr)
        return 0, [], True  # Cannot check — skip; don't false-fail

    rows: list[tuple[str, int]] = []
    total = 0

    for py_file in sorted(core_dir.glob("*.py")):
        count = count_ncloc(py_file)
        total += count
        rows.append((f"arcagent/core/{py_file.name}", count))

    passed = total <= _ARCAGENT_CORE_BUDGET
    return total, rows, passed


# ---------------------------------------------------------------------------
# Budget B — arcgateway core files
# ---------------------------------------------------------------------------


def check_arcgateway_core(root: Path) -> tuple[int, list[tuple[str, int]], bool]:
    """Count NCLOC in arcgateway core files and check against budget.

    Args:
        root: Repository root directory.

    Returns:
        Tuple of (total_count, per_file_rows, passed).
    """
    gw_base = root / "packages" / "arcgateway" / "src" / "arcgateway"

    if not gw_base.exists():
        print(
            f"WARNING: arcgateway source not found: {gw_base}", file=sys.stderr
        )
        return 0, [], True

    rows: list[tuple[str, int]] = []
    total = 0

    for rel_path in _ARCGATEWAY_CORE_FILES:
        full_path = gw_base / rel_path
        if not full_path.exists():
            print(
                f"WARNING: arcgateway core file not found: {full_path}",
                file=sys.stderr,
            )
            continue
        count = count_ncloc(full_path)
        total += count
        rows.append((f"arcgateway/{rel_path}", count))

    passed = total <= _ARCGATEWAY_CORE_BUDGET
    return total, rows, passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run both LOC budget checks and print results table.

    Returns:
        Exit code: 0 if all budgets pass, 1 if any budget is exceeded.
    """
    parser = argparse.ArgumentParser(
        description="Check LOC budgets for M1 acceptance gates G1.5 and G1.6."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root directory. Auto-detected if not provided.",
    )
    args = parser.parse_args()

    # Auto-detect root: this script lives at <root>/scripts/check_loc_budgets.py
    if args.root is not None:
        root = args.root.resolve()
    else:
        root = Path(__file__).parent.parent.resolve()

    if not root.exists():
        print(f"ERROR: Repository root not found: {root}", file=sys.stderr)
        return 1

    all_passed = True

    # ------------------------------------------------------------------
    # Budget A — arcagent core ≤ 3,500
    # ------------------------------------------------------------------
    print()
    print(f"Budget A — arcagent/core/  (limit: {_ARCAGENT_CORE_BUDGET:,} NCLOC)  [G1.5]")
    print(_separator())

    a_total, a_rows, a_passed = check_arcagent_core(root)
    for label, count in a_rows:
        print(_row(label, count))

    print(_separator())
    print(_row("TOTAL  arcagent core", a_total, _ARCAGENT_CORE_BUDGET))
    print()

    if not a_passed:
        all_passed = False
        over_by = a_total - _ARCAGENT_CORE_BUDGET
        print(
            f"  FAIL: arcagent core budget EXCEEDED by {over_by} lines "
            f"({a_total} / {_ARCAGENT_CORE_BUDGET}).\n"
            f"  The core must stay under {_ARCAGENT_CORE_BUDGET} NCLOC (ADR-004).\n"
            f"  Move complexity into modules/ or adapters/.\n",
            file=sys.stderr,
        )
    else:
        margin = _ARCAGENT_CORE_BUDGET - a_total
        print(f"  PASS: arcagent core budget OK ({a_total} / {_ARCAGENT_CORE_BUDGET}, "
              f"{margin} lines remaining).")
        print()

    # ------------------------------------------------------------------
    # Budget B — arcgateway core ≤ 1,200
    # ------------------------------------------------------------------
    print()
    print(
        f"Budget B — arcgateway core files  (limit: {_ARCGATEWAY_CORE_BUDGET:,} NCLOC)  [G1.6]"
    )
    print(_separator())

    b_total, b_rows, b_passed = check_arcgateway_core(root)
    for label, count in b_rows:
        print(_row(label, count))

    print(_separator())
    print(_row("TOTAL  arcgateway core", b_total, _ARCGATEWAY_CORE_BUDGET))
    print()

    if not b_passed:
        all_passed = False
        over_by = b_total - _ARCGATEWAY_CORE_BUDGET
        print(
            f"  FAIL: arcgateway core budget EXCEEDED by {over_by} lines "
            f"({b_total} / {_ARCGATEWAY_CORE_BUDGET}).\n"
            f"  Refactor runner.py / session.py / executor.py / adapters/base.py.\n"
            f"  Move protocol stubs and helpers to separate modules.\n",
            file=sys.stderr,
        )
    else:
        margin = _ARCGATEWAY_CORE_BUDGET - b_total
        print(f"  PASS: arcgateway core budget OK ({b_total} / {_ARCGATEWAY_CORE_BUDGET}, "
              f"{margin} lines remaining).")
        print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    if all_passed:
        print("ALL LOC BUDGETS PASSED (G1.5 + G1.6)")
    else:
        print("LOC BUDGET FAILURE — see FAIL lines above.")
    print("=" * 60)
    print()

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
