"""``arc workflow`` — ArcFlow operator CLI (SPEC-061 COMP-019).

``create | edit | archive | unarchive | purge | run | cancel`` delegate to the
arcteam :class:`~arcteam.workflow.control_plane.WorkflowControlPlane`
(COMP-021) — the single shared operation set every surface (agent tools, this
CLI, the dashboard) invokes, so there is exactly one implementation of what a
workflow mutation or initiation means (REQ-252/253/254). ``list`` and ``show``
read the :class:`~arcteam.workflow.store.DefinitionStore` (COMP-005) directly:
they are neither a mutation nor an initiation, so they are not control-plane
operations and inventing plane methods for them would fork the operation set.

``sign`` / ``verify`` are the one deliberate exception to delegation: signing
NEVER goes through the control plane or any agent-reachable surface. The
operator signing key is resolved only in this process and never enters an
agent process (REQ-224) — mirrors ``arc approve``
(``arccli/commands/approve.py``) and ``arc blueprint sign``
(``arccli/commands/blueprint.py:318-343``): same operator-key resolution
(``arccli.commands.operator``), same ``.arcsig`` detached-sidecar convention.
``sign`` calls :func:`~arcteam.workflow.store.sign_definition`, which is the
only path in the system to ``status="signed"``.

Everything arcteam exposes is imported statically and by name. A previous
revision resolved these lazily, by string, against a guessed module path that
no type-checker or linter can read. The guessed path was misspelled, so the
whole command group was dead in every build while every test passed against
injected fakes. Static imports of the real symbols are what turn a wrong name
into a check-time failure instead of a runtime one, so they must stay static —
``tests/test_workflow_command.py`` asserts the by-string lookup does not return.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from arcteam.workflow import (
    DefinitionStore,
    WorkflowAuditHook,
    WorkflowBundle,
    WorkflowDefinition,
    WorkflowError,
    confine,
    parse_definition,
    referenced_files,
    sign_definition,
    validate_definition,
)
from arcteam.workflow.control_plane import ControlPlaneResult, OperationIssue, WorkflowControlPlane
from arcteam.workflow.runner_contracts import Tier, ValidationIssueLike
from arctrust import WormSink
from arctrust.audit import AuditEvent, AuditSink, emit

from arccli.commands._shared import dispatch, err, print_json, print_table, write
from arccli.commands.operator import load_operator_key, operator_key_path, operator_public_key

# ---------------------------------------------------------------------------
# Deployment context — tier, operator key, audit sink
# ---------------------------------------------------------------------------


def _arc_dir(args: argparse.Namespace) -> Path:
    """``--dir`` if given, else the deployment's config dir (``$ARC_CONFIG_DIR``).

    Must resolve to the SAME directory ``build_workflow_runner`` derives its
    workspace from — the runner reads ``<config dir>/workflows``. A CLI that
    hardcoded ``~/.arc`` while the deployment ran under ``ARC_CONFIG_DIR``
    would sign bundles into a directory the engine never reads: both halves
    look healthy and no workflow is ever signed, which is the silent shape
    this feature has produced repeatedly.
    """
    from arcteam.config import default_config_dir

    override = getattr(args, "config_dir", None)
    return Path(override).expanduser() if override else default_config_dir()


def _deployment_tier(arc_dir: Path) -> Tier:
    """The deployment's baseline tier, read from ``<arc_dir>/arcagent.toml``.

    Handed to the store and the control plane at CONSTRUCTION, never guessed
    per call, so the gate that refuses an unsigned workflow and the audit event
    that records the refusal can never disagree about the posture.
    """
    path = arc_dir / "arcagent.toml"
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return "personal"
        tier = raw.get("security", {}).get("tier")
        if tier in ("personal", "enterprise", "federal"):
            return cast(Tier, tier)
    return "personal"


def _actor_did(arc_dir: Path) -> str:
    """The operator's own DID, stamped as ``actor_did`` on every non-signing op.

    Resolved from the same on-disk operator key every other CLI operator
    surface uses (``arc approve``, ``arc blueprint sign``) — never a
    per-invocation or agent identity. Bootstraps the key (zero-config) if
    absent, exactly like ``load_operator_key`` does elsewhere, so a first-run
    operator still gets a stable DID for CLI-attributed audit events.
    """
    from arctrust.identity import did_from_public_key

    pub = operator_public_key(arc_dir) or load_operator_key(arc_dir).public_key
    return did_from_public_key(pub, org="local", agent_type="operator")


def _audit_sink() -> WormSink:
    """The operator-signed WORM sink every workflow lifecycle event lands in.

    The same chain ``arc task`` writes mutable-plane events to (``arcstore``'s
    data dir, not the config dir). Without it a signed definition, an archive,
    and a forced purge would all be unrecorded — the AU-9/AU-10 hole that
    "just construct the object" wiring quietly leaves.

    The caller MUST close it: the sink holds an exclusive ``flock`` for its
    lifetime, so an unclosed one locks every later writer out of the chain.
    """
    from arcstore import resolve_data_dir
    from arcstore.ingest import WORM_ACTIVE_FILENAME

    from arccli.commands.operator import resolve_operator_signer

    worm_dir = resolve_data_dir(None) / "worm"
    worm_dir.mkdir(parents=True, exist_ok=True)
    return WormSink(worm_dir / WORM_ACTIVE_FILENAME, resolve_operator_signer())


def _store_audit_hook(sink: AuditSink, tier: Tier) -> WorkflowAuditHook:
    """Adapt the store's ``(event, payload)`` hook onto ``arctrust.audit.emit``.

    The store guarantees an event per operation but deliberately owns no sink
    (``arcteam.workflow.store.WorkflowAuditHook``); this is where the CLI's
    sink gets wired to it.
    """

    def _hook(event: str, payload: dict[str, Any]) -> None:
        emit(
            AuditEvent(
                actor_did=str(payload.get("actor_did") or "did:arc:local:operator"),
                action=event,
                target=str(payload.get("workflow_id") or ""),
                outcome="ok",
                tier=tier,
                extra=payload,
            ),
            sink,
        )

    return _hook


# ---------------------------------------------------------------------------
# The two arcteam seams
# ---------------------------------------------------------------------------


def _resolve_bundle_signer(
    root: Path, *, tier: Tier = "personal", sink: AuditSink | None = None
) -> DefinitionStore:
    """The real arcteam ``DefinitionStore`` (COMP-005) rooted at ``root``.

    ``root`` is the directory that CONTAINS bundles, not a bundle: a bundle
    lives at ``<root>/<workflow_id>/``. ``operator_public_key`` is what pins
    verification — a bundle signed by any other key reads as unsigned, which is
    the whole point of pinning (SPEC-047 HIGH-1).
    """
    return DefinitionStore(
        root,
        tier=tier,
        operator_public_key=operator_public_key(root.parent),
        audit=_store_audit_hook(sink, tier) if sink is not None else None,
    )


#: The messaging substrate the AGENTS are on — the same variable and default
#: ``arc team`` and the gateway's runner host use. A CLI that resolved node
#: owners against a different bus would resolve none of them.
_NATS_URL_ENV = "ARCTEAM_NATS_URL"


async def _team_bindings(arc_dir: Path) -> tuple[Any, Any]:
    """Owner resolution and narration for the runs THIS process starts.

    Without them ``arc workflow run`` prints "Started run …" and the run is
    already failed: no ``@handle`` resolves, so the first node dies with
    "unknown agent" — a refusal that looks like a start. The gateway's runner
    host wires the same two bindings for the same reason; the operator surface
    starts runs too, so it needs them just as much.

    Degrades to ``(None, None)`` with a warning rather than refusing: an
    operator must still be able to author, sign, and inspect on a box where the
    team bus is down.
    """
    import os

    from arcagent.core.arcteam_bootstrap import make_backend
    from arcteam.workflow.identity import RunnerIdentity
    from arcteam.workflow.stores import build_team_bindings

    from arccli.commands.operator import resolve_operator_signer

    try:
        return await build_team_bindings(
            backend=await make_backend(os.environ.get(_NATS_URL_ENV, "nats://127.0.0.1:4222")),
            operator_signer=resolve_operator_signer(),
            identity=RunnerIdentity.load(operator_key_path(arc_dir)),
        )
    except Exception as exc:  # reason: authoring must survive a down team bus
        err(f"Warning: team bindings unavailable ({exc}); runs cannot resolve node owners")
        return None, None


async def _resolve_control_plane(
    arc_dir: Path, *, tier: Tier = "personal"
) -> tuple[WorkflowControlPlane, Callable[[], Coroutine[Any, Any, None]]]:
    """Build the real ``WorkflowControlPlane`` (COMP-021) and its closer.

    Constructed here at the call site rather than behind an arcteam factory:
    shaping a library's constructor around whichever caller imported it first
    is how the wrong abstraction gets locked in. arcteam exposes the class and
    its dependencies; naming them is this module's job.

    The purge guard's run count is genuinely wired — ``WorkflowRunStore``
    counts through ``arcstore.runs.RunStore.list(workflow_id=...)``, so
    ``purge`` refuses on real outstanding runs rather than on a stub that
    always reports zero.

    Returns:
        The plane and an ``aclose`` coroutine factory that releases the sqlite
        backend; a CLI process must not leak the handle between subcommands.
    """
    from arcstore import store_db_path
    from arcstore.backends.sqlite import SqliteBackend
    from arcteam.workflow.runner import build_workflow_runner
    from arcteam.workflow.stores import WorkflowRunStore

    backend = SqliteBackend(store_db_path(None))
    await backend.start()
    sink = _audit_sink()
    owners, narrator = await _team_bindings(arc_dir)
    runner = build_workflow_runner(
        tier=tier,
        task_store_backend=backend,
        runner_key_path=operator_key_path(arc_dir),
        workspace_root=arc_dir,
        operator_public_key=operator_public_key(arc_dir),
        audit_sink=sink,
        registry=owners,
        narrator=narrator,
    )
    plane = WorkflowControlPlane(
        definitions=_resolve_bundle_signer(_workflows_root(arc_dir), tier=tier, sink=sink),
        parse=_parse,
        validate=_validator(_workflows_root(arc_dir)),
        runner=runner,
        runs=WorkflowRunStore(backend, sink=sink),
        tier=tier,
        audit_sink=sink,
    )

    async def _aclose() -> None:
        await runner.aclose()
        await backend.stop()
        sink.close()

    return plane, _aclose


def _parse(document: Mapping[str, Any]) -> WorkflowDefinition:
    """``parse_definition`` widened to the control plane's ``Mapping`` seam.

    COMP-021 hands its parser a ``Mapping``; ``parse_definition`` is annotated
    for a ``dict``. It only reads, so the copy is the whole adaptation.
    """
    return parse_definition(dict(document))


def _validator(root: Path) -> Callable[..., Sequence[ValidationIssueLike]]:
    """``validate_definition`` bound to the bundle root, as COMP-021 expects.

    The control plane's validator seam is ``(definition, *, pending_files)``;
    the real validator additionally needs the bundle root to resolve and
    confine file references. Binding it here keeps that knowledge with the
    caller that owns the workspace layout.
    """

    def _validate(
        definition: Any, *, pending_files: frozenset[str] = frozenset()
    ) -> Sequence[ValidationIssueLike]:
        return validate_definition(
            definition,
            bundle_root=root / definition.id,
            pending_files=pending_files,
        )

    return _validate


def _workflows_root(arc_dir: Path) -> Path:
    """Where bundles live: ``<arc_dir>/workflows``.

    Must match what ``build_workflow_runner`` derives from its key path
    (``workspace_root / "workflows"``) — a CLI that signs into a different
    directory than the runner reads would leave every definition looking
    unsigned to the engine.
    """
    return arc_dir / "workflows"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _workflow_dir_and_toml(raw_path: str) -> tuple[Path, Path]:
    """Resolve ``<path>`` to ``(bundle_dir, workflow.toml)``.

    Accepts either the bundle directory or the ``workflow.toml`` file itself.
    """
    path = Path(raw_path).expanduser().resolve()
    toml_path = path / "workflow.toml" if path.is_dir() else path
    return toml_path.parent, toml_path


def _read_bundle(raw_path: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read a bundle from disk as ``(document, companion files)``.

    Companion prompts, schemas, and scripts travel WITH the document into the
    control plane rather than being written beside it beforehand, so the store
    validates the whole graph before committing any of those bytes.

    A reference that escapes the bundle is refused here as well as in the
    store: reading bytes from outside the bundle in order to hand them to the
    store would already be the exfiltration the confinement check exists to
    prevent.
    """
    bundle_dir, toml_path = _workflow_dir_and_toml(raw_path)
    if not toml_path.is_file():
        err(f"Error: workflow.toml not found: {toml_path}")
        sys.exit(1)
    try:
        document: dict[str, Any] = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        err(f"Error: {toml_path} is not valid TOML: {exc}")
        sys.exit(1)

    try:
        definition = parse_definition(document)
    except Exception:  # reason: the control plane reports typed parse issues; don't pre-empt it
        return document, {}

    files: dict[str, bytes] = {}
    root = bundle_dir.resolve()
    for reference in referenced_files(definition):
        target = confine(root, reference)
        if target is None:
            err(f"Error: {reference!r} escapes the workflow bundle; refusing to read it")
            sys.exit(1)
        if target.is_file():
            files[reference] = target.read_bytes()
    return document, files


def _issue_line(issue: ValidationIssueLike | OperationIssue) -> str:
    where = ".".join(part for part in (issue.node_id, issue.field) if part)
    return f"{where}: {issue.error}" if where else issue.error


def _ok_or_exit(result: ControlPlaneResult) -> ControlPlaneResult:
    """Relay a refusal as typed, repairable lines and exit 1; else pass it through."""
    if result.ok:
        return result
    for issue in result.errors:
        err(f"  {_issue_line(issue)}")
    sys.exit(1)


def _bundle_row(bundle: WorkflowBundle) -> list[str]:
    trigger = bundle.effective_trigger
    return [
        bundle.definition.id,
        str(bundle.definition.version),
        bundle.status,
        trigger.type if trigger is not None else "manual",
        bundle.signer_did or "-",
    ]


def _run_or_report(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run an async handler, turning an arcteam refusal into a clean CLI error."""
    try:
        asyncio.run(body())
    except (WorkflowError, RuntimeError) as exc:
        err(f"Error: {exc}")
        sys.exit(1)


def _with_plane(
    args: argparse.Namespace,
    body: Callable[[WorkflowControlPlane, str], Coroutine[Any, Any, None]],
) -> None:
    """Construct the plane, run ``body(plane, actor_did)``, always close the backend."""
    arc_dir = _arc_dir(args)

    async def _run() -> None:
        # Resolve the actor FIRST: it bootstraps the operator key when absent,
        # and the runner's own identity (RunnerIdentity) loads that same key
        # fail-closed without ever generating one. Building the plane first
        # makes `arc workflow create` refuse on a fresh box — a required setup
        # step, which zero-config forbids.
        actor_did = _actor_did(arc_dir)
        plane, aclose = await _resolve_control_plane(arc_dir, tier=_deployment_tier(arc_dir))
        try:
            await body(plane, actor_did)
        finally:
            await aclose()

    _run_or_report(_run)


def _store(args: argparse.Namespace) -> DefinitionStore:
    """The read-side definition store for ``list`` / ``show`` (no audit writes)."""
    arc_dir = _arc_dir(args)
    return _resolve_bundle_signer(_workflows_root(arc_dir), tier=_deployment_tier(arc_dir))


# ---------------------------------------------------------------------------
# sign / verify — COMP-019's one non-delegated responsibility (REQ-224)
# ---------------------------------------------------------------------------


def _sign(args: argparse.Namespace) -> None:
    """Operator-sign a workflow bundle: writes ``workflow.toml.arcsig``.

    Signing key resolution happens ONLY here, in this CLI process — it never
    enters an agent process (REQ-224), mirroring ``arc blueprint sign``
    exactly: same ``load_operator_key``, same in-process-seed requirement (a
    vault_transit federal key has no in-process seed and must sign
    out-of-band), same ``operator:<pubkey-prefix>`` signer DID convention.
    This function never resolves the control plane — signing never touches the
    agent-shared operation set, by construction.

    The bundle is addressed as ``<parent>/<id>`` so an operator can sign a
    bundle wherever it sits, not only under ``~/.arc/workflows``.
    """
    bundle_dir, toml_path = _workflow_dir_and_toml(args.path)
    if not toml_path.is_file():
        err(f"Error: workflow.toml not found: {toml_path}")
        sys.exit(1)

    arc_dir = _arc_dir(args)
    operator = load_operator_key(arc_dir)
    # DC-4 known limitation (shared with `arc blueprint sign`): signing needs the
    # raw seed; a vault_transit (federal) operator key has no in-process seed and
    # must sign out-of-band.
    seed = getattr(operator, "seed", None)
    if not seed:
        err(
            "Error: the operator key has no in-process seed (vault_transit custody). "
            "`arc workflow sign` needs an in-process operator/author key; a vault-held "
            "federal key must sign out-of-band."
        )
        sys.exit(1)

    sink = _audit_sink()
    try:
        bundle = sign_definition(
            _resolve_bundle_signer(bundle_dir.parent, tier=_deployment_tier(arc_dir), sink=sink),
            bundle_dir.name,
            signer_did=f"operator:{operator.public_key.hex()[:16]}",
            private_key=seed,
        )
    except WorkflowError as exc:
        err(f"Error: {exc}")
        sys.exit(1)
    finally:
        sink.close()
    write(f"Signed {toml_path.name} -> workflow.toml.arcsig (status={bundle.status})")


def _verify(args: argparse.Namespace) -> None:
    """Report a workflow bundle's signature validity, pinned to the operator key.

    Pinned (not TOFU): ``is_verified`` is true only when the sidecar verifies
    against THIS deployment's operator key, so an attacker who self-signs with
    a random keypair is refused (same posture as `arc blueprint verify`,
    SPEC-047 HIGH-1). Trust is read from ``is_verified``, never from
    ``status`` — an archived bundle can still be validly signed.
    """
    bundle_dir, toml_path = _workflow_dir_and_toml(args.path)
    if not toml_path.is_file():
        err(f"Error: workflow.toml not found: {toml_path}")
        sys.exit(1)

    arc_dir = _arc_dir(args)
    store = _resolve_bundle_signer(bundle_dir.parent, tier=_deployment_tier(arc_dir))
    try:
        bundle = store.load(bundle_dir.name)
    except WorkflowError as exc:
        err(f"Error: {exc}")
        sys.exit(1)
    if bundle.is_verified:
        write(f"VALID — signed by {bundle.signer_did} (pinned operator key)")
        return
    write("INVALID — unsigned, or not signed by this deployment's operator key")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Reads — the definition store (COMP-005); neither mutation nor initiation
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    store = _store(args)
    rows = [_bundle_row(store.load(wid)) for wid in store.list_ids(include_archived=args.all)]
    if not rows:
        write("No workflows.")
        return
    print_table(["ID", "VERSION", "STATUS", "TRIGGER", "SIGNER"], rows)


def _show(args: argparse.Namespace) -> None:
    store = _store(args)
    try:
        if args.version is not None:
            print_json(store.load_version(args.id, args.version).to_document())
            return
        bundle = store.load(args.id)
    except WorkflowError as exc:
        err(f"Error: {exc}")
        sys.exit(1)
    print_json(
        {
            "id": bundle.definition.id,
            "version": bundle.definition.version,
            "status": bundle.status,
            "is_verified": bundle.is_verified,
            "signer_did": bundle.signer_did,
            "content_hash": bundle.content_hash,
            "versions": list(store.versions(args.id)),
            "definition": bundle.definition.to_document(),
        }
    )


# ---------------------------------------------------------------------------
# Mutations and initiation — all delegate to WorkflowControlPlane (COMP-021)
# ---------------------------------------------------------------------------


def _create(args: argparse.Namespace) -> None:
    document, files = _read_bundle(args.path)

    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        result = _ok_or_exit(await plane.create(document, actor_did=actor_did, files=files))
        bundle = result.bundle
        assert bundle is not None  # noqa: S101 — ok=True always carries the bundle
        write(
            f"Created draft workflow {bundle.definition.id} "
            f"v{bundle.definition.version} (status={bundle.status})"
        )

    _with_plane(args, _run)


def _edit(args: argparse.Namespace) -> None:
    document, files = _read_bundle(args.document)

    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        result = _ok_or_exit(
            await plane.edit(
                args.id,
                document,
                expected_version=args.expected_version,
                actor_did=actor_did,
                reason=args.reason or "",
                files=files,
            )
        )
        bundle = result.bundle
        assert bundle is not None  # noqa: S101 — ok=True always carries the bundle
        write(f"Edited {args.id} -> v{bundle.definition.version} (status={bundle.status})")

    _with_plane(args, _run)


def _archive(args: argparse.Namespace) -> None:
    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        _ok_or_exit(await plane.archive(args.id, actor_did=actor_did))
        write(f"Archived {args.id}.")

    _with_plane(args, _run)


def _unarchive(args: argparse.Namespace) -> None:
    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        _ok_or_exit(await plane.unarchive(args.id, actor_did=actor_did))
        write(f"Unarchived {args.id} (status=draft).")

    _with_plane(args, _run)


def _purge(args: argparse.Namespace) -> None:
    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        _ok_or_exit(
            await plane.purge(
                args.id,
                actor_did=actor_did,
                force=bool(args.force),
                reason=args.reason or "",
            )
        )
        write(f"Purged {args.id}.")

    _with_plane(args, _run)


def _run_workflow(args: argparse.Namespace) -> None:
    run_input: Mapping[str, Any] = {}
    if args.input:
        run_input = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))

    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        result = _ok_or_exit(await plane.run(args.id, input=run_input, actor_did=actor_did))
        record = result.run
        assert record is not None  # noqa: S101 — ok=True always carries the run
        write(f"Started run {record.run_id} for {args.id} (status={record.status})")

    _with_plane(args, _run)


def _cancel(args: argparse.Namespace) -> None:
    async def _run(plane: WorkflowControlPlane, actor_did: str) -> None:
        result = _ok_or_exit(
            await plane.cancel(
                args.run_id, actor_did=actor_did, reason=args.reason or "cancelled by operator"
            )
        )
        record = result.run
        assert record is not None  # noqa: S101 — ok=True always carries the run
        write(f"Cancelled {args.run_id} (status={record.status}).")

    _with_plane(args, _run)


# ---------------------------------------------------------------------------
# Argparse dispatcher
# ---------------------------------------------------------------------------


def _add_dir_arg(p: argparse.ArgumentParser) -> None:
    """``--dir`` on each subparser (not the group parser) so it can follow the subcommand."""
    p.add_argument("--dir", dest="config_dir", default=None, help="Config dir (default: ~/.arc).")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc workflow",
        description="ArcFlow — named, signed, conversationally-authored workflows.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p = subs.add_parser("list", help="List workflows.")
    p.add_argument("--all", action="store_true", help="Include archived workflows.")
    _add_dir_arg(p)

    p = subs.add_parser("show", help="Show a workflow's definition and status.")
    p.add_argument("id")
    p.add_argument("--version", type=int, default=None, help="Show a retained prior revision.")
    _add_dir_arg(p)

    p = subs.add_parser(
        "create", help="Create a workflow from a definition bundle (unsigned draft)."
    )
    p.add_argument("path", help="Path to the workflow bundle directory or workflow.toml.")
    _add_dir_arg(p)

    p = subs.add_parser("edit", help="Revise a workflow (optimistic concurrency).")
    p.add_argument("id")
    p.add_argument(
        "--document",
        required=True,
        help="Path to the revised bundle directory or workflow.toml.",
    )
    p.add_argument("--expected-version", dest="expected_version", type=int, required=True)
    p.add_argument("--reason", default=None)
    _add_dir_arg(p)

    p = subs.add_parser(
        "archive", help="Archive a workflow (hides + disables trigger; history retained)."
    )
    p.add_argument("id")
    _add_dir_arg(p)

    p = subs.add_parser("unarchive", help="Restore an archived workflow as a draft.")
    p.add_argument("id")
    _add_dir_arg(p)

    p = subs.add_parser(
        "purge", help="Permanently purge a workflow (refuses while runs reference it)."
    )
    p.add_argument("id")
    p.add_argument(
        "--force", action="store_true", help="Force purge; audits unrenderable history."
    )
    p.add_argument("--reason", default=None)
    _add_dir_arg(p)

    p = subs.add_parser("run", help="Start a run.")
    p.add_argument("id")
    p.add_argument("--input", default=None, help="Path to a JSON file with the run's typed input.")
    _add_dir_arg(p)

    p = subs.add_parser("cancel", help="Cancel a run.")
    p.add_argument("run_id")
    p.add_argument("--reason", default=None)
    _add_dir_arg(p)

    p = subs.add_parser("sign", help="Operator-sign a workflow bundle (writes .arcsig sidecar).")
    p.add_argument("path")
    _add_dir_arg(p)

    p = subs.add_parser(
        "verify", help="Verify a workflow bundle's signature against the pinned operator key."
    )
    p.add_argument("path")
    _add_dir_arg(p)

    return parser


_SUBCOMMAND_MAP: dict[str, Callable[[argparse.Namespace], None]] = {
    "list": _list,
    "show": _show,
    "create": _create,
    "edit": _edit,
    "archive": _archive,
    "unarchive": _unarchive,
    "purge": _purge,
    "run": _run_workflow,
    "cancel": _cancel,
    "sign": _sign,
    "verify": _verify,
}


def workflow_handler(args: list[str]) -> None:
    """Top-level handler for ``arc workflow <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["workflow_handler"]
