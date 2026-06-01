"""Module-boundary test — arcrun imports only arcstore.spool (SPEC-026 AC-4.3).

Importing the event bus (the spool producer) must not transitively pull the
store backends or a DB driver. Run in a fresh subprocess for a clean
``sys.modules`` assertion.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = (
    "import arcrun.events, sys; "
    "banned = [m for m in ('sqlite3', 'asyncpg', 'psycopg', 'boto3', "
    "'arcstore.backends', 'arcstore.ingest') if m in sys.modules]; "
    "assert not banned, banned; "
    "print('clean')"
)


def test_producers_import_spool_only() -> None:
    result = subprocess.run(  # noqa: S603 — fixed trusted command, _PROBE is a constant
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout
