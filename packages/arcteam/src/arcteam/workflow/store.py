"""COMP-005/COMP-022 — the definition bundle, the signing gate, archival.

The workspace is the source of truth for a workflow. A bundle is a directory:

    <root>/<workflow_id>/
        workflow.toml            the definition
        workflow.toml.arcsig     detached operator signature over the bundle
        archived.json            present only while archived
        versions/<n>.toml        every prior revision, retained
        schemas/ prompts/ scripts/

Three rules make this component what it is.

**Agents and dashboards author drafts; only the operator signs.** If the
authoring process could also sign, prompt injection could author an
exfiltration pipeline and bless it (LLM06/ASI04). Every write here produces
``status="draft"``. The only path to ``"signed"`` is
:func:`sign_definition`, which demands an operator private key that never
enters an agent process.

**Verification pins the operator key.** A valid signature from any other key
fails. An unpinned gate would accept any self-signed definition, which is the
whole supply-chain hole (LLM03).

**Deletion is archival.** Archiving hides a workflow, disables its trigger, and
refuses new runs while retaining the bundle, every version, and all run history
so past runs stay renderable and the audit chain stays whole. A hard purge is
a separate operator action that refuses while any run still references the
definition, and — when forced — records in the audit chain that history for
that workflow is henceforth unrenderable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from arctrust import ArtifactSignature, sign_artifact, verify_artifact
from pydantic import BaseModel, ConfigDict

from arcteam.types import normalize_channel
from arcteam.workflow.errors import (
    InvalidWorkflowIdError,
    PurgeRefusedError,
    StaleEditError,
    UnsignedWorkflowError,
    ValidationIssue,
    WorkflowArchivedError,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    WorkflowValidationError,
)
from arcteam.workflow.models import (
    WORKFLOW_ID_PATTERN,
    Trigger,
    WorkflowDefinition,
    parse_definition,
)
from arcteam.workflow.serialize import canonical_bytes, content_hash, dump_toml, file_manifest
from arcteam.workflow.validator import KnownReferences, confine, validate_definition

_logger = logging.getLogger("arcteam.workflow.store")

DEFINITION_FILE = "workflow.toml"
SIDECAR_FILE = "workflow.toml.arcsig"
ARCHIVE_MARKER = "archived.json"
VERSIONS_DIR = "versions"

DefinitionStatus = Literal["draft", "signed", "archived"]

WorkflowAuditHook = Callable[[str, dict[str, Any]], None]
"""Where lifecycle events go. The control plane wires the real audit chain; the
store never owns a sink, it only guarantees an event per operation."""

_TIER_RANK: dict[str, int] = {"personal": 0, "enterprise": 1, "federal": 2}

_LEGAL_ID = re.compile(WORKFLOW_ID_PATTERN)


class WorkflowBundle(BaseModel):
    """A definition as it exists on disk, with its trust status attached.

    Status lives here rather than on :class:`WorkflowDefinition` on purpose: it
    is a property of the bundle and its signature, so there is structurally no
    way for parsing or validating a document to confer it (REQ-223).
    """

    model_config = ConfigDict(frozen=True)

    definition: WorkflowDefinition
    status: DefinitionStatus
    content_hash: str
    manifest: dict[str, str]
    root: Path
    signer_did: str | None = None

    @property
    def is_verified(self) -> bool:
        """Whether the pinned operator signature verified over this bundle.

        Ask this, not ``status``, whenever the question is *trust*. ``status``
        answers a different question — it folds lifecycle and trust into one
        render value, so an archived bundle reads ``"archived"`` even when its
        signature is perfectly good. Reading trust off ``status`` has already
        produced two defects in this feature, both in security checks, so the
        safe read is the named one.
        """
        return self.signer_did is not None

    @property
    def effective_trigger(self) -> Trigger | None:
        """The trigger the scheduler should honour — ``None`` while archived."""
        return None if self.status == "archived" else self.definition.trigger


class VersionRecord(BaseModel):
    """One row of a workflow's version history: who signed it and when.

    ``signer_did``/``signed_at`` are ``None`` for a version that was never
    signed, or one retained before the sidecar-retention fix landed — reported
    honestly, never as a fabricated "unsigned draft" for a version the operator
    actually signed.
    """

    model_config = ConfigDict(frozen=True)

    version: int
    signer_did: str | None = None
    signed_at: str | None = None


class DefinitionStore:
    """Load, verify, version, and archive workflow bundles under one root.

    Args:
        root: The ``workflows/`` directory inside the owning agent's workspace.
        tier: Deployment stringency, received at construction so audit events
            reflect the true posture rather than a per-call guess.
        operator_public_key: The key every signature is pinned against. Above
            personal tier its absence is a hard refusal, never a fallback to
            trusting whatever key signed.
        audit: Hook receiving ``(event, payload)`` for every operation.
    """

    def __init__(
        self,
        root: Path,
        *,
        tier: str = "personal",
        operator_public_key: bytes | None = None,
        audit: WorkflowAuditHook | None = None,
    ) -> None:
        self.root = root
        self.tier = str(tier).lower()
        self._operator_public_key = operator_public_key
        self._audit = audit

    # -- reading --

    def path_for(self, workflow_id: str) -> Path:
        """The bundle directory for ``workflow_id``.

        Every read and lifecycle method routes through here, which makes this
        the one place a caller-supplied id becomes a path — and therefore the
        one place it must be proved to be a bare name.
        """
        if not _LEGAL_ID.match(workflow_id):
            raise InvalidWorkflowIdError(
                f"{workflow_id!r} is not a workflow id; an id is a bare name matching "
                f"{WORKFLOW_ID_PATTERN} and may never contain a path separator or '..' "
                f"(fail-closed — an id becomes a directory under the agent's workspace)"
            )
        return self.root / workflow_id

    def exists(self, workflow_id: str) -> bool:
        """True when a bundle with a definition file is present."""
        return (self.path_for(workflow_id) / DEFINITION_FILE).is_file()

    def list_ids(self, *, include_archived: bool = False) -> tuple[str, ...]:
        """Every workflow id under the root; archived ones are hidden by default.

        A directory is not automatically an id: a name this store could not
        resolve is skipped rather than listed, so everything returned here is
        loadable. Otherwise a stray directory becomes a row a dashboard renders
        and then fails to open.
        """
        if not self.root.is_dir():
            return ()
        found = [
            path.name
            for path in sorted(self.root.iterdir())
            if _LEGAL_ID.match(path.name)
            and (path / DEFINITION_FILE).is_file()
            and (include_archived or not (path / ARCHIVE_MARKER).exists())
        ]
        return tuple(found)

    def load(self, workflow_id: str) -> WorkflowBundle:
        """Read and verify a bundle. Never raises for an unsigned definition."""
        bundle_root = self._require(workflow_id)
        definition = parse_definition(tomllib.loads(self._definition_text(bundle_root)))
        manifest = file_manifest(definition, bundle_root)
        digest = content_hash(definition, manifest)
        signature = self._pinned_signature(bundle_root, definition, manifest)
        status: DefinitionStatus = (
            "archived"
            if self._is_archived(bundle_root)
            else ("signed" if signature is not None else "draft")
        )
        return WorkflowBundle(
            definition=definition,
            status=status,
            content_hash=digest,
            manifest=manifest,
            root=bundle_root,
            signer_did=signature.signer_did if signature is not None else None,
        )

    def load_for_dispatch(self, workflow_id: str) -> WorkflowBundle:
        """Load a bundle to execute one node of an already-admitted run.

        Applies the two gates that must hold on every single dispatch: the
        bundle still matches its signature, and its trust level clears the
        tier. It deliberately does **not** ask whether the workflow is
        archived — that is an admission question, answered once when a run
        starts (:meth:`load_for_run`). Archiving refuses new runs; it does not
        reach into runs already in flight and strand them mid-graph.

        Raises:
            WorkflowIntegrityError: a signature exists but the bundle has drifted
                under it — refuse rather than execute a hybrid of two versions.
            UnsignedWorkflowError: unsigned or foreign-signed above personal tier.
        """
        bundle = self.load(workflow_id)
        self._assert_no_drift(bundle)
        if bundle.is_verified:
            return bundle
        if _TIER_RANK.get(self.tier, 0) > 0:
            raise UnsignedWorkflowError(
                f"workflow {workflow_id!r} is unsigned or not signed by the deployment "
                f"operator key; refusing to run it at the {self.tier} tier (fail-closed, "
                f"LLM03/ASI04)"
            )
        _logger.warning("workflow %r running unsigned at the personal tier", workflow_id)
        self.emit_audit(
            "workflow.unsigned_run_permitted",
            {"workflow_id": workflow_id, "tier": self.tier, "version": bundle.definition.version},
        )
        return bundle

    def load_for_run(self, workflow_id: str) -> WorkflowBundle:
        """Load a bundle to **start** a run: admission, then the dispatch gates.

        Raises:
            WorkflowArchivedError: the definition is archived and accepts no new runs.
            WorkflowIntegrityError: see :meth:`load_for_dispatch`.
            UnsignedWorkflowError: see :meth:`load_for_dispatch`.
        """
        if self._is_archived(self._require(workflow_id)):
            raise WorkflowArchivedError(
                f"workflow {workflow_id!r} is archived and accepts no new runs; unarchive it "
                f"first (runs already in flight are unaffected and keep dispatching)"
            )
        return self.load_for_dispatch(workflow_id)

    def versions(self, workflow_id: str) -> tuple[int, ...]:
        """Retained prior version numbers, ascending."""
        directory = self.path_for(workflow_id) / VERSIONS_DIR
        if not directory.is_dir():
            return ()
        return tuple(sorted(int(path.stem) for path in directory.glob("*.toml")))

    def load_version(self, workflow_id: str, version: int) -> WorkflowDefinition:
        """Read a retained prior revision, for diffing and history rendering."""
        path = self.path_for(workflow_id) / VERSIONS_DIR / f"{version}.toml"
        if not path.is_file():
            raise WorkflowNotFoundError(f"workflow {workflow_id!r} has no retained v{version}")
        return parse_definition(tomllib.loads(path.read_text(encoding="utf-8")))

    # -- writing --

    def save_draft(
        self,
        definition: WorkflowDefinition,
        *,
        actor_did: str,
        expected_version: int | None,
        files: Mapping[str, bytes] | None = None,
        known: KnownReferences | None = None,
    ) -> WorkflowBundle:
        """Validate the whole graph, then write it as an unsigned draft.

        Every edit is a transaction: validate, write atomically, bump the
        version, retain the prior revision, audit. ``expected_version`` is
        optimistic concurrency — a stale edit is refused, never merged.

        Raises:
            WorkflowValidationError: the graph did not validate; nothing written.
            StaleEditError: ``expected_version`` does not match the stored one.
            WorkflowNotFoundError: an expected version was given but no bundle exists.
            WorkflowArchivedError: the bundle is archived.
        """
        bundle_root = self.path_for(definition.id)
        version = self._next_version(definition.id, expected_version)
        incoming = self._resolve_files(bundle_root, files or {})

        # A bare channel name is promoted to channel://<name> on the way to
        # disk, so the one narration binding that reaches the runner is always a
        # valid messaging URI — a run must never be refused because an authoring
        # surface stored "work" where a run expects "channel://work".
        pending = definition.model_copy(
            update={"version": version, "channel": normalize_channel(definition.channel)}
        )
        text = dump_toml(pending.to_document())
        issues = validate_definition(
            pending,
            known=known,
            bundle_root=bundle_root,
            raw_size_bytes=len(text.encode("utf-8")),
            pending_files=frozenset(files or ()),
        )
        if issues:
            raise WorkflowValidationError(issues)

        for target, body in incoming.items():
            _atomic_write(target, body)
        self._retain_current(bundle_root)
        _atomic_write(bundle_root / DEFINITION_FILE, text.encode("utf-8"))
        (bundle_root / SIDECAR_FILE).unlink(missing_ok=True)

        bundle = self.load(definition.id)
        self.emit_audit(
            "workflow.created" if version == 1 else "workflow.edited",
            {
                "workflow_id": definition.id,
                "actor_did": actor_did,
                "version": version,
                "content_hash": bundle.content_hash,
                "status": bundle.status,
            },
        )
        return bundle

    # -- archival (COMP-022) --

    def archive(self, workflow_id: str, *, actor_did: str, reason: str = "") -> WorkflowBundle:
        """Hide the workflow, disable its trigger, refuse new runs — retain everything."""
        bundle_root = self._require(workflow_id)
        (bundle_root / ARCHIVE_MARKER).write_text(
            json.dumps(
                {
                    "archived_at": datetime.now(UTC).isoformat(),
                    "actor_did": actor_did,
                    "reason": reason,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.emit_audit(
            "workflow.archived",
            {"workflow_id": workflow_id, "actor_did": actor_did, "reason": reason},
        )
        return self.load(workflow_id)

    def unarchive(self, workflow_id: str, *, actor_did: str) -> WorkflowBundle:
        """Restore the workflow as a draft.

        The signature is dropped: a definition that left the active set returns
        for the operator to look at and re-sign, never silently re-armed.
        """
        bundle_root = self._require(workflow_id)
        (bundle_root / ARCHIVE_MARKER).unlink(missing_ok=True)
        (bundle_root / SIDECAR_FILE).unlink(missing_ok=True)
        self.emit_audit(
            "workflow.unarchived", {"workflow_id": workflow_id, "actor_did": actor_did}
        )
        return self.load(workflow_id)

    def purge(
        self,
        workflow_id: str,
        *,
        actor_did: str,
        runs_referencing: Callable[[str], int],
        force: bool = False,
        reason: str = "",
    ) -> None:
        """Permanently destroy a bundle. Refused while any run references it.

        Args:
            runs_referencing: Returns how many runs still reference the
                workflow. Injected rather than imported so this layer keeps no
                dependency on the run store.
            force: Destroy anyway, orphaning those runs' history. The event is
                always emitted; when nothing is wired to receive it the loss of
                recording is warned about, because the store cannot know whether
                its caller audits — the control plane does, and does not use
                this hook.
        """
        self._require(workflow_id)
        outstanding = runs_referencing(workflow_id)
        if outstanding and not force:
            raise PurgeRefusedError(
                f"refusing to purge workflow {workflow_id!r}: {outstanding} run(s) still "
                f"reference it and their history would become unrenderable; archive it instead"
            )
        if force and self._audit is None:
            _logger.warning(
                "forced purge of workflow %r with no audit hook wired on this store; "
                "the caller must record that its history is henceforth unrenderable (AU-9)",
                workflow_id,
            )
        self.emit_audit(
            "workflow.purged",
            {
                "workflow_id": workflow_id,
                "actor_did": actor_did,
                "reason": reason,
                "forced": force,
                "orphaned_runs": outstanding,
                "history_unrenderable": True,
            },
        )
        shutil.rmtree(self.path_for(workflow_id))

    # -- internals --

    def _require(self, workflow_id: str) -> Path:
        bundle_root = self.path_for(workflow_id)
        if not (bundle_root / DEFINITION_FILE).is_file():
            raise WorkflowNotFoundError(f"no workflow bundle at {bundle_root}")
        return bundle_root

    def _definition_text(self, bundle_root: Path) -> str:
        return (bundle_root / DEFINITION_FILE).read_text(encoding="utf-8")

    def _is_archived(self, bundle_root: Path) -> bool:
        return (bundle_root / ARCHIVE_MARKER).exists()

    def _pinned_signature(
        self, bundle_root: Path, definition: WorkflowDefinition, manifest: Mapping[str, str]
    ) -> ArtifactSignature | None:
        """The sidecar, but only when it verifies against the pinned operator key.

        With no key pinned there is nothing to verify *against*, so the answer
        is "unsigned" at every tier — never "signed by whoever happened to sign
        it". Accepting the sidecar's own embedded key would be trust-on-first-
        use (LLM03) and would let an agent self-sign the workflow it authored,
        which is the precise composition the draft-then-operator-sign lifecycle
        exists to prevent (LLM06/ASI04). Personal tier still runs the definition
        — it simply runs it as the draft it is.
        """
        sidecar = load_sidecar(bundle_root)
        if sidecar is None or self._operator_public_key is None:
            return None
        content = canonical_bytes(definition, manifest)
        verified = verify_artifact(content, sidecar, trusted_public_key=self._operator_public_key)
        return sidecar if verified else None

    def _assert_no_drift(self, bundle: WorkflowBundle) -> None:
        """A sidecar over different bytes means the bundle changed under a signature."""
        sidecar = load_sidecar(bundle.root)
        if sidecar is not None and sidecar.artifact_sha256 != bundle.content_hash:
            raise WorkflowIntegrityError(
                f"workflow {bundle.definition.id!r} no longer matches its signature "
                f"(signed {sidecar.artifact_sha256}, found {bundle.content_hash}); refusing to "
                f"execute a hybrid of two versions"
            )

    def _next_version(self, workflow_id: str, expected_version: int | None) -> int:
        if not self.exists(workflow_id):
            if expected_version is not None:
                raise WorkflowNotFoundError(
                    f"no workflow {workflow_id!r} to edit; omit expected_version to create it"
                )
            return 1
        if self._is_archived(self.path_for(workflow_id)):
            raise WorkflowArchivedError(
                f"workflow {workflow_id!r} is archived; unarchive it before editing"
            )
        current = self.load(workflow_id).definition.version
        if expected_version != current:
            raise StaleEditError(
                f"workflow {workflow_id!r} is at version {current} but the edit expected "
                f"{expected_version}; re-read it and re-apply the change"
            )
        return current + 1

    def _resolve_files(self, bundle_root: Path, files: Mapping[str, bytes]) -> dict[Path, bytes]:
        """Confine every companion-file path, writing nothing.

        Resolution is separated from writing so that a save which is going to
        be refused touches no bytes at all. Writing first and validating after
        meant a *rejected* edit still moved a signed bundle's manifest hash,
        breaking the operator's signature and taking a live workflow out of
        service — a failed call must never be able to do that.
        """
        resolved: dict[Path, bytes] = {}
        for reference, body in files.items():
            target = confine(bundle_root.resolve(), reference)
            if target is None:
                raise WorkflowValidationError((_escape_issue(reference),))
            resolved[target] = body
        return resolved

    def _retain_current(self, bundle_root: Path) -> None:
        """Copy the current definition into ``versions/`` before overwriting it.

        The signature sidecar is retained alongside it, so the version history
        can truthfully show who signed each superseded revision instead of
        reporting every one as an unsigned draft the moment it is superseded.
        """
        current = bundle_root / DEFINITION_FILE
        if not current.is_file():
            return
        definition = parse_definition(tomllib.loads(current.read_text(encoding="utf-8")))
        archive = bundle_root / VERSIONS_DIR
        archive.mkdir(exist_ok=True)
        _atomic_write(archive / f"{definition.version}.toml", current.read_bytes())
        sidecar = bundle_root / SIDECAR_FILE
        if sidecar.is_file():
            _atomic_write(archive / f"{definition.version}.arcsig", sidecar.read_bytes())

    def version_history(self, workflow_id: str) -> tuple[VersionRecord, ...]:
        """Every version's signer + signed-at, ascending, current version last.

        Prior versions read their retained sidecar; the current version reads
        the live one. A version with no retained sidecar (unsigned, or retained
        before sidecar retention existed) reports ``None`` — not a fabricated
        draft.
        """
        root = self.path_for(workflow_id)
        records: list[VersionRecord] = []
        vdir = root / VERSIONS_DIR
        if vdir.is_dir():
            for path in sorted(vdir.glob("*.toml"), key=lambda p: int(p.stem)):
                version = int(path.stem)
                sig = _read_signature(vdir / f"{version}.arcsig")
                records.append(
                    VersionRecord(
                        version=version,
                        signer_did=sig.signer_did if sig else None,
                        signed_at=sig.signed_at if sig else None,
                    )
                )
        current = self.load(workflow_id)
        live = load_sidecar(root)
        records.append(
            VersionRecord(
                version=current.definition.version,
                signer_did=current.signer_did,
                signed_at=live.signed_at if live else None,
            )
        )
        return tuple(records)

    def emit_audit(self, event: str, payload: dict[str, Any]) -> None:
        """Emit one lifecycle event through the wired hook, if any."""
        if self._audit is not None:
            self._audit(event, payload)


def sign_definition(
    store: DefinitionStore, workflow_id: str, *, signer_did: str, private_key: bytes
) -> WorkflowBundle:
    """Sign a bundle out of band. The only path to ``status="signed"``.

    Called by the operator command line, never from an agent process — the
    signing key must not exist inside a process a model can influence. The
    signature covers :func:`canonical_bytes`, which spans the canonical
    definition *and* the manifest of every referenced file, so editing a prompt
    or a script invalidates it just as editing the graph does.
    """
    bundle = store.load(workflow_id)
    signature = sign_artifact(
        canonical_bytes(bundle.definition, bundle.manifest),
        signer_did=signer_did,
        private_key=private_key,
    )
    _atomic_write(bundle.root / SIDECAR_FILE, signature.to_json().encode("utf-8"))
    signed = store.load(workflow_id)
    store.emit_audit(
        "workflow.signed",
        {
            "workflow_id": workflow_id,
            "actor_did": signer_did,
            "version": signed.definition.version,
            "content_hash": signed.content_hash,
            "status": signed.status,
        },
    )
    return signed


def _read_signature(path: Path) -> ArtifactSignature | None:
    """Read one detached signature file, or ``None`` if absent or corrupt.

    A corrupt or forged sidecar is treated as unsigned rather than as an error,
    which keeps the failure mode fail-closed instead of denial-of-service.
    """
    if not path.is_file():
        return None
    try:
        return ArtifactSignature.from_json(path.read_text(encoding="utf-8"))
    except Exception:  # reason: any unreadable sidecar counts as unsigned (fail-closed)
        _logger.warning("unreadable workflow signature sidecar at %s; treating as unsigned", path)
        return None


def load_sidecar(bundle_root: Path) -> ArtifactSignature | None:
    """Read a bundle's live detached signature, or ``None`` if absent/corrupt."""
    return _read_signature(bundle_root / SIDECAR_FILE)


def _escape_issue(reference: str) -> ValidationIssue:
    return ValidationIssue(
        field="files",
        error="a bundle file path must be relative and must stay inside the bundle",
        observed=reference,
        admissible=("a relative path inside the workflow bundle",),
    )


def _atomic_write(path: Path, body: bytes) -> None:
    """Write via temp file, fsync, rename — a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = [
    "ARCHIVE_MARKER",
    "DEFINITION_FILE",
    "SIDECAR_FILE",
    "VERSIONS_DIR",
    "DefinitionStatus",
    "DefinitionStore",
    "VersionRecord",
    "WorkflowAuditHook",
    "WorkflowBundle",
    "canonical_bytes",
    "content_hash",
    "file_manifest",
    "load_sidecar",
    "sign_definition",
]
