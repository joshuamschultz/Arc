"""`arc agent memory` — a straight, read-only view of an agent's memory DATABASE.

Reads ``workspace/memory/index.db`` directly (the raw episodic stream + the entity
graph), organized by type with metadata — the DB reality, not the curated markdown.
Complements arcui's Knowledge view (which renders the glass-box markdown cards).

Read-only: the DB is opened ``mode=ro`` and never written.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from arccli.commands._shared import print_table as _print_table
from arccli.commands.agent._common import _resolve_agent_dir

_TEXT_MAX = 70


def _memory(args: argparse.Namespace) -> None:
    """Print a straight database view of the agent's stored memory."""
    agent_dir = _resolve_agent_dir(args.path)
    mem_dir = agent_dir / "workspace" / "memory"
    db_path = mem_dir / "index.db"
    if not db_path.is_file():
        sys.stdout.write(f"No memory database at {db_path}\n")
        sys.stdout.write("(the agent has captured nothing yet).\n")
        return

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        view = _collect(conn, mem_dir, limit=args.limit)
    finally:
        conn.close()

    if args.json:
        sys.stdout.write(json.dumps(view, indent=2, default=str) + "\n")
        return
    _render(view, agent_dir.name, db_path)


def _collect(conn: sqlite3.Connection, mem_dir: Path, *, limit: int) -> dict[str, Any]:
    """Gather the DB reality (episodic, graph edges) + curated-file counts."""
    return {
        "scope": _scalar(conn, "SELECT scope FROM episodic LIMIT 1") or "(none)",
        "counts": {
            "episodic": _scalar(conn, "SELECT COUNT(*) FROM episodic") or 0,
            "graph_edges": _scalar(conn, "SELECT COUNT(*) FROM edges") or 0,
            "indexed_chunks": _scalar(conn, "SELECT COUNT(*) FROM fts_chunks") or 0,
            "entity_files": _file_count(mem_dir / "entities"),
            "insight_files": _file_count(mem_dir / "insights"),
            "procedure_files": _file_count(mem_dir / "procedures"),
            "daily_notes": _file_count(mem_dir / "daily-log"),
        },
        "by_kind": _rows(
            conn, "SELECT kind, COUNT(*) FROM episodic GROUP BY kind ORDER BY COUNT(*) DESC"
        ),
        "episodic": _rows(
            conn,
            "SELECT ts, kind, classification, salience, entities, text "
            "FROM episodic ORDER BY seq DESC LIMIT ?",
            (limit,),
        ),
        "edges": _rows(
            conn,
            "SELECT src, dst, kind, weight FROM edges ORDER BY weight DESC LIMIT ?",
            (limit,),
        ),
    }


def _render(view: dict[str, Any], agent_name: str, db_path: Path) -> None:
    """Print the organized view: overview, episodic-by-type, entity graph."""
    counts = view["counts"]
    sys.stdout.write(f"\nMemory database — agent '{agent_name}'\n")
    sys.stdout.write(f"  scope: {view['scope']}\n  db:    {db_path}\n\n")

    _print_table(
        ["Type", "Count"],
        [
            ["episodic entries", str(counts["episodic"])],
            *[[f"  kind: {k}", str(n)] for k, n in view["by_kind"]],
            ["entity cards (files)", str(counts["entity_files"])],
            ["insight cards (files)", str(counts["insight_files"])],
            ["procedure cards (files)", str(counts["procedure_files"])],
            ["daily-notes (files)", str(counts["daily_notes"])],
            ["graph edges", str(counts["graph_edges"])],
            ["indexed chunks", str(counts["indexed_chunks"])],
        ],
    )

    if view["episodic"]:
        sys.stdout.write(f"\nEpisodic stream (latest {len(view['episodic'])}, newest first):\n")
        _print_table(
            ["Time", "Kind", "Class", "Sal", "Entities", "Text"],
            [
                [
                    str(ts)[:16],
                    str(kind),
                    str(cls),
                    f"{float(sal):.2f}",
                    ",".join(json.loads(ents or "[]"))[:20],
                    _clip(text),
                ]
                for ts, kind, cls, sal, ents, text in view["episodic"]
            ],
        )

    if view["edges"]:
        sys.stdout.write(f"\nEntity graph (top {len(view['edges'])} associations by weight):\n")
        _print_table(
            ["Source", "Target", "Kind", "Weight"],
            [[str(s), str(d), str(k), f"{float(w):.3f}"] for s, d, k, w in view["edges"]],
        )
    sys.stdout.write("\n")


def _clip(text: Any) -> str:
    """One-line, length-bounded rendering of an entry's text."""
    flat = " ".join(str(text).split())
    return flat[: _TEXT_MAX - 1] + "…" if len(flat) > _TEXT_MAX else flat


def _file_count(directory: Path) -> int:
    """Number of ``.md`` cards in a curated store dir (0 if absent)."""
    return len(list(directory.glob("*.md"))) if directory.is_dir() else 0


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    """First column of the first row, or None if the query fails / is empty."""
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    """All rows, or [] if the table is missing (a degraded DB must not crash the view)."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


__all__ = ["_memory"]
