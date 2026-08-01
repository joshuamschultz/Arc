"""``arc workflow`` — ArcFlow operator CLI (SPEC-061 COMP-019).

``list | show | create | edit | archive | unarchive | purge | run | cancel``
delegate to the arcteam ``WorkflowControlPlane`` (COMP-021) — the single
shared operation set every surface (agent tools, this CLI, the dashboard)
invokes, so there is exactly one implementation of what a workflow edit or
run means (REQ-252/253/254). This module holds no validation, versioning,
or execution logic of its own; it authenticates the on-box operator,
translates argv to a control-plane call, and prints the result. Deleting
this file must never remove a workflow capability that the dashboard (or
agent conversation) also offers, and deleting the *dashboard* must never
remove one reachable here (REQ-257) — this command group is the proof.

``sign`` / ``verify`` are the one deliberate exception: signing NEVER
delegates to the control plane or any agent-reachable surface. The
operator signing key is resolved only in this process and never enters an
agent process (REQ-224) — mirrors ``arc approve``
(``arccli/commands/approve.py``) and ``arc blueprint sign``
(``arccli/commands/blueprint.py:318-343``): same operator-key resolution
(``arccli.commands.operator``), same ``.arcsig`` detached-sidecar
convention (``arcagent.capabilities.artifact_signing``), same
``arctrust.artifact`` primitives underneath.

Merge-reconciliation note (SPEC-061 concurrent build)
------------------------------------------------------
COMP-021 (``WorkflowControlPlane``) and COMP-005 (``DefinitionStore``, the
canonical-hash + manifest signer's input) are being implemented concurrently
in ``arcteam`` on a sibling branch. This module codes against the SDD's
documented contract (SDD.md COMP-005/COMP-021) via the two Protocols below
and resolves the real implementation *lazily by name* (``importlib``, never
a static ``from arcteam.workflows... import ...``) so:

1. mypy --strict does not fail resolving a submodule that does not exist yet
   in this checkout, and
2. this file runs (with a clear, fail-closed ``RuntimeError``) whether or
   not ``arcteam.workflows`` has landed.

At merge, reconcile:
  - ``_resolve_control_plane`` against the real
    ``arcteam.workflows.control_plane`` factory/class name and method
    signatures (currently assumed: ``build_workflow_control_plane() ->
    WorkflowControlPlane`` with the embedded async methods below).
  - ``_resolve_bundle_signer`` against the real ``arcteam.workflows.store``
    (COMP-005) factory — the canonicalize + manifest-hash + verify surface
    this module signs and verifies over.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from arccli.commands._shared import dispatch, err, print_json, print_table, write
from arccli.commands.operator import load_operator_key, operator_public_key

_NOT_AVAILABLE = (
    "arc workflow: {what} is not available in this build.\n"
    "  SPEC-061's arcteam engine has not landed in this checkout yet "
    "(see arccli.commands.workflow merge-reconciliation note).\n"
)


# ---------------------------------------------------------------------------
# Merge-reconciliation seam — arcteam COMP-005 / COMP-021 contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowVerifyResult:
    """Result of verifying a workflow bundle's ``.arcsig`` sidecar (COMP-005)."""

    valid: bool
    signer_did: str | None
    detail: str


@runtime_checkable
class WorkflowBundleSigner(Protocol):
    """Structural contract for arcteam's COMP-005 ``DefinitionStore``.

    Produces the exact bytes ``arc workflow sign`` signs and reports what
    ``arc workflow verify`` needs. Per SDD.md (Storage, signing, versioning):
    canonicalizes ``workflow.toml`` plus a sorted manifest of every
    referenced schema/prompt/script file (RFC 8785-style canonical JSON) —
    never raw TOML bytes, because formatters legitimately rewrite TOML
    (REQ-226).
    """

    def canonical_bytes(self, bundle_dir: Path) -> bytes:
        """Return the exact bytes that should be signed for this bundle."""
        ...

    def verify_signature(
        self, bundle_dir: Path, *, trusted_public_key: bytes | None
    ) -> WorkflowVerifyResult:
        """Re-verify the bundle's ``.arcsig`` sidecar, pinned to the operator key."""
        ...


@runtime_checkable
class WorkflowControlPlane(Protocol):
    """Structural contract for arcteam's COMP-021 ``WorkflowControlPlane``.

    "The single shared operation set for every workflow mutation and
    initiation ... Every caller invokes these same operations: the agent's
    builder tools, the operator command line, and the dashboard." (SDD.md
    COMP-021). Every method below is async because the real implementation
    talks to arcstore.
    """

    async def list_workflows(
        self, *, actor_did: str, include_archived: bool
    ) -> list[dict[str, Any]]: ...

    async def show(
        self, workflow_id: str, *, actor_did: str, version: int | None
    ) -> dict[str, Any]: ...

    async def create(self, *, definition_path: Path, actor_did: str) -> dict[str, Any]: ...

    async def edit(
        self,
        workflow_id: str,
        *,
        patch_path: Path,
        expected_version: int,
        actor_did: str,
        reason: str,
    ) -> dict[str, Any]: ...

    async def archive(self, workflow_id: str, *, actor_did: str) -> dict[str, Any]: ...

    async def unarchive(self, workflow_id: str, *, actor_did: str) -> dict[str, Any]: ...

    async def purge(self, workflow_id: str, *, actor_did: str, force: bool) -> dict[str, Any]: ...

    async def run(
        self, workflow_id: str, *, run_input: dict[str, Any], actor_did: str
    ) -> dict[str, Any]: ...

    async def cancel(self, run_id: str, *, actor_did: str) -> dict[str, Any]: ...


def _resolve_control_plane() -> WorkflowControlPlane:
    """Lazily resolve the real arcteam WorkflowControlPlane (COMP-021).

    Uses ``importlib`` by name (never a static import of the not-yet-landed
    submodule) so this module type-checks and imports cleanly before and
    after arcteam's SPEC-061 half merges. Tests inject a fake by
    monkeypatching this function, never the real module.
    """
    try:
        module = importlib.import_module("arcteam.workflows.control_plane")
    except ImportError as exc:
        raise RuntimeError(
            _NOT_AVAILABLE.format(what="the WorkflowControlPlane (COMP-021)")
        ) from exc
    factory = getattr(module, "build_workflow_control_plane", None)
    if factory is None:
        raise RuntimeError(
            "arc workflow: arcteam.workflows.control_plane has no "
            "build_workflow_control_plane() — reconcile arccli.commands.workflow "
            "against the landed arcteam API."
        )
    return cast(WorkflowControlPlane, factory())


def _resolve_bundle_signer() -> WorkflowBundleSigner:
    """Lazily resolve the real arcteam DefinitionStore/canonicalizer (COMP-005).

    Same lazy-by-name pattern as :func:`_resolve_control_plane` — see the
    module-level merge-reconciliation note.
    """
    try:
        module = importlib.import_module("arcteam.workflows.store")
    except ImportError as exc:
        raise RuntimeError(_NOT_AVAILABLE.format(what="the DefinitionStore (COMP-005)")) from exc
    factory = getattr(module, "build_definition_store", None)
    if factory is None:
        raise RuntimeError(
            "arc workflow: arcteam.workflows.store has no build_definition_store() "
            "— reconcile arccli.commands.workflow against the landed arcteam API."
        )
    return cast(WorkflowBundleSigner, factory())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _arc_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "config_dir", None) or Path.home() / ".arc").expanduser()


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


def _workflow_dir_and_toml(raw_path: str) -> tuple[Path, Path]:
    """Resolve ``<path>`` to ``(bundle_dir, workflow.toml)``.

    Accepts either the bundle directory or the ``workflow.toml`` file itself.
    """
    path = Path(raw_path).expanduser().resolve()
    toml_path = path / "workflow.toml" if path.is_dir() else path
    return toml_path.parent, toml_path


# ---------------------------------------------------------------------------
# sign / verify — COMP-019's one non-delegated responsibility (REQ-224)
# ---------------------------------------------------------------------------


def _sign(args: argparse.Namespace) -> None:
    """Operator-sign a workflow bundle: writes ``workflow.toml.arcsig``.

    Signing key resolution happens ONLY here, in this CLI process — it never
    enters an agent process (REQ-224), mirroring ``arc blueprint sign``
    (``arccli/commands/blueprint.py:318-343``) exactly: same
    ``load_operator_key``, same in-process-seed requirement (a vault_transit
    federal key has no in-process seed and must sign out-of-band), same
    ``operator:<pubkey-prefix>`` signer DID convention. This function never
    calls ``_resolve_control_plane`` — signing never touches the agent-shared
    control plane, by construction.
    """
    bundle_dir, toml_path = _workflow_dir_and_toml(args.path)
    if not toml_path.is_file():
        err(f"Error: workflow.toml not found: {toml_path}")
        sys.exit(1)

    try:
        signer_store = _resolve_bundle_signer()
        content = signer_store.canonical_bytes(bundle_dir)
    except RuntimeError as exc:
        err(str(exc))
        sys.exit(1)

    arc_dir = _arc_dir(args)
    operator = load_operator_key(arc_dir)
    # DC-4 known limitation (shared with `arc blueprint sign`): write_signature
    # needs the raw seed; a vault_transit (federal) operator key has no
    # in-process seed and must sign out-of-band.
    seed = getattr(operator, "seed", None)
    if not seed:
        err(
            "Error: the operator key has no in-process seed (vault_transit custody). "
            "`arc workflow sign` needs an in-process operator/author key; a vault-held "
            "federal key must sign out-of-band."
        )
        sys.exit(1)
    signer_did = f"operator:{operator.public_key.hex()[:16]}"

    from arcagent.capabilities.artifact_signing import write_signature

    sidecar = write_signature(toml_path, content, signer_did=signer_did, private_key=seed)
    write(f"Signed {toml_path.name} -> {sidecar.name}")


def _verify(args: argparse.Namespace) -> None:
    """Report a workflow bundle's signature validity, pinned to the operator key.

    Pinned (not TOFU): verification fails unless the bundle was signed by
    THIS deployment's operator key — an attacker who self-signs with a
    random keypair is refused (same posture as `arc blueprint verify`,
    SPEC-047 HIGH-1).
    """
    bundle_dir, toml_path = _workflow_dir_and_toml(args.path)
    if not toml_path.is_file():
        err(f"Error: workflow.toml not found: {toml_path}")
        sys.exit(1)

    try:
        signer_store = _resolve_bundle_signer()
    except RuntimeError as exc:
        err(str(exc))
        sys.exit(1)

    arc_dir = _arc_dir(args)
    trusted = operator_public_key(arc_dir)
    result = signer_store.verify_signature(bundle_dir, trusted_public_key=trusted)
    if result.valid:
        write(f"VALID — signed by {result.signer_did} (pinned operator key)")
    else:
        write(f"INVALID — {result.detail}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Non-signing operations — all delegate to WorkflowControlPlane (COMP-021)
# ---------------------------------------------------------------------------


def _row(item: dict[str, Any]) -> list[str]:
    return [
        str(item.get("id", "")),
        str(item.get("version", "")),
        str(item.get("status", "")),
        str(item.get("trigger", "manual")),
        str(item.get("last_run", "") or ""),
    ]


def _run_or_report(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run an async handler body, turning a not-yet-available control plane into a clean error."""
    try:
        asyncio.run(body())
    except RuntimeError as exc:
        err(str(exc))
        sys.exit(1)


def _list(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        rows = await plane.list_workflows(
            actor_did=_actor_did(_arc_dir(args)), include_archived=bool(args.all)
        )
        if not rows:
            write("No workflows.")
            return
        print_table(["ID", "VERSION", "STATUS", "TRIGGER", "LAST RUN"], [_row(r) for r in rows])

    _run_or_report(_run)


def _show(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        detail = await plane.show(
            args.id, actor_did=_actor_did(_arc_dir(args)), version=args.version
        )
        print_json(detail)

    _run_or_report(_run)


def _create(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        path = Path(args.path).expanduser().resolve()
        result = await plane.create(definition_path=path, actor_did=_actor_did(_arc_dir(args)))
        write(
            f"Created draft workflow {result.get('id')} v{result.get('version')} "
            f"(status={result.get('status', 'draft')})"
        )

    _run_or_report(_run)


def _edit(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        patch_path = Path(args.patch).expanduser().resolve()
        result = await plane.edit(
            args.id,
            patch_path=patch_path,
            expected_version=args.expected_version,
            actor_did=_actor_did(_arc_dir(args)),
            reason=args.reason or "",
        )
        write(
            f"Edited {args.id} -> v{result.get('version')} "
            f"(status={result.get('status', 'draft')})"
        )

    _run_or_report(_run)


def _archive(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        await plane.archive(args.id, actor_did=_actor_did(_arc_dir(args)))
        write(f"Archived {args.id}.")

    _run_or_report(_run)


def _unarchive(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        await plane.unarchive(args.id, actor_did=_actor_did(_arc_dir(args)))
        write(f"Unarchived {args.id} (status=draft).")

    _run_or_report(_run)


def _purge(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        result = await plane.purge(
            args.id, actor_did=_actor_did(_arc_dir(args)), force=bool(args.force)
        )
        if result.get("purged"):
            write(f"Purged {args.id}.")
        else:
            err(f"Refused: {result.get('reason', 'runs still reference this workflow')}")
            sys.exit(1)

    _run_or_report(_run)


def _run_workflow(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        run_input: dict[str, Any] = {}
        if args.input:
            run_input = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
        result = await plane.run(
            args.id, run_input=run_input, actor_did=_actor_did(_arc_dir(args))
        )
        write(f"Started run {result.get('run_id')} for {args.id} (status={result.get('status')})")

    _run_or_report(_run)


def _cancel(args: argparse.Namespace) -> None:
    async def _run() -> None:
        plane = _resolve_control_plane()
        result = await plane.cancel(args.run_id, actor_did=_actor_did(_arc_dir(args)))
        write(f"Cancelled {args.run_id} (status={result.get('status')}).")

    _run_or_report(_run)


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
    p.add_argument("--version", type=int, default=None)
    _add_dir_arg(p)

    p = subs.add_parser(
        "create", help="Create a workflow from a definition bundle (unsigned draft)."
    )
    p.add_argument("path", help="Path to the workflow bundle directory or workflow.toml.")
    _add_dir_arg(p)

    p = subs.add_parser("edit", help="Apply a patch to a workflow (optimistic concurrency).")
    p.add_argument("id")
    p.add_argument("--patch", required=True, help="Path to a JSON patch document.")
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
    _add_dir_arg(p)

    p = subs.add_parser("run", help="Start a run.")
    p.add_argument("id")
    p.add_argument("--input", default=None, help="Path to a JSON file with the run's typed input.")
    _add_dir_arg(p)

    p = subs.add_parser("cancel", help="Cancel a run.")
    p.add_argument("run_id")
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


__all__ = [
    "WorkflowBundleSigner",
    "WorkflowControlPlane",
    "WorkflowVerifyResult",
    "workflow_handler",
]
