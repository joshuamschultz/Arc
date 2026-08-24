"""Contract tests for the optional PostgreSQL/Supabase live datastore adapter."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.extension.source import InspectSource, ListSourceResources, SelectSourceResources
from arcagent.modules.connectors.install import build_attachment

_BUNDLE = Path(__file__).resolve().parents[1] / "postgresql"


class _Connection:
    def __init__(self, recorder: _Driver, *, fails: bool = False) -> None:
        self._recorder = recorder
        self._fails = fails

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self._recorder.calls.append((query, args))
        if self._fails:
            raise OSError("connection lost")
        if query == "SELECT 1":
            return [{"?column?": 1}]
        if "information_schema.tables" in query:
            return [{"table_schema": "public", "table_name": "widgets"}]
        if "information_schema.columns" in query:
            return [
                {"column_name": "id", "data_type": "integer"},
                {"column_name": "name", "data_type": "text"},
            ]
        if "table_constraints" in query:
            return [{"column_name": "id"}]
        return [{"id": 1, "name": "sprocket"}]


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)

    async def close(self) -> None:
        self.closed = True


class _Driver:
    def __init__(self, *, first_fails: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.pools: list[_Pool] = []
        self._first_fails = first_fails

    async def create_pool(self, **kwargs: object) -> _Pool:
        assert kwargs["min_size"] == 1
        assert kwargs["max_size"] == 4
        assert kwargs["statement_cache_size"] == 0
        pool = _Pool(_Connection(self, fails=self._first_fails and not self.pools))
        self.pools.append(pool)
        return pool


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Driver]:
    value = _Driver()
    monkeypatch.setitem(sys.modules, "asyncpg", value)
    yield value


def _attachment(connection_id: str = "") -> Any:
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(
        manifest,
        _BUNDLE,
        {"database_dsn": Secret("postgresql://reader:secret@db.example/app")},
        connection_id=connection_id,
    )
    return wrapper._delegate


async def test_resources_selection_and_typed_parameterized_reads(driver: _Driver) -> None:
    attachment = _attachment()
    resources = await attachment.list_source_resources(ListSourceResources(connection_id="pg"))
    assert [resource.resource_id for resource in resources] == ["public.widgets"]
    await attachment.select_source_resources(
        SelectSourceResources(connection_id="pg", resource_ids=("public.widgets",))
    )
    description = await attachment.inspect_source(InspectSource(connection_id="pg"))
    assert description.source_kind == "postgres"
    assert "secret" not in description.account_id

    result = await attachment.query(
        "find", "public.widgets", {"column": "name", "value": "sprocket", "limit": 10}
    )

    assert result == [{"id": 1, "name": "sprocket"}]
    statement, arguments = driver.calls[-1]
    assert '"public"."widgets"' in statement
    assert "sprocket" not in statement
    assert arguments == ("sprocket", 10)
    await attachment.close_source()
    assert driver.pools[0].closed


async def test_reconnects_after_a_lost_pool_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _Driver(first_fails=True)
    monkeypatch.setitem(sys.modules, "asyncpg", driver)
    attachment = _attachment()

    result = await attachment.probe()

    assert result.reachable
    assert len(driver.pools) == 2
    assert driver.pools[0].closed


async def test_rejects_unselected_or_injected_table_names(driver: _Driver) -> None:
    attachment = _attachment()
    await attachment.introspect()

    with pytest.raises(ValueError, match="unknown or unapproved"):
        await attachment.query("list", "public.widgets;DROP TABLE widgets", {})


@pytest.mark.skipif(
    not os.environ.get("ARC_TEST_POSTGRES_DSN"),
    reason="set ARC_TEST_POSTGRES_DSN for live PostgreSQL",
)
async def test_live_postgresql_probe() -> None:
    """Runs against a real local PostgreSQL/Supabase-compatible read-only endpoint."""
    import importlib

    importlib.import_module("asyncpg")
    attachment = _attachment_for_dsn(os.environ["ARC_TEST_POSTGRES_DSN"])
    try:
        assert (await attachment.probe()).reachable
    finally:
        await attachment.close_source()


def _attachment_for_dsn(dsn: str) -> Any:
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(manifest, _BUNDLE, {"database_dsn": Secret(dsn)})
    return wrapper._delegate


@pytest.fixture
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any generated semantic layer inside the test's own tree."""
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "operator"))


async def test_postgres_honours_the_operators_semantic_layer(
    driver: _Driver, _isolated_config: None
) -> None:
    """The same editable meanings sqlite gets, or an operator learns the feature
    exists per-connector — which is how one datastore came to describe itself and
    the other to answer with a bare list of table names.
    """
    from arctrust.paths import semantic_layer_file

    await _attachment("app").introspect()
    semantic_layer_file("app").write_text(
        '[table."public.widgets"]\n'
        'entity = "widget"\n'
        'description = "One row per manufactured part."\n'
        '[table."public.widgets".column.name]\n'
        'label = "part name"\n'
        'description = "What the shop floor calls it."\n',
        encoding="utf-8",
    )

    answer = await _attachment("app").invoke("postgres_schema", {})

    assert "one row is a widget" in answer.content
    assert "One row per manufactured part." in answer.content
    assert "name (part name: What the shop floor calls it.)" in answer.content


async def test_connecting_postgres_generates_an_editable_layer(
    driver: _Driver, _isolated_config: None
) -> None:
    """An operator needs something real to edit, produced by connecting."""
    from arctrust.paths import semantic_layer_file

    await _attachment("app").introspect()

    written = semantic_layer_file("app").read_text(encoding="utf-8")
    assert 'table."public.widgets"' in written
    assert "YOURS TO EDIT" in written


async def test_postgres_without_a_connection_id_still_answers(driver: _Driver) -> None:
    """No id means no per-connection file; that must degrade, not refuse."""
    answer = await _attachment().invoke("postgres_schema", {})

    assert "public.widgets" in answer.content
