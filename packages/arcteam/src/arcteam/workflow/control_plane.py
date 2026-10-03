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

from .errors import GateNotAuthorizedError, PlaceholderOwnerError, WorkflowError
from .runner import TEST_RUN_MAX_COST_USD, NodeRetryRefusedError, WorkflowRunner
from .runner_contracts import (
    BundleSpec,
    DefinitionParser,
    DefinitionStoreLike,
    DefinitionValidator,
    Initiator,
    RunRecord,
    RunStoreLike,
    Tier,
    ValidationIssueLike,
)
from .templates import load_template
from .validator import approver_roles

logger = logging.getLogger(__name__)

#: What a reviewer may choose, mapped to what the runner reads off the row.
_GATE_DECISIONS: dict[str, str] = {
    "approve": "approved",
    "fail_run": "rejected",
    "return_for_revision": "returned_for_revision",
}

#: The role an AUTHENTICATED operator surface (the dashboard's operator session,
#: the CLI holding the operator key) asserts. Operator decides every gate.
OPERATOR_ROLE = "operator"

#: The words a human reviewer uses (CLI, chat card) mapped to the decision the
#: control plane takes. One table, so every caller means the same thing by "reject".
GATE_WORDS: dict[str, str] = {
    "approve": "approve",
    "reject": "fail_run",
    "revise": "return_for_revision",
}


@dataclass(frozen=True)
class OperationIssue:
    """A repairable failure, addressed to a field of a node where possible."""

    node_id: str | None
    field: str | None
    error: str
    observed: Any = None
    admissible: tuple[str, ...] = ()
    #: Command-line wording for the fix. A terminal may print it; a page never does.
    cli_hint: str = ""


def _refusal_issue(exc: Exception) -> OperationIssue:
    """A refused run as an issue, carrying the command hint the error holds (if any)."""
    hint = exc.cli_hint if isinstance(exc, PlaceholderOwnerError) else ""
    return OperationIssue(None, None, str(exc), cli_hint=hint)


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

    @property
    def runner(self) -> WorkflowRunner:
        """The runner this control plane starts and cancels runs through."""
        return self._runner

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

    async def create_from_template(
        self,
        template: str,
        workflow_id: str,
        *,
        actor_did: str,
        owner: str | None = None,
    ) -> ControlPlaneResult:
        """Start a new draft from a shipped starter template (J3 F6, G5).

        Goes through :meth:`create`, so a template is validated, versioned,
        audited and left unsigned exactly like any other authored definition —
        a template confers no trust, it only saves the blank page.
        """
        try:
            document, files = load_template(template, workflow_id=workflow_id, owner=owner)
        except WorkflowError as exc:
            self._emit(
                _Operation("workflow.created", workflow_id, "invalid", {"template": template}),
                actor_did,
            )
            return ControlPlaneResult(
                ok=False, errors=(OperationIssue(None, "template", str(exc)),)
            )
        return await self._write(
            document,
            actor_did=actor_did,
            expected_version=None,
            action="workflow.created",
            reason=f"from template {template}",
            files=files,
        )

    # -- initiation ---------------------------------------------------------

    async def test_run(self, workflow_id: str, *, actor_did: str) -> ControlPlaneResult:
        """Try a draft at any tier without trusting it (J3 F6, G8).

        The one unsigned run allowed above personal tier, and only in test mode:
        the run id is in the reserved test namespace, every node row is flagged
        so the executing agent stubs state-modifying tools with a recorded echo,
        agent nodes run under a small cost cap, and nothing a schedule or an agent
        does can start it. Audited as ``workflow.test_run``. Callers are
        operator-authenticated surfaces; this method does not itself check a role.
        """
        try:
            record = await self._runner.start_run(
                workflow_id,
                input={},
                initiator="operator",
                initiator_did=actor_did,
                mode="test",
            )
        except Exception as exc:
            self._emit(
                _Operation("workflow.test_run", workflow_id, "refused", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(_refusal_issue(exc),))
        self._emit(
            _Operation(
                "workflow.test_run",
                f"{workflow_id}/{record.run_id}",
                "started",
                {"run_id": record.run_id, "max_cost_usd": TEST_RUN_MAX_COST_USD},
            ),
            actor_did,
        )
        return ControlPlaneResult(ok=True, run=record)

    async def run(
        self,
        workflow_id: str,
        *,
        input: Mapping[str, Any],  # noqa: A002 — the definition's own vocabulary
        initiator: Initiator,
        actor_did: str,
        run_id: str | None = None,
        trigger_digest: str | None = None,
        detached: bool = False,
    ) -> ControlPlaneResult:
        """Start a run. The dashboard, the CLI, an agent and a schedule all land here;
        ``initiator`` names which, and gates unsigned drafts.

        ``detached=True`` creates the run without the singleton lease; the
        lease-holding service runner advances it on its next tick.
        """
        try:
            record = await self._runner.start_run(
                workflow_id,
                input=input,
                initiator=initiator,
                initiator_did=actor_did,
                run_id=run_id,
                trigger_digest=trigger_digest,
                detached=detached,
            )
        except Exception as exc:
            self._emit(
                _Operation("workflow.run.started", workflow_id, "refused", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(_refusal_issue(exc),))
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

    async def resolve_gate(
        self,
        task_id: str,
        *,
        decision: str,
        notes: str = "",
        actor_did: str,
        actor_roles: frozenset[str],
    ) -> ControlPlaneResult:
        """Resolve a waiting gate: approve, fail the run, or return for revision.

        Only an approver decides (alpha-2 #67): the operator, a DID the gate's
        signed ``approvers`` lists, or a holder of a listed ``role:<name>``.
        ``actor_roles`` is what the CALLER established from an authenticated
        identity — the operator session, or the team registry's record for a
        paired chat user — never anything the decider typed. Anyone else gets
        :class:`GateNotAuthorizedError` and an audited ``workflow.gate.denied``.
        A gate decides once: resolving a settled gate again is refused, so a
        replayed approval cannot ride on the original.

        The reviewer's three outcomes are not a task approve/reject (REQ-247):
        returning for revision sends the reviewed work back to whoever produced
        it and the run continues, which no task status can express. So the
        decision is WRITTEN ON THE ROW and the runner acts on it — the row's
        status carries only whether the gate is settled.

        Gate resolution exists only here (REQ-246). No agent-callable tool
        reaches it; the dashboard route, the CLI, and the channel card are all
        callers of this one method, and each one names the deciding human.
        """
        if decision not in _GATE_DECISIONS:
            return ControlPlaneResult(
                ok=False,
                errors=(
                    OperationIssue(
                        None,
                        "decision",
                        f"unknown gate decision {decision!r}",
                        decision,
                        tuple(_GATE_DECISIONS),
                    ),
                ),
            )
        tasks = self._runner.tasks
        task = await tasks.get(task_id)
        if task is None or str(task.metadata.get("node_kind", "")) != "gate":
            self._emit(_Operation("workflow.gate.resolved", task_id, "refused"), actor_did)
            return ControlPlaneResult(
                ok=False,
                errors=(OperationIssue(None, None, f"task {task_id!r} is not a workflow gate"),),
            )
        await self._authorize_gate(task, decision, actor_did, actor_roles)
        recorded = _GATE_DECISIONS[decision]
        metadata = {
            **dict(task.metadata),
            "gate_decision": recorded,
            "gate_notes": notes,
            "gate_actor_did": actor_did,
        }
        # ``failed`` only for the outcome that really does fail the run; the
        # other two settle the gate and let the runner take it from there.
        status = "failed" if recorded == "rejected" else "done"
        updated = await tasks.update_if(
            task_id,
            {"status": status, "metadata": metadata, "resolution": notes or recorded},
            where={"status": "review"},
            actor_did=actor_did,
        )
        if updated is None:
            current = await tasks.get(task_id)
            replayed = current is not None and current.metadata.get("gate_decision") == recorded
            self._emit(
                _Operation(
                    "workflow.gate.resolved",
                    task_id,
                    "replayed" if replayed else "conflict",
                    {"decision": recorded},
                ),
                actor_did,
            )
            return ControlPlaneResult(
                ok=False,
                errors=(OperationIssue(None, "status", "gate was already resolved"),),
            )
        self._emit(
            _Operation(
                "workflow.gate.resolved",
                task_id,
                recorded,
                {
                    "run_id": str(task.metadata.get("flow_run_id", "")),
                    "decision": recorded,
                    "notes": notes,
                },
            ),
            actor_did,
        )
        run_id = str(task.metadata.get("flow_run_id", ""))
        if not run_id:
            return ControlPlaneResult(ok=True)
        # Advance now rather than on the next tick: the reviewer is watching.
        record = await self._runner.advance(run_id)
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
            return ControlPlaneResult(ok=False, errors=(OperationIssue(None, None, str(exc)),))
        self._emit(
            _Operation("workflow.run.cancelled", run_id, record.status, {"reason": reason}),
            actor_did,
        )
        return ControlPlaneResult(ok=True, run=record)

    async def retry_node(
        self,
        run_id: str,
        node_id: str,
        *,
        actor_did: str,
        accept_side_effect_repeat: bool = False,
    ) -> ControlPlaneResult:
        """Re-run one failed node of a failed run; completed nodes are kept (J3 G3).

        ``accept_side_effect_repeat`` is the operator's explicit say-so that a
        non-idempotent tool may run again; it is audited with the retry.
        """
        target = f"{run_id}/{node_id}"
        try:
            record = await self._runner.retry_node(
                run_id,
                node_id,
                actor_did=actor_did,
                accept_side_effect_repeat=accept_side_effect_repeat,
            )
        except NodeRetryRefusedError as exc:
            self._emit(
                _Operation("workflow.node.retried", target, "refused", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(OperationIssue(node_id, None, str(exc)),))
        except Exception as exc:
            self._emit(
                _Operation("workflow.node.retried", target, "error", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(OperationIssue(None, None, str(exc)),))
        self._emit(
            _Operation(
                "workflow.node.retried",
                target,
                "retried",
                {"run_id": run_id, "accepted_repeat": accept_side_effect_repeat},
            ),
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
        problems = tuple(self._validate(definition, pending_files=frozenset(files or ())))
        if problems:
            self._emit(_Operation(action, target, "invalid", {"errors": len(problems)}), actor_did)
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
            return ControlPlaneResult(ok=False, errors=(OperationIssue(None, None, str(exc)),))
        self._emit(_Operation(action, target, "ok", {"status": bundle.status}), actor_did)
        return ControlPlaneResult(ok=True, bundle=bundle)

    async def _authorize_gate(
        self, task: Any, decision: str, actor_did: str, actor_roles: frozenset[str]
    ) -> None:
        """Allow the operator, a listed DID, or a listed role; deny everyone else.

        The approver list comes from the run's pinned, signed definition — never
        the row. Any failure to read it denies (fail closed), except for the
        operator, who decides every gate whatever the definition says.
        """
        if OPERATOR_ROLE in actor_roles:
            return
        try:
            approvers = await self._runner.gate_approvers(task)
        except Exception:  # reason: an unreadable approver list must deny, not 500
            logger.warning("gate %s: approvers unreadable; denying", task.id, exc_info=True)
            approvers = ()
        if actor_did in approvers or approver_roles(approvers) & actor_roles:
            return
        self._emit(
            _Operation(
                "workflow.gate.denied",
                task.id,
                "denied",
                {"task_id": task.id, "actor_did": actor_did, "decision": decision},
            ),
            actor_did,
        )
        raise GateNotAuthorizedError(task.id, actor_did)

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
    "GATE_WORDS",
    "OPERATOR_ROLE",
    "ControlPlaneResult",
    "OperationIssue",
    "WorkflowControlPlane",
]
