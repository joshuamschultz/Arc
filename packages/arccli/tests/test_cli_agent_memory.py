"""`arc agent memory` — a straight, read-only database view of stored memory.

Reads the episodic stream + entity graph directly from ``workspace/memory/index.db``
(the DB reality), organized by type — NOT the curated markdown that arcui renders.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from arccli.commands.agent._dispatch import agent_handler


def _agent_with_memory(tmp_path: Path) -> Path:
    """Scaffold an agent dir and seed a real index.db with episodic rows."""
    pytest.importorskip("arcmemory")
    from arcmemory.db import MemoryDB
    from arcmemory.stores.episodic import EpisodicStore
    from arcmemory.types import Event

    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n', encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    db = MemoryDB(workspace)
    db.connect()
    store = EpisodicStore(db, workspace)
    store.append(
        Event(
            event_id="e0",
            scope="did:arc:aria",
            kind="respond",
            text="Brad Baker is the CTO of CTGFederal",
            classification="unclassified",
            salience=0.8,
            entities=["brad-baker", "ctgfederal"],
        )
    )
    store.append(
        Event(event_id="e1", scope="did:arc:aria", kind="tool", text="tool:bash -> ok")
    )
    return tmp_path


def test_memory_shows_db_entries_grouped_by_type(tmp_path: Path) -> None:
    agent = _agent_with_memory(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["memory", str(agent)])
    text = out.getvalue()

    assert "did:arc:aria" in text  # the scope
    assert "episodic entries" in text and "2" in text  # count overview
    assert "respond" in text and "tool" in text  # grouped by kind
    assert "Brad Baker is the CTO" in text  # the actual entry text
    assert "brad-baker" in text  # entry metadata (tagged entities)


def test_memory_json_output_is_machine_readable(tmp_path: Path) -> None:
    agent = _agent_with_memory(tmp_path)
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["memory", str(agent), "--json"])
    data = json.loads(out.getvalue())

    assert data["scope"] == "did:arc:aria"
    assert data["counts"]["episodic"] == 2
    assert {k for k, _ in data["by_kind"]} == {"respond", "tool"}


def test_memory_no_database_is_graceful(tmp_path: Path) -> None:
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n', encoding="utf-8"
    )
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["memory", str(tmp_path)])
    assert "No memory database" in out.getvalue()
