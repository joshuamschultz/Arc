"""Typed failures for the ArcFlow definition layer (SPEC-061).

One error shape spans parse and graph validation. The research is explicit
about why: an authoring model repairs a rejected graph far more reliably when
the rejection names the field path, the value it observed, and the *admissible
alternatives* — the alternatives drive the largest share of the repair gain, so
they are a required part of the contract, not a nicety (REQ-222).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ValidationIssue(BaseModel):
    """One rejected thing, phrased so a model or a human can repair it.

    Attributes:
        node_id: The offending node, or ``None`` for a workflow-level problem.
        field: The field path within that node (or the workflow table).
        error: What is wrong, in one sentence.
        observed: The value actually seen.
        admissible: Concrete values or actions that would be accepted.
    """

    model_config = ConfigDict(frozen=True)

    node_id: str | None = None
    field: str
    error: str
    observed: Any = None
    admissible: tuple[str, ...] = ()


class WorkflowError(Exception):
    """Base class for every ArcFlow definition-layer failure."""


class WorkflowParseError(WorkflowError):
    """A document could not be turned into a :class:`WorkflowDefinition`."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        summary = "; ".join(f"{i.field}: {i.error}" for i in issues[:5])
        super().__init__(f"workflow definition could not be parsed — {summary}")


class WorkflowValidationError(WorkflowError):
    """A definition parsed but its graph did not validate; nothing was written."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        summary = "; ".join(f"{i.node_id or 'workflow'}.{i.field}: {i.error}" for i in issues[:5])
        super().__init__(f"workflow definition is not valid — {summary}")


class PlaceholderOwnerError(WorkflowError):
    """Nodes would fall back to the template placeholder owner, which no agent answers to."""

    def __init__(self, workflow_id: str, owner: str, node_ids: tuple[str, ...]) -> None:
        self.workflow_id = workflow_id
        self.owner = owner
        self.node_ids = node_ids
        super().__init__(
            f"workflow {workflow_id!r} is still owned by the template placeholder {owner!r}, "
            f"which no agent answers to; node(s) {', '.join(node_ids)} would wait on nobody. "
            "Pick a real agent as the owner, or as the agent for the node(s) named."
        )
        #: Command-line wording for the same fix. A terminal may print it; a page never does.
        self.cli_hint = (
            f"Set a real owner with `arc workflow edit {workflow_id} --owner @<agent>` "
            f"(add `--node <id>` to set one node's agent instead), or re-create it with "
            f"`arc workflow new {workflow_id} --from <template> --owner @<agent>`."
        )


class PredicateError(WorkflowError):
    """Base class for predicate grammar failures."""


class PredicateParseError(PredicateError):
    """The expression is not in the frozen v1 predicate grammar.

    A call, an attribute walk, an import, or any bare identifier lands here —
    at *parse* time, before any value is touched. That ordering is the security
    boundary: an unsupported construct can never reach evaluation (ASI05).
    """


class PredicateEvaluationError(PredicateError):
    """A well-formed predicate could not be evaluated against the given scope."""


class WorkflowReferenceError(WorkflowError):
    """Base class for ``$nodes``/``$input`` reference failures."""


class UnresolvableReferenceError(WorkflowReferenceError):
    """A reference names a node or field that the scope does not carry."""


class TextualInterpolationError(WorkflowReferenceError):
    """A string embedded a reference instead of *being* one.

    Refused rather than substituted. Every expression-injection class in
    comparable systems comes from splicing an upstream value into a command or
    prompt string; ArcFlow binds references as typed values only (LLM01).
    """


class StaleEditError(WorkflowError):
    """The editor's ``expected_version`` no longer matches the stored version."""


class WorkflowIntegrityError(WorkflowError):
    """A signed bundle no longer matches its signature — fail closed."""


class UnsignedWorkflowError(WorkflowError):
    """A draft or foreign-signed definition was asked to run above personal tier."""


class WorkflowArchivedError(WorkflowError):
    """An archived definition was asked to run or to be edited."""


class PurgeRefusedError(WorkflowError):
    """A purge would orphan live runs, or would destroy history unrecorded."""


class InvalidWorkflowIdError(WorkflowError):
    """A workflow id was not a bare name, so it could have named a path.

    A workflow id also names a directory in the owning agent's workspace. An id
    carrying a separator or a parent reference would let a caller read, archive,
    or destroy a bundle outside that workspace — including another agent's
    (ASI03, and a breach of workspace containment, ADR-029). Refused fail-closed.
    """


class WorkflowNotFoundError(WorkflowError):
    """No bundle exists for the requested workflow id."""


class GateNotAuthorizedError(WorkflowError):
    """The decider may not resolve this gate.

    Not the operator, not listed by DID, and holding none of the gate's listed
    roles in the team registry. Roles are never taken from the caller's message.
    """

    def __init__(self, task_id: str, actor_did: str) -> None:
        self.task_id = task_id
        self.actor_did = actor_did
        super().__init__(f"{actor_did} is not an approver of gate {task_id}")


__all__ = [
    "GateNotAuthorizedError",
    "InvalidWorkflowIdError",
    "PredicateError",
    "PredicateEvaluationError",
    "PredicateParseError",
    "PurgeRefusedError",
    "StaleEditError",
    "TextualInterpolationError",
    "UnresolvableReferenceError",
    "UnsignedWorkflowError",
    "ValidationIssue",
    "WorkflowArchivedError",
    "WorkflowError",
    "WorkflowIntegrityError",
    "WorkflowNotFoundError",
    "WorkflowParseError",
    "WorkflowReferenceError",
    "WorkflowValidationError",
]
