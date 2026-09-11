"""Run every Arc test suite in isolated pytest processes.

One process per package, plus one for the repository-level integration and
architecture tests.

This is no longer about collection. Module names used to alias, and a whole-repo
run died in collection while silently dropping the trees it could not name; that
is fixed, and ``uv run pytest`` from the root now collects the entire suite with
zero errors. Keep running it — it is the only check that proves nothing is
silently uncollected, which per-package runs structurally cannot tell you.

Neither is it about process-global leakage any more. Three leaks did cross
package boundaries in a shared interpreter, and all three are now fixed at the
source rather than papered over by separate processes: the OpenTelemetry global
tracer provider (set-once, never restored), the asyncio current event loop
(``asyncio.run`` clears it, and a sync test built a loop-bound
``StreamReader``), and ``ARC_CONFIG_DIR`` (assigned through raw ``os.environ``
with no teardown, repointing the deployment root for every later package). A
whole-repo run is green, so separate processes buy no isolation this suite
depends on.

What this script is now is the per-package gate: it proves each package's suite
stands on its own, which is how CI runs them and what a standalone-installable
package has to be able to claim.
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
