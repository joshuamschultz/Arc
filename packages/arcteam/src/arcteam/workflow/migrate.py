"""One-time bundle migration and the health check that makes breakage visible.

The alpha-2 schema deleted ``NodeBase.join`` (and the loop fields) and forbids
unknown keys. The previous runtime's serializer wrote ``join = "all"`` on every
node of every bundle, so every saved bundle stopped parsing and the workflow
list went silently empty. The parser takes no compatibility shim (that is
deliberate); this module is the explicit, operator-run alternative:

* :func:`check_bundle` / :func:`check_store` classify every bundle as ``ok``,
  ``unsigned``, ``needs_resign`` or ``unreadable``, with a plain-words reason
  and the command that fixes it. A broken bundle is *reported*, never dropped.
* :func:`migrate_store` removes ONLY removed fields whose value equals the one
  behavior that remains (``join = "all"``), refuses anything that carried real
  behavior (``join = "any"``, ``loop_back_to``, ``max_iterations``), validates
  the result with the real parser, and optionally re-signs through the operator
  signer handle. Every write is atomic and the whole operation is idempotent.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from arctrust import Signer
from pydantic import BaseModel, ConfigDict

from arcteam.workflow.errors import WorkflowParseError
from arcteam.workflow.models import parse_definition
from arcteam.workflow.store import (
    DEFINITION_FILE,
    VERSIONS_DIR,
    DefinitionStore,
    atomic_write,
    load_sidecar,
    sign_definition_with_signer,
)

BundleState = Literal["ok", "unsigned", "needs_resign", "unreadable"]
MigrationAction = Literal[
    "unchanged",
    "would_rewrite",
    "rewritten",
    "would_resign",
    "resigned",
    "refused",
]

_REMOVED_ONLY_IF_ALL = "join"
_REMOVED_LOOP_FIELDS = ("loop_back_to", "max_iterations")
_JOIN_ALL_LINE = re.compile(
    r"^[ \t]*join[ \t]*=[ \t]*([\"'])all\1[ \t]*(#.*)?\r?\n?", re.MULTILINE
)
_MIGRATE_FIX = "arc workflow migrate --dry-run, then arc workflow migrate --resign"


class BundleCheck(BaseModel):
    """One bundle's health: what it is, in plain words, and how to fix it."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    state: BundleState
    detail: str = ""
    fix: str = ""

    @property
    def is_failure(self) -> bool:
        """Whether this bundle cannot run as an operator-trusted workflow."""
        return self.state in ("unreadable", "needs_resign")


class MigrationResult(BaseModel):
    """What the migration did (or would do) to one bundle."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    action: MigrationAction
    files: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    reason: str = ""


def _plain_parse_error(exc: Exception) -> str:
    if isinstance(exc, WorkflowParseError):
        return "; ".join(f"{issue.field}: {issue.error}" for issue in exc.issues[:5])
    return f"{type(exc).__name__}: {exc}"


def check_bundle(store: DefinitionStore, workflow_id: str) -> BundleCheck:
    """Parse and verify one bundle; never raises for a bad bundle."""
    try:
        bundle = store.load(workflow_id)
    except Exception as exc:  # reason: the point is to report every failure, not raise
        return BundleCheck(
            workflow_id=workflow_id,
            state="unreadable",
            detail=(
                "this workflow file does not match the current format and cannot be "
                f"read ({_plain_parse_error(exc)})"
            ),
            fix=_MIGRATE_FIX,
        )
    if bundle.is_verified:
        return BundleCheck(workflow_id=workflow_id, state="ok")
    if load_sidecar(bundle.root) is not None:
        return BundleCheck(
            workflow_id=workflow_id,
            state="needs_resign",
            detail=(
                "this workflow's signature no longer matches its contents or the "
                "operator key, so it will not run as signed"
            ),
            fix=f"arc workflow sign {workflow_id}",
        )
    return BundleCheck(
        workflow_id=workflow_id,
        state="unsigned",
        detail="this workflow is an unsigned draft",
        fix=f"arc workflow sign {workflow_id}",
    )


def check_store(store: DefinitionStore) -> tuple[BundleCheck, ...]:
    """Check every bundle, archived ones included."""
    return tuple(
        check_bundle(store, workflow_id) for workflow_id in store.list_ids(include_archived=True)
    )


def _definition_files(bundle_root: Path) -> list[Path]:
    versions = sorted((bundle_root / VERSIONS_DIR).glob("*.toml"))
    return [bundle_root / DEFINITION_FILE, *versions]


def _refusal(document: dict[str, Any]) -> tuple[str, list[str]]:
    """``(reason, nodes_to_strip)``; a non-empty reason means refuse the file."""
    strip: list[str] = []
    for node in document.get("node") or []:
        node_id = str(node.get("id"))
        for loop_field in _REMOVED_LOOP_FIELDS:
            if loop_field in node:
                return f"node {node_id!r} uses {loop_field}, which no longer exists", []
        if _REMOVED_ONLY_IF_ALL in node:
            if node[_REMOVED_ONLY_IF_ALL] != "all":
                return (
                    f"node {node_id!r} uses join = {node[_REMOVED_ONLY_IF_ALL]!r}, which no "
                    f"longer exists and cannot be converted safely",
                    [],
                )
            strip.append(node_id)
    return "", strip


def _rewrite(path: Path) -> tuple[str | None, tuple[str, ...], str]:
    """``(new_text | None, stripped_nodes, refusal_reason)`` for one definition file."""
    text = path.read_text(encoding="utf-8")
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, (), f"{path.name} is not valid TOML ({exc})"
    reason, nodes = _refusal(document)
    if reason:
        return None, (), f"{path.name}: {reason}"
    if not nodes:
        return None, (), ""
    new_text = _JOIN_ALL_LINE.sub("", text)
    try:
        remaining = tomllib.loads(new_text)
        parse_definition(remaining)
    except Exception as exc:  # reason: refuse rather than write a bundle that still fails
        return None, (), f"{path.name}: still not readable after removing join ({exc})"
    return new_text, tuple(nodes), ""


def _plan(bundle_root: Path) -> tuple[dict[Path, str], tuple[str, ...], str]:
    rewrites: dict[Path, str] = {}
    nodes: list[str] = []
    for path in _definition_files(bundle_root):
        new_text, stripped, reason = _rewrite(path)
        if reason:
            return {}, (), reason
        if new_text is not None:
            rewrites[path] = new_text
            nodes.extend(n for n in stripped if n not in nodes)
    return rewrites, tuple(nodes), ""


def migrate_store(
    store: DefinitionStore,
    *,
    dry_run: bool,
    signer: Signer | None = None,
    signer_did: str = "",
) -> tuple[MigrationResult, ...]:
    """Migrate every legacy bundle; with ``signer``, re-sign what it migrated.

    A bundle is re-signed only if it carried a signature before (a bundle that
    was a draft stays a draft; migration never blesses anything). A bundle that
    already parses but whose signature no longer verifies is re-signed too,
    because the canonical form changed with it.
    """
    return tuple(
        _migrate_bundle(store, workflow_id, dry_run=dry_run, signer=signer, signer_did=signer_did)
        for workflow_id in store.list_ids(include_archived=True)
    )


def _migrate_bundle(
    store: DefinitionStore,
    workflow_id: str,
    *,
    dry_run: bool,
    signer: Signer | None,
    signer_did: str,
) -> MigrationResult:
    bundle_root = store.path_for(workflow_id)
    rewrites, nodes, reason = _plan(bundle_root)
    if reason:
        return MigrationResult(workflow_id=workflow_id, action="refused", reason=reason)
    had_signature = load_sidecar(bundle_root) is not None
    files = tuple(str(path.relative_to(bundle_root)) for path in rewrites)

    if rewrites:
        if dry_run:
            return MigrationResult(
                workflow_id=workflow_id, action="would_rewrite", files=files, nodes=nodes
            )
        # Version files first: a crash between writes leaves the current file
        # (the one that matters) still in its old, reported-as-unreadable state.
        for path in sorted(rewrites, key=lambda p: p.name == DEFINITION_FILE):
            atomic_write(path, rewrites[path].encode("utf-8"))
        store.emit_audit(
            "workflow.migrated",
            {
                "workflow_id": workflow_id,
                "actor_did": signer_did or "did:arc:local:operator",
                "files": list(files),
                "removed_fields": ["join"],
                "nodes": list(nodes),
            },
        )
        if signer is not None and had_signature:
            sign_definition_with_signer(store, workflow_id, signer_did=signer_did, signer=signer)
        return MigrationResult(
            workflow_id=workflow_id, action="rewritten", files=files, nodes=nodes
        )

    if check_bundle(store, workflow_id).state != "needs_resign":
        return MigrationResult(workflow_id=workflow_id, action="unchanged")
    if signer is None:
        return MigrationResult(workflow_id=workflow_id, action="would_resign")
    if dry_run:
        return MigrationResult(workflow_id=workflow_id, action="would_resign")
    sign_definition_with_signer(store, workflow_id, signer_did=signer_did, signer=signer)
    return MigrationResult(workflow_id=workflow_id, action="resigned")


__all__ = [
    "BundleCheck",
    "BundleState",
    "MigrationResult",
    "check_bundle",
    "check_store",
    "migrate_store",
]
