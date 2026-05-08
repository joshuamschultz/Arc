"""`arc agent sessions` — list session transcripts."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from arccli.commands.agent._common import _print_table, _resolve_agent_dir


def _sessions(args: argparse.Namespace) -> None:
    """List session transcripts for an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    sessions_dir = agent_dir / "workspace" / "sessions"

    if not sessions_dir.is_dir():
        sys.stdout.write("No sessions directory found.\n")
        return

    session_files = sorted(
        sessions_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not session_files:
        sys.stdout.write("No sessions found.\n")
        return

    rows = []
    for sf in session_files:
        stat = sf.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        line_count = sum(1 for _ in open(sf))
        size_kb = stat.st_size / 1024
        rows.append(
            [
                sf.stem,
                mtime.strftime("%Y-%m-%d %H:%M"),
                str(line_count),
                f"{size_kb:.1f} KB",
            ]
        )

    _print_table(["Session ID", "Last Modified", "Messages", "Size"], rows)
