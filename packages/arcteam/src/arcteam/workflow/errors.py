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


class WorkflowNotFoundError(WorkflowError):
    """No bundle exists for the requested workflow id."""


__all__ = [
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
]
