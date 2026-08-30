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

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from arctrust.artifact import ArtifactSignature
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from arcmemory.datastore import DatastoreOntology

#: Detached-signature sidecar, exactly like an arcprompt overlay (COMP-007). Its
#: PRESENCE is what turns verification on: a file an operator has never signed
#: through arcui behaves exactly as before (typo-tolerant, degrade-don't-crash),
#: while a file that HAS been signed must keep verifying on every read — so a
#: direct filesystem edit after signing (bypassing arcui) is a tamper, not a
#: silent content change, and fails loud rather than quietly feeding an agent's
#: every DB search (LLM01: this file's text is instruction-adjacent content).
SIGNATURE_SUFFIX = ".arcsig"


class SemanticLayerTamperedError(RuntimeError):
    """A signed semantic layer's bytes no longer match its ``.arcsig`` sidecar.

    Raised instead of degrading to an empty/partial layer: a syntax typo in a
    hand-edited file is an ordinary Tuesday, but a signed file whose content no
    longer matches its signature means either corruption or an attacker editing
    instruction-adjacent content out from under the signature that was supposed
    to protect it. Neither is safe to paper over by falling back to schema names.
    """


def _operator_public_key() -> bytes | None:
    """The deployment operator's Ed25519 verify key, or ``None`` if unavailable.

    Mirrors ``arcagent.core.prompt_context._operator_public_key`` — the arcui
    write path signs with the same on-box operator key, so pinning it here is
    what makes a signed layer verify. Absent on a bare/test deployment -> None,
    which fails a signed file's verification closed rather than skipping it.
    """
    try:
        from arctrust import OperatorKey, default_operator_key_path

        return OperatorKey.load(default_operator_key_path(), generate_if_absent=False).public_key
    except (OSError, ValueError, RuntimeError):
        return None


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
    #: Bounded, REDACTED example values captured at the table's first scan (see
    #: ``datastore.py``). Generated once, exactly like ``description`` — an
    #: operator can edit or blank these; a later scan never overwrites them.
    samples: list[str] = Field(default_factory=list)


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

    #: The connection's trust label, inherited (not operator-authored) so a file
    #: that now carries real sample values never loses the classification of the
    #: data it was drawn from. Re-stamped from the live connection on every
    #: ``overlay()`` call — this is provenance, not a hand-edited field.
    classification: str = "unclassified"
    table: dict[str, TableMeaning] = Field(default_factory=dict)

    def entity_for(self, table: str) -> str:
        """The operator's name for one row of ``table``, or empty if unnamed."""
        return self.table.get(table, TableMeaning()).entity

    def is_hidden(self, table: str) -> bool:
        """Whether the operator asked for this table to stay out of the way."""
        return self.table.get(table, TableMeaning()).hidden


def load_semantic_layer(path: Path) -> SemanticLayer:
    """Read one connection's layer. A missing or unreadable file is an empty one.

    Unreadable is deliberately not an error — UNLESS the file carries a
    ``.arcsig`` sidecar, meaning it has been signed through arcui at least once.
    A signed file's bytes MUST still match the signature that vouches for them,
    or this raises :class:`SemanticLayerTamperedError` rather than degrading, exactly
    like an arcprompt overlay (COMP-007): a signed file is instruction-adjacent
    content consulted on every DB search, so a mismatch is a tamper, not a typo.
    A file that has never been signed keeps the old behavior: a stray quote from
    a CLI hand edit is an ordinary Tuesday, not an incident.
    """
    sig_path = path.with_name(path.name + SIGNATURE_SUFFIX)
    if sig_path.is_file():
        _verify_signed(path, sig_path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return SemanticLayer()
    try:
        return SemanticLayer.model_validate(raw)
    except ValueError:
        return SemanticLayer()


def _verify_signed(path: Path, sig_path: Path) -> None:
    """Verify a signed layer's bytes against its ``.arcsig`` sidecar; fail loud."""
    from arcprompt.verifier import SignatureVerifier

    try:
        content = path.read_bytes()
        manifest = ArtifactSignature.from_json(sig_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SemanticLayerTamperedError(
            f"{path} has a {SIGNATURE_SUFFIX} sidecar that could not be read: {exc}"
        ) from exc
    # Mandatory pinning (same posture as arcprompt): an unpinned key is refused,
    # not skipped — a None pin would let an attacker self-sign with a throwaway
    # keypair and have it verify.
    if not SignatureVerifier(_operator_public_key()).verify(content, manifest):
        raise SemanticLayerTamperedError(
            f"{path} failed signature verification against the pinned operator key "
            "— its content no longer matches the last signed save"
        )


def _is_signed(path: Path) -> bool:
    return path.with_name(path.name + SIGNATURE_SUFFIX).is_file()


def _render_tables(ontology: DatastoreOntology) -> str:
    """Render every table's ``[table.x]`` / ``[table.x.column.y]`` blocks, nothing else.

    Shared by :func:`render_semantic_layer` (full file) and the "append new
    tables" path in :func:`ensure_semantic_layer`, so a later scan's addition is
    never at risk of re-emitting a top-level key (like ``classification``) that
    TOML forbids declaring twice.
    """
    lines: list[str] = []
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
            samples = info.sample_values.get(column, [])
            if samples:
                lines.append(f"samples = [{', '.join(_quote(v) for v in samples)}]")
            lines.append("")
    return "\n".join(lines)


def render_semantic_layer(
    connection_id: str, ontology: DatastoreOntology, *, classification: str = "unclassified"
) -> str:
    """Render a starting file for ``ontology`` — every table, nothing invented.

    ``entity`` is pre-filled with the naive singular the schema already implies,
    because a field an operator can correct beats one they must fill in from
    nothing. Everything else is left blank on purpose: a made-up description is
    worse than none, since an agent cannot tell the difference. ``classification``
    is inherited provenance, not a field to fill in — it mirrors the connection's
    own registered trust label so a file that may carry real sample values never
    understates what it holds.
    """
    header = _HEADER.format(connection_id=connection_id)
    return f"{header}\nclassification = {_quote(classification)}\n\n{_render_tables(ontology)}"


def ensure_semantic_layer(
    path: Path,
    connection_id: str,
    ontology: DatastoreOntology,
    *,
    classification: str = "unclassified",
) -> Path:
    """Create the file if absent, or append only the tables it does not mention.

    Never rewrites an existing entry. An operator's sentence about a table is
    the one thing here that cannot be regenerated from the database, so a scan
    that found a renamed table adds the new name and leaves the old entry alone.

    A file already carrying a ``.arcsig`` sidecar is operator-owned: this
    function has no signing identity, so it must never mutate a byte of it —
    doing so would desync the content from the signature that vouches for it
    and turn the operator's own save into a tamper failure on the next read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            render_semantic_layer(connection_id, ontology, classification=classification),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path
    if _is_signed(path):
        return path
    _restamp_classification(path, classification)
    known = set(load_semantic_layer(path).table)
    missing = {name: info for name, info in ontology.tables.items() if name not in known}
    if missing:
        from arcmemory.datastore import DatastoreOntology as _Ontology

        body = _render_tables(_Ontology(tables=missing, entity_map={}))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n# --- tables found by a later scan ---\n" + body)
    return path


_CLASSIFICATION_LINE = re.compile(r'^classification\s*=\s*".*"\s*$', re.MULTILINE)


def _restamp_classification(path: Path, classification: str) -> None:
    """Keep the top-level ``classification`` line in sync with the live connection.

    Provenance metadata, not an operator's sentence — unlike table entries this
    is always kept current, so a re-classified connection never leaves a stale
    (too-low) label in a file that may carry real sample values. A pre-H-025
    file has no such line yet; one is inserted rather than skipped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    line = f"classification = {_quote(classification)}"
    new_text = (
        _CLASSIFICATION_LINE.sub(line, text, count=1)
        if _CLASSIFICATION_LINE.search(text)
        else f"{line}\n{text}"
    )
    if new_text != text:
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError:
            pass


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


def overlay(
    connection_id: str, ontology: DatastoreOntology, *, classification: str = "unclassified"
) -> DatastoreOntology:
    """Ensure the editable file exists, then return the ontology as it describes.

    The one call every datastore adapter makes, so sqlite and postgres cannot
    drift into applying an operator's descriptions differently — or, worse, one
    of them not applying them at all. ``classification`` is the label the whole
    connected datastore is trusted at (SPEC-073 no-read-up); it is stamped onto
    the file so provenance travels with any sample values it may hold.

    A config directory that cannot be written is not a reason to refuse the
    database: the layer degrades to the schema's own names, which is exactly what
    an operator who has never edited it would see anyway. A SIGNED file that
    fails verification is different — :class:`SemanticLayerTamperedError` propagates
    rather than degrading (fail-closed on a verification failure), because
    serving a tampered instruction-adjacent file is worse than refusing the call.
    """
    path = layer_path(connection_id)
    if path is None:
        return ontology
    try:
        ensure_semantic_layer(path, connection_id, ontology, classification=classification)
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
        line = f"{column} ({described.label or column}: {described.description})"
    elif described.label and described.label != column:
        line = f"{column} ({described.label})"
    else:
        line = column
    if described.samples:
        line += f" e.g. {', '.join(described.samples)}"
    return line


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
    """A TOML basic string. Schema identifiers only ever needed quote/backslash
    escaping; a sample VALUE is real (if bounded, redacted) row data and can
    contain newlines/tabs a bare basic string cannot hold literally."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"' + escaped + '"'


__all__ = [
    "SIGNATURE_SUFFIX",
    "ColumnMeaning",
    "SemanticLayer",
    "SemanticLayerTamperedError",
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
