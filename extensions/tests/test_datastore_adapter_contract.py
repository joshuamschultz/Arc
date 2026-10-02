"""Every real datastore adapter honours the ``DatastorePort`` signature.

DGX: ``TypeError: SQLiteAttachment.introspect() got an unexpected keyword argument
'sample_limit'`` x143 -- the sqlite connector had been written against an older
shape of the port, and its own tests called ``introspect()`` bare, so it passed
every check except the one the runtime makes. This contract calls the port the way
the runtime does, on EVERY adapter that exposes one, and scans the bundle tree so
a new datastore connector cannot opt out by being left off a list.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.modules.connectors.install import build_attachment
from arcmemory.datastore import DatastorePort

from extensions.tests.fake_credential import FakeCredentialHandle

_EXTENSIONS = Path(__file__).resolve().parents[1]


def _datastore_classes() -> list[type]:
    """Every attachment class under extensions/ that offers a ``datastore_port``."""
    found: list[type] = []
    for bundle in sorted(_EXTENSIONS.iterdir()):
        package = next(iter(bundle.glob("arc_ext_*")), None) if bundle.is_dir() else None
        if package is None:
            continue
        module_name = f"extensions.{bundle.name}.{package.name}"
        module = importlib.import_module(module_name)
        modules = [module] + [
            importlib.import_module(f"{module_name}.{info.name}")
            for info in pkgutil.iter_modules(module.__path__)
        ]
        for mod in modules:
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if cls.__module__ == mod.__name__ and hasattr(cls, "datastore_port"):
                    found.append(cls)
    return found


def test_the_scan_finds_the_datastore_connectors() -> None:
    names = {cls.__name__ for cls in _datastore_classes()}
    assert {"SQLiteAttachment", "PostgreSQLAttachment"} <= names


@pytest.mark.parametrize("cls", _datastore_classes(), ids=lambda cls: cls.__name__)
def test_the_signature_matches_the_port(cls: type) -> None:
    introspect = inspect.signature(cls.introspect)  # type: ignore[attr-defined]
    persist = inspect.signature(cls.persist_ontology)  # type: ignore[attr-defined]

    assert introspect.parameters["sample_limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert persist.parameters["source_id"].kind is inspect.Parameter.KEYWORD_ONLY
    # The Protocol is the reference; if the port moves this test must move with it.
    assert "sample_limit" in inspect.signature(DatastorePort.introspect).parameters


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "operator"))


async def test_sqlite_is_called_the_way_the_runtime_calls_it(tmp_path: Path) -> None:
    from extensions.sqlite.arc_ext_sqlite import build_native_attachment

    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO customers VALUES (1, 'Ada'), (2, 'Grace');"
    )
    conn.commit()
    conn.close()
    attachment = build_native_attachment(
        {"database_path": str(path), "host": "", "connection_id": "shop"}
    )

    port: Any = await attachment.datastore_port()
    bare = await port.introspect()
    sampled = await port.introspect(sample_limit=1)
    store = _FactStore()
    await port.persist_ontology(store, source_id="source-shop")

    assert "customers" in bare.tables
    assert "customers" in sampled.tables
    assert store.facts, "persist_ontology wrote nothing"


class _FactStore:
    def __init__(self) -> None:
        self.facts: list[tuple[Any, ...]] = []

    def write_fact(self, *args: Any, **kwargs: Any) -> None:
        self.facts.append(args)


class _Connection:
    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
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
    async def __aenter__(self) -> _Connection:
        return _Connection()

    async def __aexit__(self, *_: object) -> None:
        return None


class _Pool:
    def acquire(self) -> _Acquire:
        return _Acquire()

    async def close(self) -> None:
        return None


class _Driver:
    async def create_pool(self, **kwargs: object) -> _Pool:
        return _Pool()


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Driver]:
    value = _Driver()
    monkeypatch.setitem(sys.modules, "asyncpg", value)
    yield value


async def test_postgres_is_called_the_way_the_runtime_calls_it(driver: _Driver) -> None:
    bundle = _EXTENSIONS / "postgresql"
    manifest = load_manifest(
        (bundle / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(
        manifest,
        bundle,
        {},
        connection_id="pg",
        credential=FakeCredentialHandle(  # type: ignore[arg-type]  # structural stand-in
            fields={"database_dsn": "postgresql://reader:secret@db.example/app"}
        ),
    )
    attachment = wrapper._delegate

    port: Any = await attachment.datastore_port()
    bare = await port.introspect()
    sampled = await port.introspect(sample_limit=3)
    store = _FactStore()
    await port.persist_ontology(store, source_id="source-pg")
    await attachment.close_source()

    assert "public.widgets" in bare.tables
    assert "public.widgets" in sampled.tables
    assert store.facts, "persist_ontology wrote nothing"
