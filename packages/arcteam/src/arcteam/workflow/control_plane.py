"""WorkflowControlPlane — one operation set, three surfaces (SPEC-061 COMP-021).

An agent's builder tools, the operator command line, and the dashboard are all
*callers*. There is exactly one implementation of what "create a workflow" or
"start a run" means, and it lives here, so the three surfaces cannot drift.
Validation, versioning, draft lifecycle, and audit emission happen once, on the
way through, for every caller.

Two rules the operations enforce on everybody equally:

* **Authoring never confers trust.** Every mutation lands as ``draft``. Signing
  is an out-of-band operator action with a key that never enters this process,
  so no amount of successful validation can produce a signed definition
  (REQ-223).
* **Every operation names its actor.** The acting identity is a required
  argument, not a default, and each operation emits exactly one audit event
  carrying that identity and the deployment tier taken at construction.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from .runner import WorkflowRunner
from .runner_contracts import (
    BundleSpec,
    DefinitionParser,
    DefinitionStoreLike,
    DefinitionValidator,
    RunRecord,
    RunStoreLike,
    Tier,
    ValidationIssueLike,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationIssue:
    """A repairable failure, addressed to a field of a node where possible."""

    node_id: str | None
    field: str | None
    error: str
    observed: Any = None
    admissible: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlPlaneResult:
    """What every operation returns: the outcome, or the list to repair."""

    ok: bool
    errors: tuple[ValidationIssueLike | OperationIssue, ...] = ()
    bundle: BundleSpec | None = None
    run: RunRecord | None = None


@dataclass
class _Operation:
    """The audit shape shared by every operation."""

    action: str
    target: str
    outcome: str = "ok"
    extra: dict[str, Any] = field(default_factory=dict)


class WorkflowControlPlane:
    """The seven operations every workflow surface delegates to."""

    def __init__(
        self,
        *,
        definitions: DefinitionStoreLike,
        parse: DefinitionParser,
        validate: DefinitionValidator,
        runner: WorkflowRunner,
        runs: RunStoreLike,
        tier: Tier,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._definitions = definitions
        self._parse = parse
        self._validate = validate
        self._runner = runner
        # The purge guard needs a run count, and this is the one component that
        # holds both the definition store and the run store.
        self._runs = runs
        self._tier: Tier = tier
        self._sink: AuditSink = audit_sink or NullSink()

    # -- authoring ----------------------------------------------------------

    async def create(
        self,
        document: Mapping[str, Any],
        *,
        actor_did: str,
        files: Mapping[str, bytes] | None = None,
    ) -> ControlPlaneResult:
        """Parse, validate, and write a new definition as a draft.

        ``files`` carries the companion prompts and schemas a node references.
        They travel WITH the definition rather than being written by the caller
        beforehand, so a surface never has to reach past this operation to
        author a complete workflow — the moment one does, the single-operation-
        set guarantee is gone.
        """
        return await self._write(
            document,
            actor_did=actor_did,
            expected_version=None,
            action="workflow.created",
            reason="created",
            files=files,
        )

    async def edit(
        self,
        workflow_id: str,
        document: Mapping[str, Any],
        *,
        expected_version: int,
        actor_did: str,
        reason: str,
        files: Mapping[str, bytes] | None = None,
    ) -> ControlPlaneResult:
        """Revise a definition against the version the editor actually saw.

        A stale expected version is refused, never merged: two editors racing
        one workflow must not silently blend their graphs.
        """
        return await self._write(
            document,
            actor_did=actor_did,
            expected_version=expected_version,
            action="workflow.edited",
            reason=reason,
            workflow_id=workflow_id,
            files=files,
        )

    async def archive(self, workflow_id: str, *, actor_did: str) -> ControlPlaneResult:
        """Hide it, disable its trigger, refuse new runs — but erase nothing."""
        return await self._store_call(
            lambda: self._definitions.archive(workflow_id, actor_did=actor_did),
            action="workflow.archived",
            target=workflow_id,
            actor_did=actor_did,
        )

    async def unarchive(self, workflow_id: str, *, actor_did: str) -> ControlPlaneResult:
        """Restore an archived workflow — as a draft, never as signed."""
        return await self._store_call(
            lambda: self._definitions.unarchive(workflow_id, actor_did=actor_did),
            action="workflow.unarchived",
            target=workflow_id,
            actor_did=actor_did,
        )

    async def purge(
        self,
        workflow_id: str,
        *,
        actor_did: str,
        force: bool = False,
        reason: str = "",
    ) -> ControlPlaneResult:
        """Destroy a definition permanently. Refused while any run references it.

        Archive is the ordinary path and it erases nothing; this is the separate
        operator-only action (REQ-256). The definition store deliberately holds
        no dependency on the run store, so the count comes from here — this is
        the one place that holds both halves, which is why the operation lives
        here rather than in either store.

        A forced purge is permitted but never quiet: past runs of this workflow
        become unrenderable, and that fact is what the audit chain must carry.
        """
        try:
            outstanding = await self._runs.count_runs_for_workflow(workflow_id)
        except Exception as exc:
            self._emit(
                _Operation("workflow.purged", workflow_id, "error", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(OperationIssue(None, None, str(exc)),))

        try:
            self._definitions.purge(
                workflow_id,
                actor_did=actor_did,
                runs_referencing=lambda _: outstanding,
                force=force,
                reason=reason,
            )
        except Exception as exc:
            self._emit(
                _Operation("workflow.purged", workflow_id, "refused", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(OperationIssue(None, None, str(exc)),))

        self._emit(
            _Operation(
                "workflow.purged",
                workflow_id,
                "ok",
                {
                    "forced": force,
                    "runs_orphaned": outstanding if force else 0,
                    "reason": reason,
                },
            ),
            actor_did,
        )
        return ControlPlaneResult(ok=True)

    # -- initiation ---------------------------------------------------------

    async def run(
        self,
        workflow_id: str,
        *,
        input: Mapping[str, Any],  # noqa: A002 — the definition's own vocabulary
        actor_did: str,
    ) -> ControlPlaneResult:
        """Start a run. The dashboard, the CLI, and an agent all land here."""
        try:
            record = await self._runner.start_run(
                workflow_id, input=input, initiator_did=actor_did
            )
        except Exception as exc:
            self._emit(
                _Operation("workflow.run.started", workflow_id, "refused", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(
                ok=False, errors=(OperationIssue(None, None, str(exc)),)
            )
        self._emit(
            _Operation(
                "workflow.run.started",
                f"{workflow_id}/{record.run_id}",
                "started",
                {"run_id": record.run_id},
            ),
            actor_did,
        )
        return ControlPlaneResult(ok=True, run=record)

    async def cancel(
        self, run_id: str, *, actor_did: str, reason: str = "cancelled by operator"
    ) -> ControlPlaneResult:
        """Stop a run. Ordering (Run first, then nodes) is the runner's job."""
        try:
            record = await self._runner.cancel(run_id, actor_did=actor_did, reason=reason)
        except Exception as exc:
            self._emit(
                _Operation("workflow.run.cancelled", run_id, "error", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(
                ok=False, errors=(OperationIssue(None, None, str(exc)),)
            )
        self._emit(
            _Operation("workflow.run.cancelled", run_id, record.status, {"reason": reason}),
            actor_did,
        )
        return ControlPlaneResult(ok=True, run=record)

    # -- internals ----------------------------------------------------------

    async def _write(
        self,
        document: Mapping[str, Any],
        *,
        actor_did: str,
        expected_version: int | None,
        action: str,
        reason: str,
        workflow_id: str | None = None,
        files: Mapping[str, bytes] | None = None,
    ) -> ControlPlaneResult:
        """The one path a definition takes to disk: parse, validate, save draft."""
        target = workflow_id or str(document.get("id", "<unnamed>"))
        try:
            definition = self._parse(document)
        except Exception as exc:
            issues = (OperationIssue(None, None, f"unparseable definition: {exc}"),)
            self._emit(_Operation(action, target, "invalid", {"errors": 1}), actor_did)
            return ControlPlaneResult(ok=False, errors=issues)

        # Files arriving with the edit count as present for validation, so a
        # node referencing a prompt written in this same call validates — and
        # still validates BEFORE the store commits any of those bytes.
        problems = tuple(
            self._validate(definition, pending_files=frozenset(files or ()))
        )
        if problems:
            self._emit(
                _Operation(action, target, "invalid", {"errors": len(problems)}), actor_did
            )
            return ControlPlaneResult(ok=False, errors=problems)

        try:
            bundle = self._definitions.save_draft(
                definition,
                actor_did=actor_did,
                expected_version=expected_version,
                files=files,
            )
        except Exception as exc:
            # The store validates again on its own and refuses a stale expected
            # version. Its typed issues, when it has them, are what a surface can
            # actually repair — a version conflict is the caller's to re-read.
            store_issues = tuple(getattr(exc, "issues", ()))
            if store_issues:
                self._emit(
                    _Operation(action, target, "invalid", {"errors": len(store_issues)}),
                    actor_did,
                )
                return ControlPlaneResult(ok=False, errors=store_issues)
            outcome = "conflict" if expected_version is not None else "error"
            self._emit(_Operation(action, target, outcome, {"error": str(exc)}), actor_did)
            return ControlPlaneResult(
                ok=False,
                errors=(OperationIssue(None, "version", f"stale edit: {exc}"),),
            )

        self._emit(
            _Operation(
                action,
                target,
                "ok",
                {"version": bundle.definition.version, "status": bundle.status, "reason": reason},
            ),
            actor_did,
        )
        return ControlPlaneResult(ok=True, bundle=bundle)

    async def _store_call(
        self,
        operation: Any,
        *,
        action: str,
        target: str,
        actor_did: str,
    ) -> ControlPlaneResult:
        try:
            bundle: BundleSpec = operation()
        except Exception as exc:
            self._emit(_Operation(action, target, "error", {"error": str(exc)}), actor_did)
            return ControlPlaneResult(
                ok=False, errors=(OperationIssue(None, None, str(exc)),)
            )
        self._emit(_Operation(action, target, "ok", {"status": bundle.status}), actor_did)
        return ControlPlaneResult(ok=True, bundle=bundle)

    def _emit(self, operation: _Operation, actor_did: str) -> None:
        emit(
            AuditEvent(
                actor_did=actor_did,
                action=operation.action,
                target=operation.target,
                outcome=operation.outcome,
                tier=self._tier,
                extra=operation.extra,
            ),
            self._sink,
        )


__all__ = [
    "ControlPlaneResult",
    "OperationIssue",
    "WorkflowControlPlane",
]
