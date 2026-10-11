"""v15: the work-event log and its callback outbox (SPEC-088 COMP-012, T-1336).

A fresh database applies every packaged migration, v1 through v15, in order.
The real-PostgreSQL proof is ``test_postgres_fresh_schema_applies_v1_through_v15``.
"""

from __future__ import annotations

from importlib.resources import files

from arcstore.backends.base import STORE_TABLES
from arcstore.migrations import SCHEMA_HEAD, migrate


def _sql() -> str:
    return " ".join(
        files("arcstore.migrations").joinpath("v15.sql").read_text(encoding="utf-8").split()
    )


def test_schema_head_includes_v15() -> None:
    assert SCHEMA_HEAD >= 15


def test_v15_creates_work_event_tables_named_in_store_tables() -> None:
    sql = _sql()
    assert "CREATE TABLE IF NOT EXISTS work_events" in sql
    assert "seq bigserial NOT NULL UNIQUE" in sql
    assert "CREATE TABLE IF NOT EXISTS work_event_deliveries" in sql
    assert "REFERENCES work_events(event_id)" in sql
    assert "CHECK (state IN ('pending', 'delivered', 'withheld', 'dead'))" in sql
    assert {"work_events", "work_event_deliveries"} <= STORE_TABLES


async def test_fresh_database_applies_v1_through_v15_in_order() -> None:
    applied: list[int] = []

    class Connection:
        async def execute(self, sql: str, *args: object) -> None:
            if "INSERT INTO arcstore_schema" in sql:
                applied.append(int(str(args[0])))

        async def fetchval(self, sql: str, *args: object) -> None:
            return None

    await migrate(Connection())

    assert applied == list(range(1, SCHEMA_HEAD + 1))
    assert applied[-1] >= 15
