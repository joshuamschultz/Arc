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

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from .errors import (
    GateNotAuthorizedError,
    PlaceholderOwnerError,
    WorkflowError,
    WorkflowNotFoundError,
)
from .gate_outcome import (
    DATA_DECISIONS,
    DECISION_WORDS,
    DECISIONS,
    OPEN_PAYLOAD_SCHEMA,
    Decision,
    outcome_envelope,
    settled_status,
)
from .runner import (
    TEST_RUN_MAX_COST_USD,
    NodeRetryRefusedError,
    WorkflowInputRefusedError,
    WorkflowRunner,
)
from .runner_contracts import (
    BundleSpec,
    DefinitionParser,
    DefinitionStoreLike,
    DefinitionValidator,
    Initiator,
    RunRecord,
    RunStoreLike,
    Tier,
    TriggerContext,
    TriggerSpec,
    ValidationIssueLike,
)
from .schema_check import check
from .stores import RunIdentityConflictError
from .templates import load_template
from .validator import approver_roles

logger = logging.getLogger(__name__)

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
    #: Machine-readable refusal class a surface maps to a status
    #: (``not_found``, ``invalid_payload``, ``conflict``); empty when unclassified.
    code: str = ""


#: Domain tag bound into every event trigger digest (D-842).
EVENT_DIGEST_DOMAIN = "arc.workflow-event.v1"


def _refusal_issue(exc: Exception) -> OperationIssue:
    """A refused run as an issue, carrying the command hint the error holds (if any)."""
    hint = exc.cli_hint if isinstance(exc, PlaceholderOwnerError) else ""
    return OperationIssue(None, None, str(exc), cli_hint=hint)


def _run_refusal_issues(exc: Exception) -> tuple[OperationIssue, ...]:
    """A refused start as issues: one per failing input field, or one classified refusal."""
    if isinstance(exc, WorkflowInputRefusedError):
        return tuple(
            OperationIssue(
                issue.node_id,
                issue.field,
                issue.error,
                observed=issue.observed,
                admissible=issue.admissible,
                code="invalid_payload",
            )
            for issue in exc.issues
        )
    if isinstance(exc, RunIdentityConflictError):
        return (OperationIssue(None, "run_id", str(exc), code="conflict"),)
    return (_refusal_issue(exc),)


def _refused(issue: OperationIssue) -> ControlPlaneResult:
    return ControlPlaneResult(ok=False, errors=(issue,))


def _admitted_payload(
    metadata: Mapping[str, Any], decision: Decision, payload: Mapping[str, Any] | None
) -> tuple[dict[str, Any], tuple[OperationIssue, ...]]:
    """The payload a decision records, or the ``invalid_payload`` issues refusing it.

    Only ``approved`` / ``submitted`` carry data; the other outcomes record
    ``{}``. The schema is the object the runner stamped from the verified bundle
    (the control plane never reads bundle files). A row whose node declared a
    schema but carries none is refused, never waved through.
    """
    if decision not in DATA_DECISIONS:
        return {}, ()
    instance: Any = {} if payload is None else payload
    schema = metadata.get("output_schema_resolved")
    refusal = None
    if not isinstance(instance, Mapping):
        refusal = "payload must be a JSON object"
    elif metadata.get("output_schema") is not None and not isinstance(schema, Mapping):
        refusal = "the step's payload schema is not on its row"
    if refusal is not None:
        return {}, (OperationIssue(None, "payload", refusal, code="invalid_payload"),)
    found = check(instance, schema if isinstance(schema, Mapping) else OPEN_PAYLOAD_SCHEMA)
    if found:
        return {}, tuple(
            OperationIssue(
                None,
                f"payload.{issue.field}" if issue.field else "payload",
                issue.error,
                observed=issue.observed,
                admissible=tuple(issue.admissible),
                code="invalid_payload",
            )
            for issue in found
        )
    return dict(instance), ()


def _event_digest(*parts: str) -> str:
    """sha256 over ``parts`` as a JSON array, so no field can bleed into the next."""
    return hashlib.sha256(json.dumps(list(parts), separators=(",", ":")).encode()).hexdigest()


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
                trigger=TriggerContext("manual"),
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
        trigger: TriggerSpec,
        run_id: str | None = None,
        trigger_digest: str | None = None,
        detached: bool = False,
    ) -> ControlPlaneResult:
        """Start a run. The dashboard, the CLI, an agent, a schedule and an event all
        land here; ``initiator`` names which, and gates unsigned drafts. ``trigger``
        names what started it and selects the schema the input must satisfy.

        ``detached=True`` creates the run without the singleton lease; the
        lease-holding service runner advances it on its next tick. A replay of a
        pinned ``run_id`` returns the existing run and is audited as a replay,
        never as a second start.
        """
        try:
            record, created = await self._runner.start_or_replay(
                workflow_id,
                input=input,
                initiator=initiator,
                initiator_did=actor_did,
                trigger=trigger,
                run_id=run_id,
                trigger_digest=trigger_digest,
                detached=detached,
            )
        except Exception as exc:
            self._emit(
                _Operation("workflow.run.started", workflow_id, "refused", {"error": str(exc)}),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=_run_refusal_issues(exc))
        self._emit(
            _Operation(
                "workflow.run.started",
                f"{workflow_id}/{record.run_id}",
                "started" if created else "replayed",
                {"run_id": record.run_id, "trigger": trigger.kind},
            ),
            actor_did,
        )
        return ControlPlaneResult(ok=True, run=record)

    async def fire_event(
        self,
        workflow_id: str,
        name: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        actor_did: str,
        on_behalf_of: str | None,
    ) -> ControlPlaneResult:
        """Start a run from a declared event (SPEC-088 COMP-007, D-842, D-843).

        The run id is derived from (workflow, event, principal, key), so an
        identical repeat returns the same run and the same key with another
        payload is a ``conflict``. The key is scoped to the principal: one
        caller can neither replay nor block another's event. The run starts
        through :meth:`run` with initiator ``event``, so an unsigned bundle is
        refused at every tier and the payload is validated against the event's
        schema before any run exists.
        """
        missing = self._undeclared_event(workflow_id, name)
        if missing is not None:
            self._emit(
                _Operation(
                    "workflow.event.fired",
                    workflow_id,
                    missing.code or "refused",
                    {"event": name, "on_behalf_of": on_behalf_of},
                ),
                actor_did,
            )
            return ControlPlaneResult(ok=False, errors=(missing,))
        identity = (workflow_id, name, actor_did, idempotency_key)
        run_id = "evt-" + _event_digest(*identity)[:24]
        result = await self.run(
            workflow_id,
            input=payload,
            initiator="event",
            actor_did=actor_did,
            trigger=TriggerContext("event", name),
            run_id=run_id,
            trigger_digest=_event_digest(EVENT_DIGEST_DOMAIN, *identity),
        )
        self._emit(
            _Operation(
                "workflow.event.fired",
                f"{workflow_id}/{run_id}",
                "ok" if result.ok else "refused",
                {"event": name, "run_id": run_id, "on_behalf_of": on_behalf_of},
            ),
            actor_did,
        )
        return result

    def _undeclared_event(self, workflow_id: str, name: str) -> OperationIssue | None:
        """``not_found`` when the workflow or its event does not exist, a plain
        refusal when the bundle cannot be read; else ``None``.

        Reads the unverified render view only to name the miss precisely; trust
        is decided by the runner, which refuses an undeclared event itself too.
        """
        try:
            definition = self._definitions.load(workflow_id).definition
        except (KeyError, WorkflowNotFoundError):
            return OperationIssue(
                None, "workflow_id", f"no workflow {workflow_id!r}", code="not_found"
            )
        except (WorkflowError, ValueError) as exc:
            return OperationIssue(None, "workflow_id", str(exc))
        declared = tuple(sorted(event.name for event in definition.events))
        if name in declared:
            return None
        return OperationIssue(
            None,
            "event",
            f"workflow {workflow_id!r} declares no event {name!r}",
            observed=name,
            admissible=declared,
            code="not_found",
        )

    async def resolve_gate(
        self,
        task_id: str,
        *,
        decision: str,
        notes: str = "",
        payload: Mapping[str, Any] | None = None,
        decider_did: str,
        on_behalf_of: str | None = None,
        actor_roles: frozenset[str],
    ) -> ControlPlaneResult:
        """Resolve a waiting gate or human_input row.

        A gate takes ``approve``, ``fail_run`` or ``return_for_revision``; a
        human_input takes ``submit`` or ``fail_run`` (D-845). ``decider_did`` is
        who decides: the person an app acts for, else the authenticated caller
        (SPEC-087 ``acting_identity``); ``on_behalf_of`` names that person when
        an app relayed the decision.

        Only an approver decides (alpha-2 #67): the operator, a DID the step's
        signed ``approvers`` lists, a holder of a listed ``role:<name>``, or the
        signed ``escalate_to`` once a passed deadline escalated the row.
        ``actor_roles`` is what the CALLER established from an authenticated
        identity, never anything the decider typed. Anyone else gets
        :class:`GateNotAuthorizedError` and an audited ``workflow.gate.denied``.

        ``approve`` / ``submit`` carry ``payload``, validated against the schema
        the runner stamped on the row (``output_schema_resolved``) and capped at
        64 KiB; a refusal is an ``invalid_payload`` issue and the row keeps
        waiting. The outcome envelope (D-844) and the status land in ONE
        conditional write on ``status == review``, so a step decides once: a
        second decision, or one racing a passed deadline, finds nothing to
        update and is refused.

        Gate resolution exists only here (REQ-246). No agent-callable tool
        reaches it; the dashboard route, the CLI, and the channel card are all
        callers of this one method, and each one names the deciding human.
        """
        if decision not in DECISION_WORDS:
            return _refused(
                OperationIssue(
                    None,
                    "decision",
                    f"unknown gate decision {decision!r}",
                    decision,
                    DECISION_WORDS,
                )
            )
        tasks = self._runner.tasks
        task = await tasks.get(task_id)
        kind = "" if task is None else str(task.metadata.get("node_kind", ""))
        if task is None or kind not in DECISIONS:
            self._emit(_Operation("workflow.gate.resolved", task_id, "refused"), decider_did)
            return _refused(
                OperationIssue(
                    None, None, f"task {task_id!r} is not a workflow gate", code="not_found"
                )
            )
        await self._authorize_gate(task, decision, decider_did, actor_roles)
        allowed = DECISIONS[kind]
        if decision not in allowed:
            return _refused(
                OperationIssue(
                    None,
                    "decision",
                    f"a {kind} step does not take {decision!r}",
                    decision,
                    tuple(allowed),
                )
            )
        recorded = allowed[decision]
        accepted, issues = _admitted_payload(task.metadata, recorded, payload)
        if issues:
            self._emit(
                _Operation("workflow.gate.resolved", task_id, "invalid_payload"), decider_did
            )
            return ControlPlaneResult(ok=False, errors=issues)
        envelope = outcome_envelope(
            recorded,
            notes=notes,
            payload=accepted,
            decided_by=decider_did,
            on_behalf_of=on_behalf_of,
            decided_at=self._runner.now(),
            timed_out=False,
        )
        updated = await tasks.update_if(
            task_id,
            {
                "status": settled_status(recorded),
                "output": envelope,
                "resolution": notes or recorded,
            },
            where={"status": "review"},
            actor_did=decider_did,
        )
        if updated is None:
            current = await tasks.get(task_id)
            replayed = current is not None and (current.output or {}).get("decision") == recorded
            self._emit(
                _Operation(
                    "workflow.gate.resolved",
                    task_id,
                    "replayed" if replayed else "conflict",
                    {"decision": recorded},
                ),
                decider_did,
            )
            return _refused(
                OperationIssue(None, "status", "gate was already resolved", code="conflict")
            )
        run_id = str(task.metadata.get("flow_run_id", ""))
        self._emit(
            _Operation(
                "workflow.gate.resolved",
                task_id,
                recorded,
                {
                    "run_id": run_id,
                    "decision": recorded,
                    "notes": notes,
                    "on_behalf_of": on_behalf_of,
                },
            ),
            decider_did,
        )
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
