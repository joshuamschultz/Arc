"""``arc prompt`` — view + edit/overwrite system prompts across all Arc packages.

Terminal parity with the arcui Prompts surface
(``arcui.routes.agent_detail.prompts``). Every Arc package ships stock system
prompts as markdown resources; an operator may override any of them with a
*signed* markdown overlay under ``<agent_root>/context/<package>/<name>.md`` plus
a detached ``<name>.md.arcsig`` sidecar. The agent's :class:`~arcprompt.PromptResolver`
re-verifies that signature at run start (:mod:`arcagent.core.prompt_context`), so
this CLI must sign exactly the same way arcui does — with the deployment operator
key. Holding that key IS operator authority on the local box (there is no session
token to read a role from, the ``arc task``/``arc approve`` convention).

Subcommands::

    arc prompt list  --agent <dir>
    arc prompt show  <package> <name> --agent <dir> [--effective | --stock]
    arc prompt diff  <package> <name> --agent <dir>
    arc prompt edit  <package> <name> --agent <dir> (--file <path> | --stdin)
    arc prompt reset <package> <name> --agent <dir>
    arc prompt history <package> <name> --agent <dir>
    arc prompt diff    <package> <name> --agent <dir> --from <ver|stock> [--to <ver|stock|current>]
    arc prompt revert  <package> <name> <version> --agent <dir>
    arc prompt sign-workspace --agent <dir>

Stock bodies are always read from the packaged resources
(:func:`arcprompt.load_stock_document`) — never from a user-supplied path. The
overlay tree lives at ``<agent_root>/context`` (the agent root, outside the
workspace subtree agent file tools are confined to), which is the only place an
override can be authored — by design (COMP-007).
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

import arcagent
from arcprompt import (
    PromptCatalog,
    PromptHistory,
    PromptMissing,
    PromptUnparseable,
    PromptUnsigned,
    PromptVersionMissing,
    SignatureVerifier,
    load_stock_document,
    parse_prompt,
    record_if_unseen,
    render_prompt,
)
from arctrust.artifact import content_sha256, sign_artifact
from arctrust.policy import read_agent_tier

from arccli.commands._shared import dispatch, err, print_table
from arccli.commands._shared import write as _out

_OVERLAY_DIRNAME = "context"
_SIDECAR_SUFFIX = ".arcsig"


# ---------------------------------------------------------------------------
# Shared resolution helpers
# ---------------------------------------------------------------------------


def _require_agent_root(args: argparse.Namespace) -> Path:
    """Resolve ``--agent`` to a validated agent-root directory, or exit 1.

    arccli has no notion of a "current agent" (its agent commands each take an
    explicit directory), so ``--agent`` is required rather than inferred.
    """
    raw: str | None = getattr(args, "agent", None)
    if not raw:
        err("arc prompt: --agent <agent-dir> is required (no current-agent default).")
        sys.exit(1)
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        err(f"arc prompt: agent directory not found: {root}")
        sys.exit(1)
    return root


def _confine(context_root: Path, package: str, name: str) -> Path | None:
    """Resolve the overlay path under ``context_root``; None if it escapes.

    Guards against a ``package``/``name`` that traverses out of the agent's
    ``context/`` root (path-injection). Mirrors the read-side confinement in
    ``arcui.routes.agent_detail.files_write._confine``.
    """
    if package.startswith("/") or name.startswith("/"):
        return None
    candidate = (context_root / package / f"{name}.md").resolve()
    try:
        candidate.relative_to(context_root.resolve())
    except ValueError:
        return None
    return candidate


def _operator_signer() -> tuple[str, bytes]:
    """Load the deployment operator key; return ``(signer_did, seed)`` or exit 1.

    The overlay is signed with the same operator key arcui's SigningAuthority uses
    and the agent's resolver pins, so the DID recorded here is the DID that
    verifies at run start. Absent/unreadable key → refuse (never an unsigned write,
    which the resolver would reject anyway).
    """
    from arctrust import OperatorKey, default_operator_key_path
    from arctrust.policy import OperatorApprovalAuthority

    try:
        op = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
    except (OSError, ValueError, RuntimeError) as exc:
        err(
            f"arc prompt: operator key unavailable ({type(exc).__name__}). "
            "Run 'arc init' to create the deployment operator key."
        )
        sys.exit(1)
    did = OperatorApprovalAuthority(op.into_signer()).did
    return did, op.seed


def _effective_body(agent_root: Path, package: str, name: str) -> str:
    """Overlay-resolved (signature-verified) body, or exit 1 on a load failure."""
    resolver = arcagent.build_prompt_resolver(
        agent_root / "arcagent.toml", read_agent_tier(agent_root)
    )
    try:
        return resolver.resolve(package, name).body
    except (PromptMissing, PromptUnsigned, PromptUnparseable) as exc:
        err(f"arc prompt: {exc}")
        sys.exit(1)


def _sign_and_record(
    agent_root: Path, package: str, name: str, overlay: Path, data: bytes
) -> tuple[str, str, int]:
    """Sign ``data``, write it as the live overlay and append it to the version history.

    The overlay that was live before is captured first, so a pre-history edit is
    never lost to this save. Returns ``(signer_did, sha256, version)``.
    """
    signer_did, seed = _operator_signer()
    signature = sign_artifact(data, signer_did=signer_did, private_key=seed)
    history = PromptHistory(agent_root, package, name)
    sidecar = Path(f"{overlay}{_SIDECAR_SUFFIX}")
    if overlay.is_file() and sidecar.is_file():
        record_if_unseen(history, overlay.read_bytes(), sidecar.read_text(encoding="utf-8"))
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(data)
    sidecar.write_text(signature.to_json(), encoding="utf-8")
    version = history.record(data, signature.to_json())
    return signer_did, signature.artifact_sha256, version.version


def _history_or_exit(agent_root: Path, package: str, name: str) -> PromptHistory:
    try:
        return PromptHistory(agent_root, package, name)
    except PromptMissing as exc:
        err(f"arc prompt: {exc}")
        sys.exit(1)


def _verifier(agent_root: Path) -> SignatureVerifier:
    """The verifier pinned to the same operator key the agent's resolver trusts."""
    resolver = arcagent.build_prompt_resolver(
        agent_root / "arcagent.toml", read_agent_tier(agent_root)
    )
    return resolver.verifier


def _read_body(args: argparse.Namespace) -> str:
    """Read the new prompt body from ``--stdin`` or ``--file``, or exit 1."""
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    file: str | None = getattr(args, "file", None)
    if file:
        try:
            return Path(file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            err(f"arc prompt: cannot read --file {file}: {exc}")
            sys.exit(1)
    err("arc prompt edit: provide the new body via --file <path> or --stdin.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> None:
    """List every packaged prompt, marked stock/overridden for this agent."""
    agent_root = _require_agent_root(args)
    context_root = agent_root / _OVERLAY_DIRNAME
    rows = [
        [
            ref.package,
            ref.name,
            "overridden" if (context_root / ref.package / f"{ref.name}.md").is_file() else "stock",
            ref.description,
        ]
        for ref in PromptCatalog().catalog()
    ]
    print_table(["PACKAGE", "NAME", "STATUS", "DESCRIPTION"], rows)


def _show(args: argparse.Namespace) -> None:
    """Print the stock body (``--stock``) or the effective overlay-resolved body."""
    package, name = args.package, args.name
    if args.stock:
        try:
            _out(load_stock_document(package, name).body)
        except PromptMissing as exc:
            err(f"arc prompt: {exc}")
            sys.exit(1)
        return
    agent_root = _require_agent_root(args)
    _out(_effective_body(agent_root, package, name))


def _version_body(
    agent_root: Path, history: PromptHistory, package: str, name: str, ref: str
) -> str:
    """Body for a diff side: ``stock``, ``current`` (effective) or a stored version."""
    if ref == "stock":
        return load_stock_document(package, name).body
    if ref == "current":
        return _effective_body(agent_root, package, name)
    number = history.resolve_ref(ref)
    data = history.read_verified(number, _verifier(agent_root))
    return parse_prompt(data, source="overlay").body


def _diff_versions(args: argparse.Namespace, agent_root: Path) -> None:
    """Print a unified diff between two stored versions (``--from`` / ``--to``)."""
    package, name = args.package, args.name
    history = _history_or_exit(agent_root, package, name)
    left_ref, right_ref = args.from_ref or "stock", args.to_ref or "current"
    try:
        left = _version_body(agent_root, history, package, name, left_ref)
        right = _version_body(agent_root, history, package, name, right_ref)
    except (PromptMissing, PromptUnsigned, PromptUnparseable, PromptVersionMissing) as exc:
        err(f"arc prompt: {exc}")
        sys.exit(1)
    diff = "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile=f"{left_ref}/{package}/{name}",
            tofile=f"{right_ref}/{package}/{name}",
        )
    )
    sys.stdout.write(diff if diff else f"No difference between {left_ref} and {right_ref}.\n")


def _history(args: argparse.Namespace) -> None:
    """List every signed version of an overlay, oldest first."""
    agent_root = _require_agent_root(args)
    versions = _history_or_exit(agent_root, args.package, args.name).versions()
    if not versions:
        _out(f"No saved versions for {args.package}/{args.name}.")
        return
    overlay = _confine(agent_root / _OVERLAY_DIRNAME, args.package, args.name)
    live = content_sha256(overlay.read_bytes()) if overlay and overlay.is_file() else None
    rows = [
        [
            str(v.version),
            v.signed_at or "",
            v.signer_did,
            v.sha256[:19],
            "current" if v.sha256 == live else "",
        ]
        for v in versions
    ]
    print_table(["VERSION", "SIGNED", "SIGNER", "SHA256", ""], rows)


def _revert(args: argparse.Namespace) -> None:
    """Re-sign an earlier stored version as a NEW version (operator action)."""
    agent_root = _require_agent_root(args)
    package, name = args.package, args.name
    overlay = _confine(agent_root / _OVERLAY_DIRNAME, package, name)
    if overlay is None:
        err(f"arc prompt: invalid prompt path {package}/{name} (escapes the context root).")
        sys.exit(1)
    history = _history_or_exit(agent_root, package, name)
    try:
        number = history.resolve_ref(args.version)
        data = history.read_verified(number, _verifier(agent_root))
        parse_prompt(data, source="overlay")
    except (PromptUnsigned, PromptUnparseable, PromptVersionMissing) as exc:
        err(f"arc prompt: {exc}")
        sys.exit(1)
    signer_did, sha256, new_version = _sign_and_record(agent_root, package, name, overlay, data)
    _out(f"Reverted {package}/{name} to version {number} as new version {new_version}.")
    _out(f"  signer: {signer_did}")
    _out(f"  sha256: {sha256}")


def _diff(args: argparse.Namespace) -> None:
    """Print a unified diff of stock vs the effective (overlay-resolved) body.

    With ``--from`` / ``--to`` it compares stored versions instead.
    """
    agent_root = _require_agent_root(args)
    if args.from_ref or args.to_ref:
        _diff_versions(args, agent_root)
        return
    package, name = args.package, args.name
    try:
        stock_body = load_stock_document(package, name).body
    except PromptMissing as exc:
        err(f"arc prompt: {exc}")
        sys.exit(1)
    effective_body = _effective_body(agent_root, package, name)
    diff = "".join(
        difflib.unified_diff(
            stock_body.splitlines(keepends=True),
            effective_body.splitlines(keepends=True),
            fromfile=f"stock/{package}/{name}",
            tofile=f"effective/{package}/{name}",
        )
    )
    if not diff:
        _out(f"No override for {package}/{name} — effective is identical to stock.")
        return
    sys.stdout.write(diff if diff.endswith("\n") else diff + "\n")


def _edit(args: argparse.Namespace) -> None:
    """Author + operator-sign an overlay from a supplied body (operator action)."""
    agent_root = _require_agent_root(args)
    package, name = args.package, args.name

    try:
        stock_doc = load_stock_document(package, name)
    except PromptMissing as exc:
        err(f"arc prompt: {exc}")
        sys.exit(1)

    overlay = _confine(agent_root / _OVERLAY_DIRNAME, package, name)
    if overlay is None:
        err(f"arc prompt: invalid prompt path {package}/{name} (escapes the context root).")
        sys.exit(1)

    body = _read_body(args)
    secret_type = arcagent.find_secret(body)
    if secret_type is not None:
        err(
            f"arc prompt: refusing to override {package}/{name}: content looks like a live "
            f"credential ({secret_type}). Credentials never touch the filesystem."
        )
        sys.exit(1)

    overlay_bytes = render_prompt(body, name=name, description=stock_doc.description)
    signer_did, sha256, version = _sign_and_record(
        agent_root, package, name, overlay, overlay_bytes
    )

    _out(f"Override saved for {package}/{name}. It takes effect on the agent's next run.")
    _out(f"  version: {version}")
    _out(f"  signer: {signer_did}")
    _out(f"  sha256: {sha256}")


def _reset(args: argparse.Namespace) -> None:
    """Remove the overlay + its ``.arcsig`` so the prompt resolves to stock again."""
    agent_root = _require_agent_root(args)
    package, name = args.package, args.name
    overlay = _confine(agent_root / _OVERLAY_DIRNAME, package, name)
    if overlay is None:
        err(f"arc prompt: invalid prompt path {package}/{name} (escapes the context root).")
        sys.exit(1)
    if not overlay.is_file():
        err(f"arc prompt: no override to reset for {package}/{name}.")
        sys.exit(1)
    overlay.unlink()
    Path(f"{overlay}{_SIDECAR_SUFFIX}").unlink(missing_ok=True)
    _out(f"Override removed for {package}/{name}. Resolves to stock on the agent's next run.")


def _sign_workspace(args: argparse.Namespace) -> None:
    """Sign the agent's identity.md / policy_pinned.md with the operator key.

    The one-time step for an agent that predates signed workspace documents, and
    the way to re-sign after editing one of them by hand on the box.
    """
    import arcagent

    from arccli.commands.agent._common import cli_operator_signing

    agent_root = _require_agent_root(args)
    operator = cli_operator_signing()
    if operator is None:
        err("arc prompt: operator key unavailable; run 'arc init' first.")
        sys.exit(1)
    signed = arcagent.scaffold.sign_workspace_documents(agent_root, operator)
    if not signed:
        _out("Nothing to sign: no identity.md or policy_pinned.md in the agent workspace.")
        return
    for name in signed:
        _out(f"Signed {name}. It takes effect on the agent's next run.")


# ---------------------------------------------------------------------------
# Parser + dispatch
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc prompt",
        description="View + edit/overwrite editable system prompts across all Arc packages.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    list_p = subs.add_parser("list", help="List every prompt, marked stock/overridden.")
    list_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")

    show_p = subs.add_parser("show", help="Print the stock or effective prompt body.")
    show_p.add_argument("package")
    show_p.add_argument("name")
    show_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")
    mode = show_p.add_mutually_exclusive_group()
    mode.add_argument(
        "--effective",
        action="store_true",
        help="Overlay-resolved body (default; requires --agent).",
    )
    mode.add_argument("--stock", action="store_true", help="Packaged stock body (no --agent).")

    diff_p = subs.add_parser("diff", help="Unified diff of stock vs effective body.")
    diff_p.add_argument("package")
    diff_p.add_argument("name")
    diff_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")
    diff_p.add_argument(
        "--from",
        dest="from_ref",
        metavar="<ver|stock>",
        help="Compare stored versions: the older side (version, sha prefix, stock, current).",
    )
    diff_p.add_argument(
        "--to",
        dest="to_ref",
        metavar="<ver|current>",
        help="Compare stored versions: the newer side (default: current).",
    )

    history_p = subs.add_parser("history", help="List every signed saved version of an override.")
    history_p.add_argument("package")
    history_p.add_argument("name")
    history_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")

    revert_p = subs.add_parser("revert", help="Re-sign an earlier version as a new version.")
    revert_p.add_argument("package")
    revert_p.add_argument("name")
    revert_p.add_argument("version", help="Version number or sha256 prefix from 'history'.")
    revert_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")

    edit_p = subs.add_parser("edit", help="Author + sign an operator overlay from a body.")
    edit_p.add_argument("package")
    edit_p.add_argument("name")
    edit_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")
    src = edit_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", metavar="<path>", help="Read the new body from this file.")
    src.add_argument("--stdin", action="store_true", help="Read the new body from stdin.")

    reset_p = subs.add_parser("reset", help="Remove the overlay; resolve to stock next run.")
    reset_p.add_argument("package")
    reset_p.add_argument("name")
    reset_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")

    sign_p = subs.add_parser(
        "sign-workspace", help="Sign identity.md / policy_pinned.md with the operator key."
    )
    sign_p.add_argument("--agent", metavar="<dir>", help="Agent root directory to target.")

    return parser


_SUBCOMMANDS = {
    "list": _list,
    "show": _show,
    "diff": _diff,
    "edit": _edit,
    "reset": _reset,
    "history": _history,
    "revert": _revert,
    "sign-workspace": _sign_workspace,
}


def prompt_handler(args: list[str]) -> None:
    """Entry point for ``arc prompt <subcommand>`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMANDS, args)


__all__ = ["prompt_handler"]
