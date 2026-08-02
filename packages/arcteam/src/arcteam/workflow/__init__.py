"""ArcFlow — the workflow definition layer (SPEC-061).

A workflow is a named, semi-permanent, signed graph of nodes that an agent
authors from conversation, a human edits in an IDE, or an operator builds in
the dashboard — one artifact, one validator, one draft-then-sign lifecycle.

This subpackage owns the *definition*, never the execution:

* :mod:`~arcteam.workflow.models` — the typed ``workflow.toml`` (COMP-001).
* :mod:`~arcteam.workflow.predicates` — the frozen v1 predicate grammar behind
  a single ``evaluate`` seam, with no ``eval`` anywhere (COMP-003).
* :mod:`~arcteam.workflow.resolver` — ``$nodes``/``$input`` references bound as
  typed values, never interpolated into text (COMP-004).
* :mod:`~arcteam.workflow.validator` — whole-graph static validation returning
  repair-oriented typed issues (COMP-002).
* :mod:`~arcteam.workflow.store` — canonical hashing, the file manifest, the
  operator-pinned signature gate, versioning, and archival (COMP-005/022).

The load-bearing invariant: **nothing here can confer signed status.** Parsing
and validating a perfect definition yields ``status="draft"``. Only
:func:`~arcteam.workflow.store.sign_definition`, which demands an operator
private key that never enters an agent process, produces a signature.
"""

from __future__ import annotations

from arcteam.workflow.errors import (
    InvalidWorkflowIdError,
    PredicateError,
    PredicateEvaluationError,
    PredicateParseError,
    PurgeRefusedError,
    StaleEditError,
    TextualInterpolationError,
    UnresolvableReferenceError,
    UnsignedWorkflowError,
    ValidationIssue,
    WorkflowArchivedError,
    WorkflowError,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    WorkflowParseError,
    WorkflowReferenceError,
    WorkflowValidationError,
)
from arcteam.workflow.models import (
    MAX_DEFINITION_BYTES,
    MAX_NODES,
    NODE_KINDS,
    SCHEMA_VERSION,
    WORKFLOW_ID_PATTERN,
    ActiveHours,
    AgentNode,
    Budget,
    GateNode,
    InputSpec,
    JoinMode,
    NodeBase,
    Route,
    RouterMode,
    RouterNode,
    ScriptNode,
    ToolNode,
    Trigger,
    TriggerType,
    WorkflowDefinition,
    WorkflowNode,
    parse_definition,
)
from arcteam.workflow.predicates import (
    BoolOp,
    Compare,
    LiteralValue,
    Not,
    PathRef,
    Predicate,
    evaluate,
    parse_predicate,
    paths_in,
    referenced_nodes,
)
from arcteam.workflow.resolver import (
    Reference,
    embedded_reference_strings,
    is_reference,
    malformed_reference_strings,
    parse_reference,
    references_in,
    resolve_args,
    resolve_value,
)
from arcteam.workflow.serialize import (
    canonical_bytes,
    content_hash,
    dump_toml,
    file_manifest,
    referenced_files,
)
from arcteam.workflow.store import (
    DefinitionStatus,
    DefinitionStore,
    WorkflowAuditHook,
    WorkflowBundle,
    load_sidecar,
    sign_definition,
)
from arcteam.workflow.validator import KnownReferences, confine, validate_definition

__all__ = [
    "MAX_DEFINITION_BYTES",
    "MAX_NODES",
    "NODE_KINDS",
    "SCHEMA_VERSION",
    "WORKFLOW_ID_PATTERN",
    "ActiveHours",
    "AgentNode",
    "BoolOp",
    "Budget",
    "Compare",
    "DefinitionStatus",
    "DefinitionStore",
    "GateNode",
    "InputSpec",
    "InvalidWorkflowIdError",
    "JoinMode",
    "KnownReferences",
    "LiteralValue",
    "NodeBase",
    "Not",
    "PathRef",
    "Predicate",
    "PredicateError",
    "PredicateEvaluationError",
    "PredicateParseError",
    "PurgeRefusedError",
    "Reference",
    "Route",
    "RouterMode",
    "RouterNode",
    "ScriptNode",
    "StaleEditError",
    "TextualInterpolationError",
    "ToolNode",
    "Trigger",
    "TriggerType",
    "UnresolvableReferenceError",
    "UnsignedWorkflowError",
    "ValidationIssue",
    "WorkflowArchivedError",
    "WorkflowAuditHook",
    "WorkflowBundle",
    "WorkflowDefinition",
    "WorkflowError",
    "WorkflowIntegrityError",
    "WorkflowNode",
    "WorkflowNotFoundError",
    "WorkflowParseError",
    "WorkflowReferenceError",
    "WorkflowValidationError",
    "canonical_bytes",
    "confine",
    "content_hash",
    "dump_toml",
    "embedded_reference_strings",
    "evaluate",
    "file_manifest",
    "is_reference",
    "load_sidecar",
    "malformed_reference_strings",
    "parse_definition",
    "parse_predicate",
    "parse_reference",
    "paths_in",
    "referenced_files",
    "referenced_nodes",
    "references_in",
    "resolve_args",
    "resolve_value",
    "sign_definition",
    "validate_definition",
]
