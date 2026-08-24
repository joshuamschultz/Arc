"""The operator-editable meaning of a datastore's tables.

Introspection says a table is called ``inv_hdr``. Only a person can say it holds
invoices. These tests pin the contract that makes that person's answer durable:
it is generated so there is something to edit, it is never overwritten, and it
can narrow what an agent sees but never widen it past the real schema.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from arcmemory.datastore import DatastoreOntology, TableInfo
from arcmemory.semantic_layer import (
    SemanticLayer,
    apply_semantic_layer,
    describe,
    ensure_semantic_layer,
    load_semantic_layer,
    render_semantic_layer,
)


def _ontology(*names: str) -> DatastoreOntology:
    return DatastoreOntology(
        tables={
            name: TableInfo(
                name=name,
                primary_key="id",
                columns=["id", "amt", "note"],
                searchable_columns=["note"],
                foreign_keys={},
            )
            for name in names
        },
        entity_map={},
    )


class TestGeneration:
    def test_a_starting_file_names_every_table_and_column(self, tmp_path: Path) -> None:
        """An operator needs something real to edit, not an empty file."""
        path = tmp_path / "mydb.toml"

        ensure_semantic_layer(path, "mydb", _ontology("invoices", "jobs"))

        written = tomllib.loads(path.read_text(encoding="utf-8"))
        assert set(written["table"]) == {"invoices", "jobs"}
        assert set(written["table"]["invoices"]["column"]) == {"id", "amt", "note"}

    def test_the_implied_singular_is_prefilled_and_descriptions_are_not(
        self, tmp_path: Path
    ) -> None:
        """A field to correct beats one to invent — but a made-up sentence is worse
        than none, because an agent cannot tell it was made up."""
        path = tmp_path / "mydb.toml"

        ensure_semantic_layer(path, "mydb", _ontology("invoices"))

        table = tomllib.loads(path.read_text(encoding="utf-8"))["table"]["invoices"]
        assert table["entity"] == "invoice"
        assert table["description"] == ""

    def test_the_file_is_owner_only(self, tmp_path: Path) -> None:
        """It names the shape of a private database; another user has no business
        reading it."""
        path = tmp_path / "mydb.toml"

        ensure_semantic_layer(path, "mydb", _ontology("invoices"))

        assert path.stat().st_mode & 0o077 == 0


class TestOperatorEditsSurvive:
    def test_a_second_scan_never_rewrites_an_edited_entry(self, tmp_path: Path) -> None:
        """The sentence an operator wrote is the one thing here that cannot be
        regenerated from the database."""
        path = tmp_path / "mydb.toml"
        ensure_semantic_layer(path, "mydb", _ontology("invoices"))
        path.write_text(
            '[table.invoices]\nentity = "bill"\ndescription = "What we charged."\n',
            encoding="utf-8",
        )

        ensure_semantic_layer(path, "mydb", _ontology("invoices"))

        layer = load_semantic_layer(path)
        assert layer.table["invoices"].entity == "bill"
        assert layer.table["invoices"].description == "What we charged."

    def test_a_later_scan_appends_only_the_new_tables(self, tmp_path: Path) -> None:
        path = tmp_path / "mydb.toml"
        ensure_semantic_layer(path, "mydb", _ontology("invoices"))
        path.write_text('[table.invoices]\nentity = "bill"\n', encoding="utf-8")

        ensure_semantic_layer(path, "mydb", _ontology("invoices", "jobs"))

        layer = load_semantic_layer(path)
        assert layer.table["invoices"].entity == "bill"
        assert layer.table["jobs"].entity == "job"

    def test_an_entry_for_a_vanished_table_is_left_alone(self, tmp_path: Path) -> None:
        """A renamed table must not silently delete the description of the old one."""
        path = tmp_path / "mydb.toml"
        path.write_text('[table.old_name]\ndescription = "years of context"\n', encoding="utf-8")

        ensure_semantic_layer(path, "mydb", _ontology("new_name"))

        layer = load_semantic_layer(path)
        assert layer.table["old_name"].description == "years of context"
        assert "new_name" in layer.table


class TestReading:
    def test_a_typo_degrades_to_the_schemas_own_names(self, tmp_path: Path) -> None:
        """This file is hand-edited, so a stray quote is an ordinary Tuesday.

        Refusing every question about a database because its description file
        has a syntax error is a worse failure than answering with table names.
        """
        path = tmp_path / "mydb.toml"
        path.write_text('[table.invoices]\nentity = "unclosed\n', encoding="utf-8")

        assert load_semantic_layer(path).table == {}

    def test_a_missing_file_is_an_empty_layer(self, tmp_path: Path) -> None:
        assert load_semantic_layer(tmp_path / "absent.toml").table == {}

    def test_an_unknown_key_does_not_take_the_whole_file_down(self, tmp_path: Path) -> None:
        """Strict validation is right; failing closed on it here is not."""
        path = tmp_path / "mydb.toml"
        path.write_text('[table.invoices]\nnonsense = "typo"\n', encoding="utf-8")

        assert load_semantic_layer(path).table == {}


class TestApplication:
    def test_the_operators_entity_name_replaces_the_implied_one(self) -> None:
        layer = SemanticLayer.model_validate({"table": {"inv_hdr": {"entity": "invoice"}}})

        applied = apply_semantic_layer(_ontology("inv_hdr"), layer)

        assert applied.entity_map == {"invoice": "table:inv_hdr"}

    def test_hidden_keeps_a_table_out_of_what_an_agent_is_shown(self) -> None:
        layer = SemanticLayer.model_validate({"table": {"jobs": {"hidden": True}}})

        applied = apply_semantic_layer(_ontology("invoices", "jobs"), layer)

        assert set(applied.tables) == {"invoices"}

    def test_searchable_narrows_to_columns_the_table_actually_has(self) -> None:
        """The file is hand-edited. A column that does not exist must be dropped
        here, not turned into a query the database rejects three layers down."""
        layer = SemanticLayer.model_validate(
            {"table": {"invoices": {"searchable": ["note", "typo_column"]}}}
        )

        applied = apply_semantic_layer(_ontology("invoices"), layer)

        assert applied.tables["invoices"].searchable_columns == ["note"]

    def test_naming_only_absent_columns_keeps_what_was_detected(self) -> None:
        """An edit that lands on nothing must not silently make a table unsearchable."""
        layer = SemanticLayer.model_validate({"table": {"invoices": {"searchable": ["nope"]}}})

        applied = apply_semantic_layer(_ontology("invoices"), layer)

        assert applied.tables["invoices"].searchable_columns == ["note"]

    def test_a_layer_cannot_invent_a_table(self) -> None:
        """It describes what introspection found; it is not a second schema."""
        layer = SemanticLayer.model_validate({"table": {"ghost": {"entity": "ghost"}}})

        applied = apply_semantic_layer(_ontology("invoices"), layer)

        assert set(applied.tables) == {"invoices"}


class TestDescribe:
    def test_the_agent_reads_meaning_before_column_names(self) -> None:
        """This is what a model sees before it decides what to query. A bare list
        of table names sent agents guessing."""
        layer = SemanticLayer.model_validate(
            {
                "table": {
                    "inv_hdr": {
                        "entity": "invoice",
                        "description": "One row per billed job.",
                        "column": {"amt": {"label": "amount", "description": "USD cents."}},
                    }
                }
            }
        )

        text = describe(apply_semantic_layer(_ontology("inv_hdr"), layer), layer)

        assert "one row is a invoice" in text
        assert "One row per billed job." in text
        assert "amt (amount: USD cents.)" in text

    def test_an_empty_datastore_says_so(self) -> None:
        assert describe(DatastoreOntology(tables={}, entity_map={}), SemanticLayer()) == (
            "No approved tables found."
        )


def test_a_rendered_file_is_valid_toml_for_awkward_identifiers() -> None:
    """Real schemas have table names TOML cannot take as a bare key."""
    ontology = DatastoreOntology(
        tables={
            "order.items": TableInfo(
                name="order.items",
                primary_key="id",
                columns=["id", "unit price"],
                searchable_columns=[],
                foreign_keys={},
            )
        },
        entity_map={},
    )

    parsed = tomllib.loads(render_semantic_layer("mydb", ontology))

    assert "order.items" in parsed["table"]
    assert "unit price" in parsed["table"]["order.items"]["column"]
