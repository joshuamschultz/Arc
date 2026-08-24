"""The operator-editable meaning of a connected datastore's tables and columns.

Introspection answers what a database *contains* — table names, column names,
primary keys. It cannot answer what any of it MEANS. A column called ``amt`` in
a table called ``inv_hdr`` is perfectly legible to the person who built it and
opaque to everything else, including an agent asked to find last month's
invoices.

This module is the missing half: a TOML file per connection, generated from the
introspected schema so there is something real to edit, and applied over that
schema on every read. It renames, describes, hides and re-scopes — it never
invents a table or a column that introspection did not find, so an edit can
narrow what an agent sees but never widen it past what the database actually
has.

**Generated once, never regenerated over.** The file is written the first time a
datastore is introspected and from then on it is the operator's. A later
introspection adds entries for tables that appeared and leaves every existing
entry exactly as written, including entries for tables that have gone away —
deleting an operator's sentence because a table was renamed loses work nothing
can reconstruct.

**It is not an authorization boundary.** ``hidden`` keeps a table out of what an
agent is *shown*; the approved-resource selection is what decides what an agent
may *reach*. Two mechanisms because they answer different questions, and a
mapping file an operator edits for readability must not be load-bearing for
access.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from arcmemory.datastore import DatastoreOntology

#: Written at the top of a generated file. An operator opening this needs to know
#: it is theirs to edit and that regeneration will not overwrite them.
_HEADER = """\
# Semantic layer for the "{connection_id}" datastore — YOURS TO EDIT.
#
# Generated from the database's own schema so there is something real to change.
# Arc reads this every time an agent asks about the data, and never rewrites an
# entry you have edited: a later scan only APPENDS tables that have appeared.
#
# Per table:
#   entity      what one row IS, singular ("invoice"). Agents search by this.
#   description one sentence an agent reads before deciding to query the table.
#   hidden      true to keep it out of what agents are shown (see below).
#   searchable  columns worth free-text searching; omit to keep what was detected.
#   [table.<name>.column.<column>] label / description for one column.
#
# hidden is readability, NOT permission: what an agent may REACH is the
# resource selection you approved when connecting. Hiding a table here does not
# secure it, and un-hiding one does not grant it.
"""


class ColumnMeaning(BaseModel):
    """What one column is, in an operator's words."""

    model_config = ConfigDict(extra="forbid")

    label: str = ""
    description: str = ""


class TableMeaning(BaseModel):
    """What one table is, in an operator's words."""

    model_config = ConfigDict(extra="forbid")

    entity: str = ""
    description: str = ""
    hidden: bool = False
    searchable: list[str] = Field(default_factory=list)
    column: dict[str, ColumnMeaning] = Field(default_factory=dict)


class SemanticLayer(BaseModel):
    """Every table's meaning for one connection."""

    model_config = ConfigDict(extra="forbid")

    table: dict[str, TableMeaning] = Field(default_factory=dict)

    def entity_for(self, table: str) -> str:
        """The operator's name for one row of ``table``, or empty if unnamed."""
        return self.table.get(table, TableMeaning()).entity

    def is_hidden(self, table: str) -> bool:
        """Whether the operator asked for this table to stay out of the way."""
        return self.table.get(table, TableMeaning()).hidden


def load_semantic_layer(path: Path) -> SemanticLayer:
    """Read one connection's layer. A missing or unreadable file is an empty one.

    Unreadable is deliberately not an error. This file is hand-edited, so a
    stray quote is an ordinary Tuesday — and refusing to answer any question
    about a database because its description file has a typo would be a worse
    failure than answering with the schema's own names.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return SemanticLayer()
    try:
        return SemanticLayer.model_validate(raw)
    except ValueError:
        return SemanticLayer()


def render_semantic_layer(connection_id: str, ontology: DatastoreOntology) -> str:
    """Render a starting file for ``ontology`` — every table, nothing invented.

    ``entity`` is pre-filled with the naive singular the schema already implies,
    because a field an operator can correct beats one they must fill in from
    nothing. Everything else is left blank on purpose: a made-up description is
    worse than none, since an agent cannot tell the difference.
    """
    lines = [_HEADER.format(connection_id=connection_id)]
    for name in sorted(ontology.tables):
        info = ontology.tables[name]
        lines.append(f"[table.{_key(name)}]")
        lines.append(f"entity = {_quote(_implied_entity(name))}")
        lines.append('description = ""')
        lines.append("hidden = false")
        lines.append(f"searchable = [{', '.join(_quote(c) for c in info.searchable_columns)}]")
        lines.append("")
        for column in info.columns:
            lines.append(f"[table.{_key(name)}.column.{_key(column)}]")
            lines.append(f"label = {_quote(column)}")
            lines.append('description = ""')
            lines.append("")
    return "\n".join(lines)


def ensure_semantic_layer(path: Path, connection_id: str, ontology: DatastoreOntology) -> Path:
    """Create the file if absent, or append only the tables it does not mention.

    Never rewrites an existing entry. An operator's sentence about a table is
    the one thing here that cannot be regenerated from the database, so a scan
    that found a renamed table adds the new name and leaves the old entry alone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(render_semantic_layer(connection_id, ontology), encoding="utf-8")
        path.chmod(0o600)
        return path
    known = set(load_semantic_layer(path).table)
    missing = {name: info for name, info in ontology.tables.items() if name not in known}
    if missing:
        from arcmemory.datastore import DatastoreOntology as _Ontology

        addition = render_semantic_layer(connection_id, _Ontology(tables=missing, entity_map={}))
        body = addition.split("\n", 1)[1] if "\n" in addition else ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n# --- tables found by a later scan ---\n" + body.lstrip("\n"))
    return path


def apply_semantic_layer(ontology: DatastoreOntology, layer: SemanticLayer) -> DatastoreOntology:
    """Return ``ontology`` as the operator describes it.

    Narrowing only: a ``searchable`` entry naming a column the table does not
    have is dropped rather than honoured. The file is hand-edited and an agent
    would otherwise build a query against a column that is not there — and be
    told by the database, three layers down, in a message nobody reads.
    """
    from arcmemory.datastore import DatastoreOntology as _Ontology

    tables = {}
    entity_map: dict[str, str] = {}
    for name, info in ontology.tables.items():
        meaning = layer.table.get(name, TableMeaning())
        if meaning.hidden:
            continue
        searchable = [
            c for c in meaning.searchable if c in info.columns
        ] or info.searchable_columns
        tables[name] = info.model_copy(update={"searchable_columns": searchable})
        entity_map[meaning.entity or _implied_entity(name)] = f"table:{name}"
    return _Ontology(tables=tables, entity_map=entity_map)


def layer_path(connection_id: str, base: Path | str | None = None) -> Path | None:
    """Resolve one connection's layer file, or ``None`` when it has no id.

    Through the accessor that owns the path, never a join here: two surfaces
    already want this file — the adapter that applies it and the CLI that opens
    it — and a second spelling would let an operator edit a file no agent reads.

    ``base`` is what ``--arc-dir`` means: operate on THAT deployment's tree. A
    surface that ignored it would show an operator the wrong deployment's
    meanings while every neighbouring command showed the right one's.
    """
    from arctrust.paths import semantic_layer_file

    if not connection_id:
        return None
    try:
        return semantic_layer_file(connection_id, base)
    except ValueError:
        return None


def layer_for(connection_id: str, base: Path | str | None = None) -> SemanticLayer:
    """One connection's meanings. An unnamed connection has none, not an error."""
    path = layer_path(connection_id, base)
    return load_semantic_layer(path) if path is not None else SemanticLayer()


def overlay(connection_id: str, ontology: DatastoreOntology) -> DatastoreOntology:
    """Ensure the editable file exists, then return the ontology as it describes.

    The one call every datastore adapter makes, so sqlite and postgres cannot
    drift into applying an operator's descriptions differently — or, worse, one
    of them not applying them at all.

    A config directory that cannot be written is not a reason to refuse the
    database: the layer degrades to the schema's own names, which is exactly what
    an operator who has never edited it would see anyway.
    """
    path = layer_path(connection_id)
    if path is None:
        return ontology
    try:
        ensure_semantic_layer(path, connection_id, ontology)
    except OSError:
        pass
    return apply_semantic_layer(ontology, layer_for(connection_id))


def describe(ontology: DatastoreOntology, layer: SemanticLayer) -> str:
    """One readable block naming every visible table in the operator's words.

    This is what an agent is shown before it decides what to query, so it leads
    with meaning: the entity name and the sentence an operator wrote, then the
    columns. A bare list of table names sent agents guessing, and a guess
    against a database is a query that returns nothing and explains nothing.
    """
    if not ontology.tables:
        return "No approved tables found."
    blocks = []
    for name in sorted(ontology.tables):
        info = ontology.tables[name]
        meaning = layer.table.get(name, TableMeaning())
        entity = meaning.entity or _implied_entity(name)
        head = f"{name} — one row is {_article(entity)} {entity}"
        if meaning.description:
            head += f". {meaning.description}"
        columns = ", ".join(_column_line(column, meaning) for column in info.columns)
        blocks.append(
            f"{head}\n  primary key: {info.primary_key or '(none)'}"
            f"\n  columns: {columns}"
            f"\n  searchable: {', '.join(info.searchable_columns) or '(none)'}"
        )
    return "\n\n".join(blocks)


def _column_line(column: str, meaning: TableMeaning) -> str:
    described = meaning.column.get(column, ColumnMeaning())
    if described.description:
        return f"{column} ({described.label or column}: {described.description})"
    if described.label and described.label != column:
        return f"{column} ({described.label})"
    return column


def _article(word: str) -> str:
    """ "a" or "an". The agent's description is read by a person too."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def _implied_entity(table: str) -> str:
    """The naive singular a table name already implies (``invoices`` -> ``invoice``)."""
    return table[:-1] if table.endswith("s") and len(table) > 1 else table


def _key(name: str) -> str:
    """A TOML key for an identifier that may not be bare-key safe."""
    return name if name.replace("_", "").replace("-", "").isalnum() else _quote(name)


def _quote(value: str) -> str:
    """A TOML basic string — the only escapes a schema identifier can need."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = [
    "ColumnMeaning",
    "SemanticLayer",
    "TableMeaning",
    "apply_semantic_layer",
    "describe",
    "ensure_semantic_layer",
    "layer_for",
    "layer_path",
    "load_semantic_layer",
    "overlay",
    "render_semantic_layer",
]
