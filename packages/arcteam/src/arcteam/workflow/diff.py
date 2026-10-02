"""What an operator is about to sign, against what they last signed (J3 F10).

An approval card that says "sign workflow X" without saying what changed asks
the operator to approve blind. This module answers with a node-level and a
file-level diff of the current draft against the most recent *signed* version.

Two limits are deliberate. Output is bounded (a prompt can be large and an
approval card is not a file viewer), and every line is passed through the
platform's secret/PII detector before it leaves: a signed bundle may embed a
credential, and a diff must not become the place it is shown.
"""

from __future__ import annotations

import difflib
from typing import Any, Literal, TypedDict

from arctrust.redaction import RegexPiiDetector, redact_text

from arcteam.workflow.models import WorkflowDefinition
from arcteam.workflow.serialize import referenced_files
from arcteam.workflow.store import DefinitionStore

#: Per-file diff text ceiling, in characters. Larger diffs are cut and marked.
MAX_DIFF_CHARS = 8_000

#: Files listed in one diff. A bundle references few files; this bounds a hostile one.
MAX_DIFF_FILES = 50

#: Longest single diff line kept; a minified blob must not fill the budget.
_MAX_LINE_CHARS = 400

_TRUNCATED = "\n... (diff truncated)"

_DETECTOR = RegexPiiDetector()

FileStatus = Literal["added", "removed", "changed"]


class NodeDiff(TypedDict):
    """Node ids by what happened to them since the last signed version."""

    added: list[str]
    removed: list[str]
    changed: list[str]


class FileDiff(TypedDict):
    """One companion file's status and, when readable, its redacted unified diff."""

    path: str
    status: FileStatus
    diff: str | None


class WorkflowDiff(TypedDict):
    """The approval-card diff. ``baseline_version`` is None when nothing was signed yet."""

    baseline_version: int | None
    nodes: NodeDiff
    files: list[FileDiff]


def diff_against_last_signed(store: DefinitionStore, workflow_id: str) -> WorkflowDiff:
    """Diff the current draft of ``workflow_id`` against its last signed revision."""
    current = store.load(workflow_id)
    baseline = store.last_signed_version(workflow_id)
    old_definition = None if baseline is None else store.load_version(workflow_id, baseline)
    nodes = _node_diff(old_definition, current.definition)
    old_paths = () if old_definition is None else referenced_files(old_definition)
    files = _file_diffs(store, workflow_id, baseline, old_paths, current.definition)
    return {"baseline_version": baseline, "nodes": nodes, "files": files}


def _node_diff(old: WorkflowDefinition | None, new: WorkflowDefinition) -> NodeDiff:
    old_nodes = {} if old is None else {node.id: _dump(node) for node in old.nodes}
    new_nodes = {node.id: _dump(node) for node in new.nodes}
    return {
        "added": sorted(set(new_nodes) - set(old_nodes)),
        "removed": sorted(set(old_nodes) - set(new_nodes)),
        "changed": sorted(
            node_id
            for node_id in set(old_nodes) & set(new_nodes)
            if old_nodes[node_id] != new_nodes[node_id]
        ),
    }


def _dump(node: Any) -> dict[str, Any]:
    return dict(node.model_dump(mode="json", exclude_none=True))


def _file_diffs(
    store: DefinitionStore,
    workflow_id: str,
    baseline: int | None,
    old_paths: tuple[str, ...],
    new_definition: WorkflowDefinition,
) -> list[FileDiff]:
    new_paths = referenced_files(new_definition)
    bundle_root = store.path_for(workflow_id)
    results: list[FileDiff] = []
    for path in sorted(set(old_paths) | set(new_paths)):
        if len(results) >= MAX_DIFF_FILES:
            break
        in_old, in_new = path in old_paths, path in new_paths
        if in_old and not in_new:
            results.append({"path": path, "status": "removed", "diff": None})
            continue
        new_body = store.read_bundle_file(bundle_root, path)
        if not in_old:
            results.append({"path": path, "status": "added", "diff": _unified(None, new_body)})
            continue
        old_body = None if baseline is None else store.retained_file(workflow_id, baseline, path)
        if old_body is not None and old_body == new_body:
            continue
        # Without a retained copy the baseline bytes are unknowable, so the file
        # is reported as changed with no text rather than guessed unchanged.
        diff = None if old_body is None else _unified(old_body, new_body)
        results.append({"path": path, "status": "changed", "diff": diff})
    return results


def _unified(old: bytes | None, new: bytes | None) -> str | None:
    """A redacted, bounded unified diff, or None when a side is not readable text."""
    try:
        old_lines = [] if old is None else old.decode("utf-8").splitlines()
        new_lines = [] if new is None else new.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    body = "\n".join(difflib.unified_diff(old_lines, new_lines, "signed", "draft", lineterm=""))
    redacted = "\n".join(_redact_line(line) for line in body.splitlines())
    if len(redacted) <= MAX_DIFF_CHARS:
        return redacted
    return redacted[:MAX_DIFF_CHARS] + _TRUNCATED


def _redact_line(line: str) -> str:
    clipped = line[:_MAX_LINE_CHARS]
    return redact_text(clipped, _DETECTOR.detect(clipped))


__all__ = [
    "MAX_DIFF_CHARS",
    "MAX_DIFF_FILES",
    "FileDiff",
    "NodeDiff",
    "WorkflowDiff",
    "diff_against_last_signed",
]
