"""Decorator-form workflows module — SPEC-061 COMP-012.

Twelve module-level ``@tool`` functions expose the ArcFlow builder surface.
There is no ``@capability`` class: the engine's lifecycle belongs to the runner
host in arcgateway (COMP-009), not to a per-agent capability.

The surface is deliberately THIN. Every tool does exactly four agent-side jobs
and then delegates:

1. **Allowlist** the caller's fields (``models.project``) so an invented
   ``model``, ``temperature``, ``status``, or ``signature`` is dropped rather
   than forwarded (ASI02).
2. **Check quotas before validation work** — a node-count check is free, a
   whole-graph validation over 200 nodes is not, so the free check runs first
   and neither reaches the control plane on refusal (LLM10).
3. **Normalize inline free text** (NFKC + zero-width strip) *before* the
   injection scan, so a homoglyph payload folds to ASCII first (LLM01).
4. **Delegate** to arcteam's control plane (COMP-021) — the same operation set
   the command line and the dashboard call — and refuse to report a mutation as
   anything but a draft (REQ-223).

Graph validation, versioning, hashing, and signing are NOT here and must never
be: one implementation of what a workflow edit means is the only way three
authoring surfaces stay in agreement.

Audit is emitted CENTRALLY by the tool registry, keyed on each tool's declared
``classification`` — tools never call ``arctrust.audit.emit`` themselves.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from arcagent.modules.workflows import _runtime
from arcagent.modules.workflows.models import (
    NODE_FIELDS,
    TRIGGER_FIELDS,
    as_object,
    as_objects,
    issue,
    normalize_inline_text,
    project,
)
from arcagent.tools._decorator import hook, tool

_logger = logging.getLogger("arcagent.modules.workflows.capabilities")

# The only status a builder tool may ever report for a mutation. Signing is an
# out-of-band operator command whose key never enters an agent process
# (REQ-223/REQ-224), so a mutation that came back as anything else means a lower
# layer regressed — and this surface fails closed rather than reporting it.
_DRAFT = "draft"

# What a tool catches and reports instead of crashing the loop.
_TOOL_ERRORS = (ValueError, TypeError, OSError)

# Ceiling on companion files per call — a bundle is a handful of prompts and
# schemas, not a directory tree.
_MAX_FILES = 20


async def _plane() -> Any:
    """Return the control plane, finishing lazy async wiring first, or None.

    The team roster is refreshed on the way through: an agent that joined since
    this process started must be nameable in a node, and one that never existed
    must be refused at authoring time rather than at its first dispatch.
    """
    await _runtime.ensure_control_plane()
    await _runtime.refresh_roster()
    return _runtime.state().control_plane


def _errors(*issues: dict[str, Any]) -> str:
    """Render a typed error list plus the agent's remaining repair budget."""
    return json.dumps(
        {
            "errors": list(issues),
            "max_repair_attempts": _runtime.state().config.max_repair_attempts,
        }
    )


def _unavailable() -> str:
    return _errors(
        issue(
            field="workflows",
            error=(
                "workflow control plane unavailable — the arcteam workflow engine "
                "is not installed or could not be opened"
            ),
        )
    )


def _issues(raw: Any) -> str:
    """Render arcteam's issue objects into the one typed error shape.

    ``ValidationIssue`` is a pydantic model and ``OperationIssue`` a dataclass;
    both carry the same five fields, and an agent repairing a rejection must
    never have to branch on which one it got.
    """
    return json.dumps(
        {
            "errors": [_issue_dict(item) for item in raw],
            "max_repair_attempts": _runtime.state().config.max_repair_attempts,
        }
    )


def _issue_dict(item: Any) -> dict[str, Any]:
    """One issue in the canonical five-key shape, whatever its class."""
    if hasattr(item, "model_dump"):
        raw = item.model_dump(mode="json")
    elif is_dataclass(item) and not isinstance(item, type):
        raw = asdict(item)
    else:
        return issue(field="workflow", error=str(item))
    return issue(
        node_id=raw.get("node_id"),
        field=str(raw.get("field") or "workflow"),
        error=str(raw.get("error", "")),
        observed=raw.get("observed"),
        admissible=tuple(raw.get("admissible") or ()),
    )


def _from_exception(exc: Exception) -> str:
    """Convert a control-plane refusal into the one typed error shape."""
    raw = getattr(exc, "issues", None)
    if raw:
        return _issues(raw)
    # A stale edit is refused, never merged (REQ-248). Recognised by the
    # committed arcteam exception name rather than by message text.
    names = {klass.__name__ for klass in type(exc).__mro__}
    if "StaleEditError" in names:
        return _errors(
            issue(
                field="expected_version",
                error=f"stale edit refused: {exc}",
                admissible=("re-read the workflow with workflow_inspect, then retry",),
            )
        )
    return _errors(issue(field="workflow", error=str(exc)))


def _mutation(result: Any) -> str:
    """Render a mutation result, refusing anything that is not a draft.

    This is enforcement, not decoration: there must be no path by which a
    successful edit reports signed status, so the check lives on the surface
    rather than only in a test.
    """
    status = getattr(result, "status", None)
    if status != _DRAFT:
        _logger.error("Workflow mutation returned status %r; refusing to report it", status)
        return _errors(
            issue(
                field="status",
                error=(
                    f"mutation returned status {status!r}; a builder tool only ever "
                    f"produces a {_DRAFT} — signing is an out-of-band operator command"
                ),
                observed=status,
                admissible=(_DRAFT,),
            )
        )
    return json.dumps({"status": _DRAFT, **_dump(result)})


def _dump(result: Any) -> dict[str, Any]:
    """JSON-friendly form of a control-plane result (pydantic model or mapping)."""
    if isinstance(result, dict):
        return result
    dump = getattr(result, "model_dump", None)
    if dump is None:
        return {"result": str(result)}
    payload = dump(mode="json")
    return payload if isinstance(payload, dict) else {"result": payload}


def _clean_nodes(nodes: Any) -> list[dict[str, Any]]:
    """Project every node through the field allowlist.

    Takes ``Any`` on purpose: what arrives is model output, and a shape this
    refuses must come back as a repairable error rather than a crash — see
    :func:`~arcagent.modules.workflows.models.as_objects`.
    """
    return [project(node, NODE_FIELDS) for node in as_objects(nodes, "nodes")]


def _clean_files(files: Any, st: _runtime._State) -> dict[str, bytes] | None:
    """Companion bodies as bytes, keyed by bundle-relative path.

    Paths are confined by the definition store, which refuses anything that
    escapes the bundle — this only bounds how much an agent may write in one
    call, and refuses a shape it cannot encode.
    """
    if not files:
        return None
    # Same coercion every other structured argument gets: a model hands a JSON
    # string where an object is declared, and a refusal it cannot repair costs
    # the whole build.
    files = as_object(files, "files")
    if len(files) > _MAX_FILES:
        raise ValueError(f"at most {_MAX_FILES} companion files per call")
    payload: dict[str, bytes] = {}
    for path, body in files.items():
        if not isinstance(body, str):
            raise ValueError(f"{path}: file body must be text, not {type(body).__name__}")
        encoded = body.encode("utf-8")
        if len(encoded) > st.config.max_file_bytes:
            raise ValueError(f"{path}: exceeds {st.config.max_file_bytes} bytes")
        payload[str(path)] = encoded
    return payload


def _text(value: str, field: str) -> str:
    """Normalize one inline free-text field; raises ``ValueError`` on refusal."""
    if not value:
        return ""
    limit = _runtime.state().config.max_inline_text_length
    try:
        return normalize_inline_text(value, max_length=limit)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


# A workflow id is a BARE NAME because it becomes a directory. arcteam's store
# raises ``InvalidWorkflowIdError`` on anything else — but its guard is a
# backstop, and a backstop firing in normal operation means the boundary check
# is missing. This is the boundary.
_LEGAL_WORKFLOW_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _bad_workflow_id(workflow_id: str) -> dict[str, Any] | None:
    """The typed refusal for an id that is not a bare name, or None."""
    if not workflow_id:
        return issue(field="workflow_id", error="a workflow needs a stable id")
    if _LEGAL_WORKFLOW_ID.match(workflow_id) and ".." not in workflow_id:
        return None
    return issue(
        field="workflow_id",
        error=(
            "a workflow id is a bare name and may never contain a path separator "
            "or '..' — it becomes a directory under the agent's workspace"
        ),
        observed=workflow_id,
        admissible=("lowercase letters, digits, '.', '_', '-'; 1-64 characters",),
    )


def _version_required(expected_version: int | None) -> dict[str, Any] | None:
    """The typed refusal for a missing ``expected_version``, or None if present."""
    if expected_version is not None:
        return None
    return issue(
        field="expected_version",
        error="an edit must declare the version it was based on so a stale edit is refused",
        admissible=("the `version` returned by workflow_inspect",),
    )


# --- Mutating tools --------------------------------------------------------


@tool(
    name="workflow_create",
    description="Create a named multi-step, multi-agent workflow from a list of steps",
    classification="state_modifying",
    capability_tags=("workflows",),
    when_to_use="When a repeatable, multi-step process should become a named artifact.",
    requires_skill="workflow-builder",
)
async def workflow_create(
    workflow_id: str = "",
    description: str = "",
    owner: str = "",
    channel: str = "",
    nodes: list[dict[str, Any]] | None = None,
    files: dict[str, str] | None = None,
) -> str:
    """Create version 1 of a workflow as an unsigned draft.

    ``files`` carries the prompt and schema bodies a node references, keyed by
    the SAME bundle-relative path the node declares. They travel with the
    definition so the whole bundle lands in one validated write — without this
    an agent has to know where bundles live on disk and write them itself,
    which is how a build turns into a filesystem hunt.
    """
    st = _runtime.state()
    bad_id = _bad_workflow_id(workflow_id)
    if bad_id is not None:
        return _errors(bad_id)
    try:
        node_list = as_objects(nodes, "nodes")
    except ValueError as exc:
        return _errors(issue(field="nodes", error=str(exc), observed=nodes))
    quota = _node_quota(len(node_list))
    if quota is not None:
        return _errors(quota)
    plane = await _plane()
    if plane is None:
        return _unavailable()
    if _workflow_count(st) >= st.config.max_workflows:
        return _errors(
            issue(
                field="workflow_id",
                error=f"workflow quota exceeded (max {st.config.max_workflows})",
            )
        )
    try:
        header = {"id": workflow_id, "description": _text(description, "description")}
    except ValueError as exc:
        return _errors(issue(field="description", error=str(exc), observed=description))
    if owner:
        header["owner"] = owner
    if channel:
        header["channel"] = channel
    document: dict[str, Any] = {"workflow": header, "node": _clean_nodes(node_list)}
    try:
        payload = _clean_files(files, st)
    except ValueError as exc:
        return _errors(issue(field="files", error=str(exc)))
    return await _mutate(lambda: plane.create(document, actor_did=st.identity.did, files=payload))


@tool(
    name="workflow_add_node",
    description="Add one step to an existing workflow; returns a new draft version",
    when_to_use="When an existing workflow is missing a step.",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_add_node(
    workflow_id: str = "",
    node: dict[str, Any] | None = None,
    expected_version: int | None = None,
    files: dict[str, str] | None = None,
) -> str:
    """Add a node and re-validate the whole graph.

    ``files`` carries the bodies this node references. A node that names a
    prompt the bundle does not have yet is refused — correctly, since a
    dangling reference cannot run — so declaring the reference and writing the
    file are ONE call, not a two-step dance an agent has to discover.
    """
    st = _runtime.state()
    try:
        fields = project(as_object(node or {}, "node"), NODE_FIELDS)
    except ValueError as exc:
        return _errors(issue(field="node", error=str(exc), observed=node))
    try:
        payload = _clean_files(files, st)
    except ValueError as exc:
        return _errors(issue(field="files", error=str(exc)))
    return await _edit_document(
        workflow_id,
        expected_version,
        reason="added a node",
        change=lambda doc: doc["node"].append(fields),
        added_nodes=1,
        files=payload,
    )


@tool(
    name="workflow_edit_node",
    description="Change one step of an existing workflow; returns a new draft version",
    when_to_use="When a step's agent, prompt, tool, condition, or wiring is wrong.",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_edit_node(
    workflow_id: str = "",
    node_id: str = "",
    updates: dict[str, Any] | None = None,
    expected_version: int | None = None,
    files: dict[str, str] | None = None,
) -> str:
    """Edit a node in place. A node id is immutable — a rename is remove + add.

    ``files`` carries any body the edit newly references, so pointing a node at
    a prompt and writing that prompt is one call.
    """
    st = _runtime.state()
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    try:
        payload = _clean_files(files, st)
    except ValueError as exc:
        return _errors(issue(node_id=node_id, field="files", error=str(exc)))
    # ``id`` is deliberately excluded: node ids are immutable once signed, so an
    # in-flight run pinned by hash and every historical audit row stay valid.
    allowed = NODE_FIELDS - {"id"}
    try:
        clean = project(as_object(updates or {}, "updates"), allowed)
    except ValueError as exc:
        return _errors(issue(node_id=node_id, field="updates", error=str(exc), observed=updates))
    if not clean:
        return _errors(
            issue(
                node_id=node_id,
                field="updates",
                error="no editable field supplied",
                observed=sorted(updates or {}),
                admissible=tuple(sorted(allowed)),
            )
        )

    def change(document: dict[str, Any]) -> None:
        for entry in document["node"]:
            if entry.get("id") == node_id:
                entry.update(clean)
                return
        raise KeyError(node_id)

    return await _edit_document(
        workflow_id,
        expected_version,
        reason="edited a node",
        change=change,
        files=payload,
    )


@tool(
    name="workflow_remove_node",
    description="Remove one step from a workflow; returns a new draft version",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_remove_node(
    workflow_id: str = "",
    node_id: str = "",
    expected_version: int | None = None,
) -> str:
    """Remove a node and re-validate the whole graph (dangling needs surface here)."""

    def change(document: dict[str, Any]) -> None:
        remaining = [n for n in document["node"] if n.get("id") != node_id]
        if len(remaining) == len(document["node"]):
            raise KeyError(node_id)
        document["node"] = remaining

    return await _edit_document(
        workflow_id, expected_version, reason="removed a node", change=change
    )


@tool(
    name="workflow_put_files",
    description="Write prompt or schema files into a workflow bundle; returns a new draft version",
    classification="state_modifying",
    capability_tags=("workflows",),
    when_to_use="When a node references a prompt or schema file that does not exist yet.",
    requires_skill="workflow-builder",
)
async def workflow_put_files(
    workflow_id: str = "",
    files: dict[str, str] | None = None,
    expected_version: int | None = None,
) -> str:
    """Add or replace companion files, keyed by the path a node declares.

    The bundle is the unit that gets signed, so its prompts and schemas are
    written through this tool rather than with the filesystem tools: an agent
    never has to know where bundles live, and every body lands inside the
    bundle the store confines.
    """
    st = _runtime.state()
    try:
        payload = _clean_files(files, st)
    except ValueError as exc:
        return _errors(issue(field="files", error=str(exc)))
    if payload is None:
        return _errors(issue(field="files", error="no files given"))
    return await _edit_document(
        workflow_id,
        expected_version,
        reason="added bundle files",
        change=lambda _doc: None,
        files=payload,
    )


@tool(
    name="workflow_set_trigger",
    description="Set when a workflow fires (cron/manual); returns a new draft version",
    when_to_use="When the process should run on a schedule instead of on request.",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_set_trigger(
    workflow_id: str = "",
    trigger: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> str:
    """Declare when a workflow fires. Dispatch is typed — never a free-text prompt."""

    try:
        clean = project(as_object(trigger, "trigger"), TRIGGER_FIELDS) if trigger else {}
    except ValueError as exc:
        return _errors(issue(field="trigger", error=str(exc), observed=trigger))

    def change(document: dict[str, Any]) -> None:
        if clean:
            document["trigger"] = clean
        else:
            document.pop("trigger", None)

    return await _edit_document(
        workflow_id, expected_version, reason="set the trigger", change=change
    )


@tool(
    name="workflow_set_channel",
    description="Bind a workflow to the group channel its runs narrate into",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_set_channel(
    workflow_id: str = "",
    channel: str = "",
    expected_version: int | None = None,
) -> str:
    """Bind the channel where node transitions, handoffs, and gates are narrated."""

    def change(document: dict[str, Any]) -> None:
        document["workflow"]["channel"] = channel

    return await _edit_document(
        workflow_id, expected_version, reason="set the channel", change=change
    )


@tool(
    name="workflow_request_signature",
    description="Ask the operator to sign a draft; it appears in their approvals queue",
    classification="state_modifying",
    capability_tags=("workflows",),
    when_to_use="When a draft is finished and the person needs to authorize it.",
    requires_skill="workflow-builder",
)
async def workflow_request_signature(workflow_id: str = "", reason: str = "") -> str:
    """Raise an operator approval that, once granted, signs this draft.

    The agent can never sign — this writes a REQUEST, and the signature happens
    in the operator-authenticated path that mints the grant (REQ-224). The
    request is bound to the definition's content hash, so a draft edited after
    the ask no longer matches and the approval is refused rather than signing
    something the operator did not read.
    """
    st = _runtime.state()
    await _runtime.ensure_control_plane()
    bundle = _load(st, workflow_id)
    if bundle is None:
        return _errors(issue(field="workflow_id", error=f"workflow '{workflow_id}' not found"))
    try:
        from arcstore.approvals import ApprovalStore, PendingApproval
        from arcstore.backends.sqlite import SqliteBackend
        from arcstore.config import store_db_path

        backend = SqliteBackend(store_db_path(st.config.data_dir or None))
        await backend.start()
        approval = await ApprovalStore(backend).create(
            PendingApproval(
                id=f"wfsign_{uuid.uuid4().hex[:12]}",
                agent_did=st.identity.did,
                agent_label=workflow_id,
                tool="workflow_sign",
                legs=[],
                call_hash=bundle.content_hash,
                arguments={
                    "workflow_id": workflow_id,
                    "version": str(bundle.definition.version),
                    "reason": _text(reason, "reason")[:200],
                },
            )
        )
    except _TOOL_ERRORS as exc:
        return _errors(issue(field="workflow_id", error=f"could not raise the request: {exc}"))
    return json.dumps(
        {
            "approval_id": approval.id,
            "workflow_id": workflow_id,
            "status": "pending_operator_approval",
            "note": "Approving it in the operator's approvals queue signs this exact draft.",
        }
    )


@tool(
    name="workflow_run",
    description="Start a run of a workflow with typed input",
    classification="state_modifying",
    capability_tags=("workflows",),
    requires_skill="workflow-builder",
)
async def workflow_run(workflow_id: str = "", input: dict[str, Any] | None = None) -> str:  # noqa: A002 - matches JSON schema field name
    """Start a run. Refuses when the definition spans an unapproved composition."""
    st = _runtime.state()
    plane = await _plane()
    if plane is None:
        return _unavailable()
    bundle = _load(st, workflow_id)
    if bundle is None:
        return _errors(issue(field="workflow_id", error=f"workflow '{workflow_id}' not found"))
    refusal = await _activation_refusal(bundle, workflow_id)
    if refusal is not None:
        return _errors(issue(field="workflow_id", error=refusal))
    try:
        run_input = as_object(input, "input") if input else {}
    except ValueError as exc:
        return _errors(issue(field="input", error=str(exc), observed=input))
    try:
        result = await plane.run(workflow_id, input=run_input, actor_did=st.identity.did)
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _result(result, "run")


@tool(
    name="workflow_cancel_run",
    description="Cancel an in-flight workflow run",
    classification="state_modifying",
    capability_tags=("workflows",),
)
async def workflow_cancel_run(run_id: str = "") -> str:
    """Cancel a run. The Run record is marked before node cancellation fans out."""
    st = _runtime.state()
    plane = await _plane()
    if plane is None:
        return _unavailable()
    try:
        result = await plane.cancel(run_id, actor_did=st.identity.did)
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _result(result, "run")


# --- Read-only tools -------------------------------------------------------


@tool(
    name="workflow_list",
    description="List this agent's workflows with status and current version",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_list() -> str:
    """List workflows so the agent can talk about what it owns."""
    st = _runtime.state()
    await _runtime.ensure_control_plane()
    if st.definitions is None:
        return _unavailable()
    bundles = [_load(st, workflow_id) for workflow_id in st.definitions.list_ids()]
    return json.dumps([_dump(b) for b in bundles if b is not None])


@tool(
    name="workflow_inspect",
    description="Show one workflow's graph, version, status, and content hash",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_inspect(workflow_id: str = "", version: int | None = None) -> str:
    """Render one workflow, optionally at a retained prior version."""
    st = _runtime.state()
    await _runtime.ensure_control_plane()
    if st.definitions is None:
        return _unavailable()
    if version is not None:
        try:
            definition = st.definitions.load_version(workflow_id, version)
        except Exception as exc:  # reason: a tool returns JSON, never a crash
            return _from_exception(exc)
        return json.dumps(_dump(definition))
    bundle = _load(st, workflow_id)
    if bundle is None:
        return _errors(issue(field="workflow_id", error=f"workflow '{workflow_id}' not found"))
    return json.dumps(_dump(bundle))


@tool(
    name="workflow_runs",
    description="List a workflow's run history",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_runs(workflow_id: str = "") -> str:
    """List runs of one workflow, newest first.

    Read from the run store the runner writes to, not from the runner object:
    an agent can ask how a workflow has been going on a box where the runner is
    hosted in the fleet service and this process has none.
    """
    st = _runtime.state()
    await _runtime.ensure_control_plane()
    runs = await _run_store(st)
    if runs is None:
        return _no_runner()
    records = await runs.list_for_workflow(workflow_id)
    return json.dumps([_dump(record) for record in records])


@tool(
    name="workflow_run_status",
    description="Report one run's status, path taken, and per-node state",
    classification="read_only",
    capability_tags=("workflows",),
)
async def workflow_run_status(run_id: str = "") -> str:
    """Report a single run's live state, including the path actually taken."""
    st = _runtime.state()
    await _runtime.ensure_control_plane()
    runs = await _run_store(st)
    if runs is None:
        return _no_runner()
    record = await runs.record(run_id)
    if record is None:
        return _errors(issue(field="run_id", error=f"run '{run_id}' not found"))
    return json.dumps(_dump(record))


async def _run_store(st: _runtime._State) -> Any:
    """The durable run plane, from the hosted runner or opened directly."""
    runner_runs = getattr(st.runner, "runs", None)
    if runner_runs is not None:
        return runner_runs
    from arcagent.modules.workflows.run_store import open_run_store

    try:
        return await open_run_store(str(st.config.data_dir or ""))
    except Exception:  # reason: a read tool reports absence, never crashes
        _logger.warning("workflow run store unavailable", exc_info=True)
        return None


# --- Delegation helpers ----------------------------------------------------


def _node_quota(count: int) -> dict[str, Any] | None:
    """Refuse a node count over the ceiling, before any validation work."""
    limit = _runtime.state().config.max_nodes
    if count <= limit:
        return None
    return issue(field="nodes", error=f"node quota exceeded (max {limit})", observed=count)


def _workflow_count(st: _runtime._State) -> int:
    """How many workflows this agent already owns (0 when the store is absent)."""
    return len(st.definitions.list_ids()) if st.definitions is not None else 0


def _load(st: _runtime._State, workflow_id: str) -> Any:
    """Load a bundle, or None when the id is illegal / it cannot be read.

    The id is re-checked here so EVERY read path is guarded, including the ones
    that reach the store without a mutation's up-front validation. arcteam's own
    guard raises rather than returning False from ``exists()``, so an unchecked
    id would surface as a crash rather than a typed refusal.
    """
    if st.definitions is None or _bad_workflow_id(workflow_id) is not None:
        return None
    try:
        return st.definitions.load(workflow_id)
    except Exception:  # reason: a missing or unreadable bundle is "not found" here
        return None


async def _mutate(operation: Callable[[], Awaitable[Any]]) -> str:
    """Run one control-plane mutation and render its result as a draft."""
    try:
        result = await operation()
    except Exception as exc:  # reason: a tool returns JSON, it never crashes the loop
        return _from_exception(exc)
    return _result(result, "bundle")


async def _edit_document(
    workflow_id: str,
    expected_version: int | None,
    *,
    reason: str,
    change: Callable[[dict[str, Any]], None],
    added_nodes: int = 0,
    files: dict[str, bytes] | None = None,
) -> str:
    """Load, mutate in memory, then submit the WHOLE document once.

    The ordering is the point (``decompose_task``'s lesson): everything is built
    and checked in memory and only then written, so a rejected edit leaves no
    half-applied graph behind. The control plane re-parses and re-validates the
    submitted document, so a targeted edit gets the identical whole-graph check
    the file and dashboard surfaces get.
    """
    st = _runtime.state()
    bad_id = _bad_workflow_id(workflow_id)
    if bad_id is not None:
        return _errors(bad_id)
    stale = _version_required(expected_version)
    if stale is not None:
        return _errors(stale)
    plane = await _plane()
    if plane is None:
        return _unavailable()
    bundle = _load(st, workflow_id)
    if bundle is None:
        return _errors(issue(field="workflow_id", error=f"workflow '{workflow_id}' not found"))
    document = _document(bundle)
    quota = _node_quota(len(document["node"]) + added_nodes)
    if quota is not None:
        return _errors(quota)
    try:
        change(document)
    except KeyError as exc:
        return _errors(
            issue(
                node_id=str(exc.args[0]),
                field="node_id",
                error="no such node in this workflow",
                observed=str(exc.args[0]),
                admissible=tuple(str(n.get("id")) for n in document["node"]),
            )
        )
    return await _mutate(
        lambda: plane.edit(
            workflow_id,
            document,
            expected_version=expected_version,
            actor_did=st.identity.did,
            reason=reason,
            files=files,
        )
    )


def _document(bundle: Any) -> dict[str, Any]:
    """Turn a loaded bundle back into the authoring document shape.

    ``{workflow: {...}, node: [...], trigger: {...}, input: {...}}`` — the same
    shape ``parse_definition`` consumes, so a round-trip through a targeted edit
    is byte-for-byte a document the validator already accepts.
    """
    definition = getattr(bundle, "definition", bundle)
    dumped = definition.model_dump(mode="json", exclude_none=True)
    nodes = dumped.pop("nodes", [])
    trigger = dumped.pop("trigger", None)
    input_spec = dumped.pop("input_spec", None)
    document: dict[str, Any] = {"workflow": dumped, "node": list(nodes)}
    if trigger is not None:
        document["trigger"] = trigger
    if input_spec is not None:
        document["input"] = input_spec
    return document


def _result(result: Any, field_name: str) -> str:
    """Render a ``ControlPlaneResult``: the payload, or its typed error list."""
    if not getattr(result, "ok", False):
        return _issues(getattr(result, "errors", ()))
    payload = getattr(result, field_name, None)
    return _mutation(payload) if field_name == "bundle" else json.dumps(_dump(payload))


async def _activation_refusal(bundle: Any, workflow_id: str) -> str | None:
    """COMP-016 — operator approval for a definition spanning a forbidden set."""
    from arcagent.modules.workflows.activation import require_activation_grant, union_of_legs

    st = _runtime.state()
    nodes = _document(bundle)["node"]
    return await require_activation_grant(
        human_gate=st.human_gate,
        root=st.workspace / st.config.workflows_dir,
        workflow_id=workflow_id,
        content_hash=str(getattr(bundle, "content_hash", "")),
        agent_did=st.identity.did,
        union=union_of_legs(nodes, _tool_tags()),
    )


def _no_runner() -> str:
    return _errors(
        issue(
            field="run_id",
            error=(
                "no workflow runner is hosted in this process — run history is "
                "served by the fleet service that hosts the runner (COMP-009)"
            ),
        )
    )


# --- Hooks -----------------------------------------------------------------

# Tool name -> declared capability tags, captured from the capability registry
# at agent:ready. The activation check (COMP-016) resolves a tool node's legs
# through this map; without it a tool node contributes nothing statically and
# the runtime leg threader (COMP-015) is the only gate — still fail-closed, but
# the operator loses the pre-flight warning.
_tool_tags_map: dict[str, tuple[str, ...]] = {}


def _tool_tags() -> dict[str, tuple[str, ...]]:
    """The captured tool-name → capability-tags map."""
    return dict(_tool_tags_map)


@hook(event="agent:ready", priority=100)
async def workflows_capture_tool_tags(ctx: Any) -> None:
    """Capture the registry's tool→capability-tags map for the activation check."""
    data = ctx.data if hasattr(ctx, "data") else {}
    registry = data.get("skill_registry")
    tools = getattr(registry, "_tools", None)
    if not tools:
        return
    _tool_tags_map.clear()
    for name, entry in tools.items():
        _tool_tags_map[name] = tuple(getattr(entry, "capability_tags", ()) or ())


__all__ = [
    "workflow_add_node",
    "workflow_cancel_run",
    "workflow_create",
    "workflow_edit_node",
    "workflow_inspect",
    "workflow_list",
    "workflow_remove_node",
    "workflow_run",
    "workflow_run_status",
    "workflow_runs",
    "workflow_set_channel",
    "workflow_set_trigger",
    "workflows_capture_tool_tags",
]
