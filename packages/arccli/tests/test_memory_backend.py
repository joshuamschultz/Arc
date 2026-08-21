"""``arc memory backend`` — can an operator confirm the index backend answers?

The index backend is swappable (sqlite per-agent file, or a shared postgres +
pgvector server). A deployment that points memory at postgres must be able to
prove — before it trusts the switch — that the server is actually reachable and
its schema initialises. This command probes the real backend (opens it and runs
one trivial read), reports the verdict, and exits non-zero when postgres is down
so a deploy check can gate on it. The sqlite path reports per-workspace vec
status and always exits 0 (a local file backend is always "up").
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arccli.commands.memory import memory_handler

_PG_DSN = os.environ.get("ARC_MEMORY_PG_DSN")


def test_backend_sqlite_reports_vec_status_per_workspace(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A real sqlite workspace -> exit 0, output names the backend and vec status."""
    from arcmemory.db import MemoryDB

    workspace = tmp_path / "agent"
    (workspace / "memory" / "entities").mkdir(parents=True)
    MemoryDB(workspace).connect().close()  # a real, discoverable index

    memory_handler(["backend", str(workspace)])  # no SystemExit == exit 0

    out = capsys.readouterr().out
    assert str(workspace) in out
    assert "sqlite" in out.lower()
    assert "vec" in out.lower()


def test_backend_sqlite_no_workspace_prints_default_note(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """No workspace + sqlite -> a plain note, exit 0."""
    memory_handler(["backend"])

    out = capsys.readouterr().out
    assert "sqlite" in out.lower()


def test_backend_postgres_without_dsn_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """postgres with no DSN -> connected False (ValueError caught), rendered, exit 1."""
    monkeypatch.delenv("ARC_MEMORY_PG_DSN", raising=False)

    with pytest.raises(SystemExit) as exc:
        memory_handler(["backend", "--index-backend", "postgres"])

    assert exc.value.code == 1
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert "postgres" in text.lower()
    assert "dsn" in text.lower()  # the caught error names the missing DSN


@pytest.mark.skipif(not _PG_DSN, reason="ARC_MEMORY_PG_DSN not set")
def test_backend_postgres_connects_when_dsn_set(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """Where a real DSN is set, postgres connects and the command exits 0."""
    memory_handler(["backend", "--index-backend", "postgres"])  # no SystemExit

    out = capsys.readouterr().out
    assert "postgres" in out.lower()
    assert "connected" in out.lower()
