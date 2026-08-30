"""SPEC-073 COMP-008 (T-1032/T-1033) — read-only, dependency-free datastore
introspection: typed ontology + parameterized, allowlisted read ops. No agent-
supplied SQL string is ever executed.

RED: ``arcmemory.datastore`` does not exist yet — every test fails on import
(ModuleNotFoundError), a feature-absent reason, not a typo.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from arcmemory.datastore import (
    DatastoreOntology,
    DatastorePort,
    SqliteDatastore,
    SqliteDatastorePort,
    TableInfo,
)


@pytest.fixture
def invoices_conn(tmp_path: Path) -> sqlite3.Connection:
    """A temp sqlite db: invoices(id TEXT PK, amount, customer_id) + one row."""
    db_path = tmp_path / "invoices.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount REAL, customer_id TEXT)")
    conn.execute(
        "INSERT INTO invoices (id, amount, customer_id) VALUES ('001', 249.99, 'cust-77')"
    )
    conn.commit()
    return conn


def test_introspect_yields_table_info_for_invoices(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    ontology = store.introspect()

    assert isinstance(ontology, DatastoreOntology)
    assert "invoices" in ontology.tables
    table = ontology.tables["invoices"]
    assert isinstance(table, TableInfo)
    assert table.name == "invoices"
    assert table.primary_key == "id"
    assert set(table.columns) == {"id", "amount", "customer_id"}


def test_introspect_builds_singular_noun_entity_map(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    ontology = store.introspect()
    assert ontology.entity_map.get("invoice") == "table:invoices"


def test_get_record_returns_row_by_primary_key(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    record = store.get_record("invoices", "001")
    assert record is not None
    assert record["id"] == "001"
    assert record["customer_id"] == "cust-77"


def test_get_record_returns_none_for_missing_pk(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    assert store.get_record("invoices", "does-not-exist") is None


def test_find_is_row_capped_at_the_given_limit(invoices_conn: sqlite3.Connection) -> None:
    for i in range(2, 60):
        invoices_conn.execute(
            "INSERT INTO invoices (id, amount, customer_id) VALUES (?, ?, ?)",
            (f"{i:03d}", 10.0, "cust-77"),
        )
    invoices_conn.commit()

    store = SqliteDatastore(invoices_conn)
    results = store.find("invoices", "customer_id", "cust-77", limit=10)
    assert len(results) == 10


def test_find_treats_the_value_as_a_bound_parameter_not_sql(
    invoices_conn: sqlite3.Connection,
) -> None:
    """A value shaped like a SQL injection attempt matches nothing (it is bound, not concatenated)."""
    store = SqliteDatastore(invoices_conn)
    malicious = "cust-77' OR '1'='1"
    results = store.find("invoices", "customer_id", malicious)
    assert results == []


def test_query_dispatches_get_record_by_op_name(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    result = store.query("get_record", "invoices", {"pk_value": "001"})
    assert result is not None
    assert result["id"] == "001"


def test_query_unknown_op_raises_value_error(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    with pytest.raises(ValueError):
        store.query("drop_table", "invoices", {})


def test_get_record_unknown_table_raises_value_error(invoices_conn: sqlite3.Connection) -> None:
    store = SqliteDatastore(invoices_conn)
    with pytest.raises(ValueError):
        store.get_record("not_a_real_table", "001")


def test_no_public_method_accepts_a_raw_sql_string_parameter() -> None:
    """No SQLite adapter method accepts raw SQL to execute verbatim."""
    forbidden_param_names = {"sql", "raw_sql", "query_str", "statement", "stmt"}
    public_methods = [
        name
        for name in dir(SqliteDatastore)
        if not name.startswith("_") and callable(getattr(SqliteDatastore, name, None))
    ]
    for name in public_methods:
        try:
            sig = inspect.signature(getattr(SqliteDatastore, name))
        except (TypeError, ValueError):
            continue
        offending = forbidden_param_names & {p.lower() for p in sig.parameters}
        assert not offending, (
            f"SqliteDatastore.{name} accepts raw-SQL-shaped param(s): {offending}"
        )


async def test_sqlite_adapter_implements_backend_neutral_async_port(
    invoices_conn: sqlite3.Connection,
) -> None:
    port = SqliteDatastorePort(invoices_conn)

    assert isinstance(port, DatastorePort)
    ontology = await port.introspect()
    row = await port.query("get_record", "invoices", {"pk_value": "001"})

    assert "invoices" in ontology.tables
    assert isinstance(row, dict) and row["id"] == "001"


class TestSampleValues:
    """H-025 — bounded, deduplicated, REDACTED per-column example values."""

    def test_sample_limit_zero_captures_nothing(self, invoices_conn: sqlite3.Connection) -> None:
        ontology = SqliteDatastore(invoices_conn).introspect(sample_limit=0)

        assert ontology.tables["invoices"].sample_values == {}

    def test_sample_limit_captures_bounded_deduplicated_values(
        self, invoices_conn: sqlite3.Connection
    ) -> None:
        invoices_conn.execute(
            "INSERT INTO invoices (id, amount, customer_id) VALUES ('002', 10.0, 'cust-77')"
        )
        invoices_conn.commit()

        ontology = SqliteDatastore(invoices_conn).introspect(sample_limit=5)

        samples = ontology.tables["invoices"].sample_values["customer_id"]
        assert samples == ["cust-77"]  # deduplicated, not one row per insert

    def test_a_secret_shaped_value_is_redacted_before_it_is_returned(
        self, invoices_conn: sqlite3.Connection
    ) -> None:
        invoices_conn.execute(
            "INSERT INTO invoices (id, amount, customer_id) "
            "VALUES ('003', 5.0, 'key AKIA1234567890ABCDEF here')"
        )
        invoices_conn.commit()

        ontology = SqliteDatastore(invoices_conn).introspect(sample_limit=5)

        samples = ontology.tables["invoices"].sample_values["customer_id"]
        assert not any("AKIA1234567890ABCDEF" in s for s in samples)
        assert any("[SECRET:AWS_ACCESS_KEY]" in s for s in samples)

    def test_sampling_never_issues_a_distinct_query(self, tmp_path: Path) -> None:
        """Bounded LIMIT-only reads, never a full-table ``SELECT DISTINCT`` — a
        query planner can still execute that as an unbounded scan+sort on an
        unindexed column, which is exactly what H-025 forbids."""
        executed: list[str] = []

        class _RecordingConnection(sqlite3.Connection):
            def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
                executed.append(sql)
                return super().execute(sql, *args)

        conn = sqlite3.connect(str(tmp_path / "rec.db"), factory=_RecordingConnection)
        conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, customer_id TEXT)")
        conn.execute("INSERT INTO invoices VALUES ('001', 'cust-77')")
        conn.commit()
        executed.clear()

        SqliteDatastore(conn).introspect(sample_limit=5)

        assert not any("DISTINCT" in sql.upper() for sql in executed)
        assert any("LIMIT" in sql.upper() for sql in executed)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
