"""Architecture test: the spool path pulls no DB/cloud driver (SPEC-026 FR-2, Task 1.8).

Runs in a fresh subprocess so the assertion is against a clean ``sys.modules`` —
importing ``arcstore.spool`` must not transitively import sqlite3, asyncpg, or
any cloud SDK. This protects cold-start + baseline-memory budgets (NFR-2) and
the module boundary (producers import only the spool).
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = (
    "import arcstore.spool, sys; "
    "banned = [m for m in ('sqlite3', 'asyncpg', 'psycopg', 'psycopg2', 'boto3') "
    "if m in sys.modules]; "
    "assert not banned, banned; "
    "print('clean')"
)


def test_spool_import_pulls_no_backend() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout
