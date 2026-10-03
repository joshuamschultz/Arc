"""v14: delete pre-P18 per-agent ``connections`` rows (no ``connection`` key).

The DGX kept two rows from the per-agent connection era (``agent`` + ``instance``
fields, no ``connection`` key). Every ``ConnectionStateStore.list()`` logged them
as unreadable. The migration deletes exactly those rows and nothing else. The
real-PostgreSQL proof is ``test_postgres_v14_deletes_legacy_per_agent_connection_rows``.
"""

from __future__ import annotations

from importlib.resources import files

from arcstore.migrations import SCHEMA_HEAD


def _sql() -> str:
    return files("arcstore.migrations").joinpath("v14.sql").read_text(encoding="utf-8")


def test_schema_head_includes_v14() -> None:
    assert SCHEMA_HEAD >= 14


def test_v14_deletes_only_connection_rows_without_a_connection_key() -> None:
    sql = " ".join(_sql().split())
    assert "DELETE FROM mutable_records" in sql
    assert "collection = 'connections'" in sql
    assert "NOT (value ? 'connection')" in sql
