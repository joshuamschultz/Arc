#!/usr/bin/env python3
"""Run the Alpha connected-data release gate.

The gate is intentionally explicit: it runs the cross-package connected-data
journey and the ArcUI operator lifecycle tests, rather than relying on package
test discovery to include them.  Set ``ARC_RELEASE_GATE_POSTGRES=1`` when a
PostgreSQL service is available to include ArcStore's source-sync integration
contract as well.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    tests = [
        "packages/arcagent/tests/e2e/test_connected_data_release_gate.py",
        "packages/arcui/tests/test_connected_data_routes.py",
    ]
    if os.environ.get("ARC_RELEASE_GATE_POSTGRES") == "1":
        tests.append("packages/arcstore/tests/integration/test_source_sync_postgres.py")
    return subprocess.run(  # noqa: S603 - executable and test paths are repository constants
        [sys.executable, "-m", "pytest", "-q", *tests], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
