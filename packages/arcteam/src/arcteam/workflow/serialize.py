"""Serialization and hashing for a workflow bundle (SPEC-061 REQ-226).

Three things live here because they are one idea: turning a definition into
bytes that a signature can bind to.

* :func:`dump_toml` writes the canonical ``workflow.toml``. It is deterministic
  — same definition, same bytes — so a resave never shows a spurious diff.
* :func:`file_manifest` digests every schema, prompt, and script the definition
  references, keyed by bundle-relative path.
* :func:`canonical_bytes` and :func:`content_hash` bind those two together.

The hash is taken over a canonical JSON projection of the *parsed* document
plus the manifest — never over raw TOML bytes. TOML has no canonical form: an
inline table and an array-of-tables are the same data, and a formatter may
legitimately rewrite one into the other. Hashing bytes would let a whitespace
change drop a signed workflow to draft, and would leave every referenced file
outside the signature's coverage entirely.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arctrust import canonical_json, content_sha256

from arcteam.workflow.models import AgentNode, ScriptNode, WorkflowDefinition
from arcteam.workflow.validator import confine

MISSING = "missing"
"""Manifest value for a referenced file that is not in the bundle.

A sentinel rather than an exception: a file that vanishes changes the manifest,
which changes the hash, which fails verification closed — exactly the outcome
wanted, reached without a second error path."""

UNRESOLVABLE = "unresolvable"
"""Manifest value for a reference that escapes the bundle directory."""


def referenced_files(definition: WorkflowDefinition) -> tuple[str, ...]:
    """Every schema, prompt, and script path the definition names, deduplicated."""
    paths: list[str] = []
    if definition.input_spec is not None:
        paths.append(definition.input_spec.schema_ref)
    for node in definition.nodes:
        if node.output_schema is not None:
            paths.append(node.output_schema)
        if isinstance(node, AgentNode) and node.prompt is not None:
            paths.append(node.prompt)
        if isinstance(node, ScriptNode):
            paths.append(node.script)
    return tuple(sorted(set(paths)))


def file_manifest(definition: WorkflowDefinition, root: Path) -> dict[str, str]:
    """Digest every referenced file, keyed by its bundle-relative path."""
    resolved_root = root.resolve()
    manifest: dict[str, str] = {}
    for reference in referenced_files(definition):
        target = confine(resolved_root, reference)
        if target is None:
            manifest[reference] = UNRESOLVABLE
        elif not target.is_file():
            manifest[reference] = MISSING
        else:
            manifest[reference] = content_sha256(target.read_bytes())
    return manifest


def canonical_bytes(definition: WorkflowDefinition, manifest: Mapping[str, str]) -> bytes:
    """The one byte form a workflow signature binds to.

    ``canonical_json`` sorts keys at every level, so the manifest is sorted by
    construction and the projection is stable across hosts and Python versions.
    """
    return canonical_json({"definition": definition.canonical_document(), "files": dict(manifest)})


def content_hash(definition: WorkflowDefinition, manifest: Mapping[str, str]) -> str:
    """``sha256:<hex>`` over :func:`canonical_bytes`."""
    return content_sha256(canonical_bytes(definition, manifest))


# --- TOML emission -----------------------------------------------------------


def dump_toml(document: Mapping[str, Any]) -> str:
    """Render a workflow document as TOML that parses back to the same document.

    Deliberately narrow: it handles exactly the shapes a workflow document can
    hold — a header table, optional trigger and input tables, and an
    array-of-tables of nodes whose values are scalars, arrays of scalars,
    inline tables, or arrays of inline tables.
    """
    lines: list[str] = []
    for table in ("workflow", "trigger", "input"):
        if table in document:
            lines.append(f"[{table}]")
            lines.extend(_emit_pairs(document[table]))
            lines.append("")
    for node in document.get("node", ()):
        lines.append("[[node]]")
        lines.extend(_emit_pairs(node))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _emit_pairs(table: Mapping[str, Any]) -> list[str]:
    return [f"{key} = {_format(value)}" for key, value in table.items()]


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _format_string(value)
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, Mapping):
        inner = ", ".join(f"{key} = {_format(item)}" for key, item in value.items())
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_format(item) for item in value) + "]"
    raise TypeError(f"a workflow document cannot hold {type(value).__name__}")


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _format_string(value: str) -> str:
    """Basic TOML string, escaping the characters that would break the quoting."""
    escaped = "".join(_ESCAPES.get(character, character) for character in value)
    return f'"{escaped}"'


__all__ = [
    "MISSING",
    "UNRESOLVABLE",
    "canonical_bytes",
    "content_hash",
    "dump_toml",
    "file_manifest",
    "referenced_files",
]
