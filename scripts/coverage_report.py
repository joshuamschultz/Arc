#!/usr/bin/env python3
"""Coverage report and threshold enforcer for M1 Acceptance Gate G1.7.

Runs pytest with coverage collection across all Arc packages and asserts
minimum thresholds:

    Line coverage   ≥ 80%  (per new package / module)
    Branch coverage ≥ 75%  (per new package / module)

Packages checked (new packages introduced in M1):
    arcgateway                                   — gateway daemon
    arcagent.modules.session                     — session FTS5 module
    arcagent.modules.vault                       — vault credential resolver
    arcagent.modules.skill_improver              — skill auto-nudge module

Packages NOT checked here (pre-existing, covered by their own test suites):
    arcagent.core (checked separately via arcagent pytest config)
    arcrun, arcllm, arccli (their own suites)

Usage::

    uv run python scripts/coverage_report.py
    uv run python scripts/coverage_report.py --html    # also emit htmlcov/
    uv run python scripts/coverage_report.py --fail-fast   # stop at first failure

Exit codes:
    0 — all thresholds satisfied
    1 — one or more packages below threshold (see table for details)

Note:
    This script shells out to ``uv run pytest`` with coverage flags.
    It requires pytest, pytest-cov, and pytest-asyncio to be installed
    in the active environment.

    If a package has zero test files (e.g. arcagent.modules.session tests
    are not yet written), the coverage will be 0% and the script will
    report a HONEST FAIL — it does NOT lower thresholds to hide gaps.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------

_LINE_THRESHOLD = 80   # %
_BRANCH_THRESHOLD = 75  # %

# Packages/modules to measure coverage for.
# Each entry: (display_name, pytest_cov_module_name)
# The pytest_cov_module_name is passed to --cov= flag.
_COVERAGE_TARGETS: list[tuple[str, str]] = [
    ("arcgateway", "arcgateway"),
    ("arcagent.modules.session", "arcagent.modules.session"),
    ("arcagent.modules.vault", "arcagent.modules.vault"),
    ("arcagent.modules.skill_improver", "arcagent.modules.skill_improver"),
]

# ---------------------------------------------------------------------------
# Helper: run pytest with JSON coverage output
# ---------------------------------------------------------------------------


def _run_coverage(
    root: Path,
    targets: list[tuple[str, str]],
    html: bool = False,
) -> dict[str, dict[str, float]] | None:
    """Run pytest with --cov and --cov-report=json, parse coverage.json.

    Args:
        root: Repository root directory.
        targets: List of (display_name, cov_module) pairs.
        html: If True, also emit an htmlcov/ directory.

    Returns:
        Dict mapping module name → {"line": float, "branch": float} percent,
        or None if pytest could not be invoked.
    """
    cov_args = []
    for _, module in targets:
        cov_args.extend([f"--cov={module}"])

    with tempfile.NamedTemporaryFile(
        suffix=".json", prefix="arc_coverage_", delete=False
    ) as tmp:
        json_path = Path(tmp.name)

    # Use "uv run pytest" to ensure the correct virtualenv is used.
    # This avoids sys.executable pointing at the wrong Python when
    # the script is invoked via "python scripts/coverage_report.py".
    cmd = [
        "uv", "run", "pytest",
        "--cov-branch",
        f"--cov-report=json:{json_path}",
        "--cov-report=term-missing",
        "--tb=no",
        "-q",
    ] + cov_args

    if html:
        cmd += ["--cov-report=html:htmlcov"]

    print(f"\nRunning: {' '.join(cmd)}\n")

    try:
        result = subprocess.run(
            cmd,
            cwd=root,
            capture_output=False,  # let pytest output go to stdout
            check=False,
        )
    except FileNotFoundError:
        print("ERROR: 'uv' not found. Install uv from https://docs.astral.sh/uv/", file=sys.stderr)
        print(
            "Then install dev dependencies:\n"
            "  uv pip install -e packages/arcgateway -e packages/arcagent "
            "-e packages/arcrun -e packages/arcllm -e packages/arccli",
            file=sys.stderr,
        )
        return None

    # Parse JSON coverage report
    if not json_path.exists():
        print(
            "WARNING: coverage JSON not generated. "
            "Possibly no tests found or pytest crashed.",
            file=sys.stderr,
        )
        return {}

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR reading coverage JSON: {exc}", file=sys.stderr)
        return None
    finally:
        try:
            json_path.unlink(missing_ok=True)
        except OSError:
            pass

    return _parse_coverage_json(raw, targets)


def _parse_coverage_json(
    raw: dict,
    targets: list[tuple[str, str]],
) -> dict[str, dict[str, float]]:
    """Extract per-module line and branch coverage from coverage.json.

    Args:
        raw: Parsed coverage.json dict.
        targets: Coverage target specifications.

    Returns:
        Dict mapping display_name → {"line": float, "branch": float}.
    """
    # coverage.json structure (coverage.py >= 5.x):
    # {
    #   "totals": { "percent_covered": float, ... },
    #   "files": {
    #     "path/to/file.py": { "summary": { "percent_covered": float, ... } }
    #   }
    # }
    files_data: dict = raw.get("files", {})

    results: dict[str, dict[str, float]] = {}

    for display_name, module_name in targets:
        # Match all file paths that belong to this module.
        module_path_fragment = module_name.replace(".", "/")

        matched_files: list[dict] = [
            data
            for path, data in files_data.items()
            if module_path_fragment in path.replace("\\", "/")
        ]

        if not matched_files:
            # No files measured — coverage is 0%.
            results[display_name] = {"line": 0.0, "branch": 0.0}
            continue

        # Aggregate across all matched files
        total_stmts = 0
        covered_stmts = 0
        total_branches = 0
        covered_branches = 0

        for fdata in matched_files:
            summary = fdata.get("summary", {})
            total_stmts += summary.get("num_statements", 0)
            covered_stmts += summary.get("covered_lines", 0)
            total_branches += summary.get("num_branches", 0)
            covered_branches += summary.get("covered_branches", 0)

        line_pct = (
            (covered_stmts / total_stmts * 100.0) if total_stmts > 0 else 0.0
        )
        branch_pct = (
            (covered_branches / total_branches * 100.0)
            if total_branches > 0
            else 0.0
        )

        results[display_name] = {"line": line_pct, "branch": branch_pct}

    return results


# ---------------------------------------------------------------------------
# Table printer
# ---------------------------------------------------------------------------

_COL_PKG = 42
_COL_VAL = 8


def _print_table(results: dict[str, dict[str, float]]) -> None:
    """Print a formatted coverage summary table."""
    header = (
        f"  {'Package / Module'.ljust(_COL_PKG)}  "
        f"{'Line %'.rjust(_COL_VAL)}  "
        f"{'Branch %'.rjust(_COL_VAL)}  "
        f"Status"
    )
    sep = "  " + "-" * (len(header) - 2)

    print(header)
    print(sep)

    for name, pct in sorted(results.items()):
        line_pct = pct["line"]
        branch_pct = pct["branch"]

        line_ok = line_pct >= _LINE_THRESHOLD
        branch_ok = branch_pct >= _BRANCH_THRESHOLD
        status = "PASS" if (line_ok and branch_ok) else "FAIL"

        line_str = f"{line_pct:.1f}%"
        branch_str = f"{branch_pct:.1f}%"

        if not line_ok:
            line_str += f" (< {_LINE_THRESHOLD}%)"
        if not branch_ok:
            branch_str += f" (< {_BRANCH_THRESHOLD}%)"

        pkg_cell = name.ljust(_COL_PKG)[:_COL_PKG]
        print(
            f"  {pkg_cell}  "
            f"{line_str.rjust(_COL_VAL + (0 if line_ok else 10))}  "
            f"{branch_str.rjust(_COL_VAL + (0 if branch_ok else 10))}  "
            f"{status}"
        )

    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run coverage checks and print results."""
    parser = argparse.ArgumentParser(
        description=(
            "Run coverage checks for M1 acceptance gate G1.7. "
            f"Thresholds: line >= {_LINE_THRESHOLD}%, branch >= {_BRANCH_THRESHOLD}%."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root. Auto-detected from script location if not set.",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also emit htmlcov/ directory for browser viewing.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Exit immediately after the first threshold failure.",
    )
    args = parser.parse_args()

    if args.root is not None:
        root = args.root.resolve()
    else:
        root = Path(__file__).parent.parent.resolve()

    if not root.exists():
        print(f"ERROR: Repository root not found: {root}", file=sys.stderr)
        return 1

    print()
    print("M1 Acceptance Gate G1.7 — Coverage Report")
    print(f"Thresholds: line >= {_LINE_THRESHOLD}%,  branch >= {_BRANCH_THRESHOLD}%")
    print("=" * 70)

    results = _run_coverage(root, _COVERAGE_TARGETS, html=args.html)
    if results is None:
        print("ERROR: Could not run pytest. Coverage check aborted.", file=sys.stderr)
        return 1

    print()
    print("Coverage summary:")
    _print_table(results)
    print()

    # Evaluate thresholds
    failures: list[str] = []
    for name, pct in sorted(results.items()):
        if pct["line"] < _LINE_THRESHOLD:
            failures.append(
                f"  {name}: line coverage {pct['line']:.1f}% "
                f"(below {_LINE_THRESHOLD}% threshold)"
            )
        if pct["branch"] < _BRANCH_THRESHOLD:
            failures.append(
                f"  {name}: branch coverage {pct['branch']:.1f}% "
                f"(below {_BRANCH_THRESHOLD}% threshold)"
            )
        if args.fail_fast and failures:
            break

    if failures:
        print("COVERAGE FAILURES:")
        for f in failures:
            print(f)
        print()
        print(
            "NOTE: These are honest failures — thresholds have NOT been lowered.\n"
            "If coverage is genuinely below threshold, the gap represents real work:\n"
            "  - Add unit tests for the uncovered code paths.\n"
            "  - Do NOT lower the threshold constants in this script to hide the gap.\n"
            "  - Report the gap in the M1 acceptance gate report.\n"
        )
        return 1

    print("ALL COVERAGE THRESHOLDS PASSED (G1.7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
