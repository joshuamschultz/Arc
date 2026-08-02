"""Run every Arc test suite in isolated pytest processes.

Arc packages intentionally ship independently installable ``tests`` packages.
Collecting all of them in one pytest process aliases their ``conftest`` modules,
so the trustworthy monorepo gate is one process per package plus one for the
repository-level integration and architecture tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_SUITES = (
    *(path.relative_to(ROOT) for path in sorted((ROOT / "packages").glob("*/tests"))),
    Path("packages/arctui/src/arctui/tests"),
    Path("tests"),
)


def main() -> int:
    """Run all suites, preserving extra pytest arguments and reporting all failures."""
    pytest_args = sys.argv[1:] or ["-m", "not slow"]
    failed: list[Path] = []
    for suite in TEST_SUITES:
        command = [sys.executable, "-m", "pytest", str(suite), *pytest_args]
        completed = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603
        if completed.returncode != 0:
            failed.append(suite)

    if failed:
        print("Failed test suites:", file=sys.stderr)
        for suite in failed:
            print(f"  - {suite}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
