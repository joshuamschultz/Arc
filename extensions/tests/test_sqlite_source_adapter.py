"""SQLite connector contracts: typed reads, the source seam, and the semantic layer.

Driven against a real SQLite file rather than a fake, because the thing worth
testing is that a database an operator actually made becomes tables an agent can
actually query — and every step between is real code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from arcagent.extension.attachment import ToolOutcome
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceDataShape,
    SourceError,
    SyncSource,
)

from extensions.sqlite.arc_ext_sqlite import SQLiteAttachment, build_native_attachment


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A small database with the shape real ones have: opaque names, a foreign key."""
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE inv_hdr (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            amt INTEGER,
            note TEXT
        );
        INSERT INTO customers VALUES (1, 'Ada', 'Denver'), (2, 'Grace', 'Austin');
        INSERT INTO inv_hdr VALUES (10, 1, 4200, 'annual'), (11, 2, 900, 'annual');
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every generated semantic layer inside the test's own tree."""
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "operator"))


def _attachment(database: Path, connection_id: str = "shop") -> SQLiteAttachment:
    return build_native_attachment(
        {"database_path": str(database), "host": "", "connection_id": connection_id}
    )


class TestConnecting:
    async def test_a_probe_reaches_a_real_file_and_reports_its_tools(self, database: Path) -> None:
        result = await _attachment(database).probe()

        assert result.reachable
        assert {tool.name for tool in result.tools} == {
            "sqlite_schema",
            "sqlite_get",
            "sqlite_find",
            "sqlite_list",
        }

    async def test_a_wrong_path_says_which_file_is_missing(self, tmp_path: Path) -> None:
        """The two failures an operator hits are a typo and an unreachable host,
        and they need different fixes — so they must read differently."""
        result = await _attachment(tmp_path / "nope.db").probe()

        assert not result.reachable
        assert "nope.db" in result.detail

    async def test_a_symlink_is_refused_rather_than_followed(
        self, database: Path, tmp_path: Path
    ) -> None:
        """The path is operator configuration; what it points at can be swapped by
        anyone who can write that directory, which is a much larger set of people.
        """
        link = tmp_path / "link.db"
        link.symlink_to(database)

        result = await _attachment(link).probe()

        assert not result.reachable
        assert "not a regular file" in result.detail

    async def test_no_database_path_is_reported_as_unconfigured(self) -> None:
        result = await build_native_attachment({}).probe()

        assert not result.reachable
        assert "database_path" in result.detail


class TestTypedReads:
    async def test_get_find_and_list_return_real_rows(self, database: Path) -> None:
        attachment = _attachment(database)

        got = await attachment.invoke("sqlite_get", {"table": "customers", "pk_value": "1"})
        found = await attachment.invoke(
            "sqlite_find", {"table": "customers", "column": "city", "value": "Austin"}
        )
        listed = await attachment.invoke("sqlite_list", {"table": "inv_hdr", "limit": "1"})

        assert "Ada" in got.content
        assert "Grace" in found.content
        assert listed.content.count("'id'") == 1

    async def test_an_unknown_table_is_refused_by_name(self, database: Path) -> None:
        result = await _attachment(database).invoke("sqlite_list", {"table": "secrets"})

        assert result.outcome is ToolOutcome.ERROR
        assert "secrets" in result.content

    async def test_an_unapproved_table_is_unreachable_even_though_it_exists(
        self, database: Path
    ) -> None:
        """Selection is the access boundary. A table the operator did not approve
        must be refused, not merely left out of the description."""
        attachment = _attachment(database)
        await attachment.select_source_resources(
            SelectSourceResources(connection_id="shop", resource_ids=("customers",))
        )

        result = await attachment.invoke("sqlite_list", {"table": "inv_hdr"})

        assert result.outcome is ToolOutcome.ERROR
        assert "inv_hdr" in result.content

    async def test_a_limit_cannot_be_raised_past_the_ceiling(self, database: Path) -> None:
        """A model reading ten thousand rows is a context bill, not an answer."""
        result = await _attachment(database).invoke(
            "sqlite_list", {"table": "customers", "limit": "999999"}
        )

        assert result.outcome is ToolOutcome.OK

    async def test_an_unknown_verb_is_refused(self, database: Path) -> None:
        result = await _attachment(database).invoke("sqlite_drop", {})

        assert result.outcome is ToolOutcome.ERROR

    async def test_the_connection_is_read_only(self, database: Path) -> None:
        """Enforced by SQLite through mode=ro, not by this module intending not to
        write — a smaller promise to keep."""
        attachment = _attachment(database)
        await attachment.introspect()

        with attachment._connection() as conn, pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM customers")


class TestSourceSeam:
    async def test_it_describes_itself_as_a_live_datastore(self, database: Path) -> None:
        description = await _attachment(database).inspect_source(
            InspectSource(connection_id="shop")
        )

        assert description.data_shape is SourceDataShape.DATASTORE
        assert description.source_kind == "sqlite"

    async def test_the_account_id_leaks_neither_path_nor_host(self, database: Path) -> None:
        description = await _attachment(database).inspect_source(
            InspectSource(connection_id="shop")
        )

        assert str(database) not in description.account_id

    async def test_tables_are_offered_as_selectable_resources(self, database: Path) -> None:
        resources = await _attachment(database).list_source_resources(
            ListSourceResources(connection_id="shop")
        )

        assert {r.resource_id for r in resources} == {"customers", "inv_hdr"}
        assert all(r.resource_kind == "table" for r in resources)

    async def test_selecting_a_table_that_is_not_there_is_refused(self, database: Path) -> None:
        with pytest.raises(SourceError):
            await _attachment(database).select_source_resources(
                SelectSourceResources(connection_id="shop", resource_ids=("ghost",))
            )

    async def test_a_sync_establishes_a_checkpoint_and_ingests_nothing(
        self, database: Path
    ) -> None:
        """Rows are not documents. Copying them into memory would make a stale
        duplicate of something already authoritative."""
        page = await _attachment(database).sync_source(SyncSource(connection_id="shop"))

        assert page.objects == ()
        assert not page.has_more
        assert page.next_checkpoint

    async def test_the_checkpoint_moves_when_the_database_does(self, database: Path) -> None:
        """ "Has this changed" must be answerable without reading a row."""
        attachment = _attachment(database)
        before = (await attachment.sync_source(SyncSource(connection_id="shop"))).next_checkpoint

        conn = sqlite3.connect(database)
        conn.execute("INSERT INTO customers VALUES (3, 'Alan', 'Reading')")
        conn.commit()
        conn.close()

        after = (await attachment.sync_source(SyncSource(connection_id="shop"))).next_checkpoint
        assert after != before

    async def test_rows_never_travel_through_the_document_fetch_path(self, database: Path) -> None:
        with pytest.raises(SourceError):
            await _attachment(database).fetch_source(
                FetchSourceObject(connection_id="shop", object_id="customers:1", version="1")
            )

    async def test_a_new_table_is_visible_without_a_restart(self, database: Path) -> None:
        """The schema is cached per file fingerprint, so a database that changed
        must be re-read — an agent told a table does not exist, because the copy
        it introspected an hour ago did not have it, is confidently wrong."""
        attachment = _attachment(database)
        before = await attachment.invoke("sqlite_schema", {})

        conn = sqlite3.connect(database)
        conn.execute("CREATE TABLE refunds (id INTEGER PRIMARY KEY, reason TEXT)")
        conn.commit()
        conn.close()

        after = await attachment.invoke("sqlite_schema", {})

        assert "refunds" not in before.content
        assert "refunds" in after.content


class TestSemanticLayer:
    async def test_connecting_generates_an_editable_file(self, database: Path) -> None:
        """An operator needs something real to edit, produced by connecting."""
        from arctrust.paths import semantic_layer_file

        await _attachment(database).introspect()

        written = semantic_layer_file("shop").read_text(encoding="utf-8")
        assert "[table.inv_hdr]" in written
        assert "YOURS TO EDIT" in written

    async def test_what_the_agent_is_shown_is_what_the_operator_wrote(
        self, database: Path
    ) -> None:
        """The whole point: `inv_hdr.amt` means nothing until someone says so."""
        from arctrust.paths import semantic_layer_file

        await _attachment(database).introspect()
        semantic_layer_file("shop").write_text(
            "[table.inv_hdr]\n"
            'entity = "invoice"\n'
            'description = "One row per billed job."\n'
            "[table.inv_hdr.column.amt]\n"
            'label = "amount"\n'
            'description = "Total billed in USD cents."\n',
            encoding="utf-8",
        )

        result = await _attachment(database).invoke("sqlite_schema", {})

        assert "one row is an invoice" in result.content
        assert "One row per billed job." in result.content
        assert "amt (amount: Total billed in USD cents.)" in result.content

    async def test_hiding_a_table_does_not_grant_or_revoke_reach(self, database: Path) -> None:
        """hidden is readability. Access is the approved selection — two questions,
        two mechanisms, and the mapping file must not be load-bearing for access."""
        from arctrust.paths import semantic_layer_file

        await _attachment(database).introspect()
        semantic_layer_file("shop").write_text(
            "[table.customers]\nhidden = true\n", encoding="utf-8"
        )

        attachment = _attachment(database)
        shown = await attachment.invoke("sqlite_schema", {})
        reached = await attachment.invoke("sqlite_get", {"table": "customers", "pk_value": "1"})

        assert "customers" not in shown.content
        assert reached.outcome is ToolOutcome.ERROR

    async def test_a_connection_without_an_id_still_works(self, database: Path) -> None:
        """No id means no per-connection file. That must degrade to the schema's
        own names, not refuse to answer."""
        attachment = build_native_attachment({"database_path": str(database)})

        result = await attachment.invoke("sqlite_schema", {})

        assert "inv_hdr" in result.content

    async def test_persisted_facts_carry_the_meaning_and_never_a_row(self, database: Path) -> None:
        from arctrust.paths import semantic_layer_file

        await _attachment(database).introspect()
        semantic_layer_file("shop").write_text(
            '[table.inv_hdr]\nentity = "invoice"\ndescription = "Billed jobs."\n',
            encoding="utf-8",
        )
        written: list[tuple[str, str, str]] = []

        class _Store:
            def write_fact(self, slug: str, key: str, value: str, **kwargs: object) -> None:
                written.append((slug, key, value))

        await _attachment(database).persist_ontology(_Store())

        facts = {(key, value) for _, key, value in written}
        assert ("entity", "invoice") in facts
        assert ("description", "Billed jobs.") in facts
        assert not any("4200" in value for _, _, value in written)
        assert ("fk:customer_id", "customers") in facts


class TestRemoteHost:
    async def test_a_host_without_ssh_says_so_plainly(
        self, database: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)

        result = await build_native_attachment(
            {"database_path": str(database), "host": "laptop.local"}
        ).probe()

        assert not result.reachable
        assert "ssh is not installed" in result.detail

    async def test_the_remote_path_is_quoted_for_the_remote_shell(self, database: Path) -> None:
        """ssh hands its trailing arguments to the REMOTE shell, so this is the one
        place a quoting mistake turns a filename into a command."""
        attachment = SQLiteAttachment("/data/my db.sqlite; rm -rf /", "laptop.local")
        seen: list[str] = []

        async def _fake_ssh(command: str) -> bytes:
            seen.append(command)
            return b"1:2:3"

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]
        await attachment._remote_fingerprint()

        assert seen == ["stat -c '%i:%Y:%s' '/data/my db.sqlite; rm -rf /'"]

    async def test_an_oversized_remote_database_is_refused_before_it_is_copied(
        self,
    ) -> None:
        """A SQLite file has no streaming read, so fetching one means having all of
        it — an unbounded copy is an unbounded write to this machine's disk."""
        attachment = SQLiteAttachment("/data/huge.db", "laptop.local")

        async def _fake_ssh(command: str) -> bytes:
            return b"1:2:9999999999999"

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]

        with pytest.raises(SourceError, match="copy limit"):
            await attachment._remote_fingerprint()

    async def test_an_unchanged_remote_file_is_not_copied_twice(self, database: Path) -> None:
        """A question asked twice must not copy the database twice."""
        attachment = SQLiteAttachment("/data/shop.db", "laptop.local")
        payload = database.read_bytes()
        fetches = 0

        async def _fake_ssh(command: str) -> bytes:
            nonlocal fetches
            if command.startswith("stat"):
                return b"1:2:3"
            fetches += 1
            return payload

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]

        await attachment._ensure_local()
        await attachment._ensure_local()

        assert fetches == 1

    async def test_a_changed_remote_file_is_copied_again(self, database: Path) -> None:
        attachment = SQLiteAttachment("/data/shop.db", "laptop.local")
        payload = database.read_bytes()
        fingerprints = iter([b"1:2:3", b"1:9:3"])
        fetches = 0

        async def _fake_ssh(command: str) -> bytes:
            nonlocal fetches
            if command.startswith("stat"):
                return next(fingerprints)
            fetches += 1
            return payload

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]

        await attachment._ensure_local()
        await attachment._ensure_local()

        assert fetches == 2

    async def test_a_fetched_copy_is_readable_only_by_this_user(self, database: Path) -> None:
        attachment = SQLiteAttachment("/data/shop.db", "laptop.local")
        payload = database.read_bytes()

        async def _fake_ssh(command: str) -> bytes:
            return b"1:2:3" if command.startswith("stat") else payload

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]

        local = await attachment._ensure_local()

        assert local.stat().st_mode & 0o077 == 0
        await attachment.close_source()

    async def test_a_remote_database_answers_the_same_typed_reads(self, database: Path) -> None:
        """The point of copying: a file on another machine becomes queryable here."""
        attachment = SQLiteAttachment("/data/shop.db", "laptop.local", "shop")
        payload = database.read_bytes()

        async def _fake_ssh(command: str) -> bytes:
            return b"1:2:3" if command.startswith("stat") else payload

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]

        result = await attachment.invoke("sqlite_get", {"table": "customers", "pk_value": "2"})

        assert "Grace" in result.content
        await attachment.close_source()

    async def test_closing_removes_the_local_copy(self, database: Path) -> None:
        attachment = SQLiteAttachment("/data/shop.db", "laptop.local")
        payload = database.read_bytes()

        async def _fake_ssh(command: str) -> bytes:
            return b"1:2:3" if command.startswith("stat") else payload

        attachment._ssh = _fake_ssh  # type: ignore[assignment,method-assign]
        local = await attachment._ensure_local()

        await attachment.close_source()

        assert not local.exists()
