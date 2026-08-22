"""``arc capability-import`` — inspect, trust, and revoke staged reviews.

Promotion and revocation are explicit operator trust mutations. They use the
deployment operator signer and the agent-local ``arcagent.toml`` trust store;
staged source is never executed by this command.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import arcagent
from arcgateway import team_roster
from arctrust import Signer, SignerError
from arctrust.policy import OperatorApprovalAuthority

from arccli.commands._shared import audit_chain, dispatch, print_table, write
from arccli.commands.trust import _resolve_agent

_ARCHIVE_CHUNK_SIZE = 64 * 1024


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


def _staging(agent_root: Path, import_id: str) -> Path:
    if len(import_id) != 64 or any(char not in "0123456789abcdef" for char in import_id):
        raise ValueError("import id must be a 64-character hexadecimal digest")
    path = agent_root / "capabilities" / "imports" / ".staging" / import_id
    if not path.is_dir():
        raise ValueError(f"capability import {import_id!r} was not found")
    return path


def _team_root() -> Path:
    candidate = Path.cwd() / "team"
    return candidate if candidate.is_dir() else Path.cwd()


def _resolve_target(agent_arg: str | None) -> tuple[str, Path, str]:
    agent_id, agent_root, _label = _resolve_agent(agent_arg)
    entry = next(
        entry
        for entry in team_roster.list_team(team_root=_team_root(), online_ids=set())
        if entry.agent_id == agent_id
    )
    return agent_id, agent_root, str(entry.did)


def _copy_stdin_archive(limits: arcagent.CapabilityImportLimits) -> Path:
    """Spool standard input into a bounded, private ZIP file.

    ``CapabilityImportService`` deliberately receives a path: its archive
    preflight owns the ZIP validation and its intake owns quarantine/staging.
    The CLI only supplies a regular, size-bounded file when the operator pipes
    bytes in; it never inspects or extracts the archive itself.
    """
    descriptor, raw_path = tempfile.mkstemp(prefix="arc-capability-import-", suffix=".zip")
    temporary = Path(raw_path)
    try:
        total = 0
        with os.fdopen(descriptor, "wb") as output:
            while chunk := sys.stdin.buffer.read(_ARCHIVE_CHUNK_SIZE):
                total += len(chunk)
                if total > limits.max_compressed_bytes:
                    raise arcagent.CapabilityImportError(
                        "uploaded archive exceeds configured limit"
                    )
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _archive_source(archive: Path, limits: arcagent.CapabilityImportLimits) -> tuple[Path, bool]:
    """Return the archive path and whether this command must remove it.

    Local sources remain in place and are subject to the same ArcAgent
    preflight as a web upload. ``-`` is the conventional stdin spelling and
    is bounded before that preflight runs.
    """
    if str(archive) == "-":
        return _copy_stdin_archive(limits), True
    if archive.suffix.casefold() != ".zip":
        raise ValueError("capability imports must be ZIP archives (use '-' for stdin)")
    return archive, False


def _review_payload(review: arcagent.CapabilityImportReview, agent_id: str) -> dict[str, object]:
    """Return the JSON-safe CLI contract without exposing quarantined bytes."""
    payload = review.model_dump(mode="json")
    payload["agent_id"] = agent_id
    return payload


def _print_review(review: arcagent.CapabilityImportReview, agent_id: str) -> None:
    """Render a compact table for a newly staged review."""
    print_table(
        ["Agent", "Import", "Status", "Tools", "Skills", "Review digest", "Activation"],
        [
            [
                agent_id,
                review.import_id,
                review.status.value,
                ", ".join(review.tools) or "-",
                ", ".join(review.skills) or "-",
                review.review_digest,
                review.activation,
            ]
        ],
    )


def _import(args: argparse.Namespace) -> None:
    """Stage one archive for review; this intentionally cannot activate it."""
    agent_id, agent_root, target_did = _resolve_target(args.agent)
    limits = arcagent.CapabilityImportLimits()
    source, remove_source = _archive_source(args.archive, limits)
    try:
        capabilities_root = agent_root / "capabilities"
        service = arcagent.CapabilityImportService(capabilities_root)
        intake = arcagent.intake_capability_archive(source, capabilities_root, limits=limits)
        manifest = service.review(intake, target_agent_did=target_did, limits=limits)
        review = service.review_summary(manifest)
    finally:
        if remove_source:
            source.unlink(missing_ok=True)

    if args.json:
        from arccli.commands._shared import print_json

        print_json(_review_payload(review, agent_id))
        return
    _print_review(review, agent_id)
    write(
        "Staged for review only; inspect with `arc capability-import show`, then edit and "
        "operator-promote explicitly."
    )


def _list(args: argparse.Namespace) -> None:
    agent_id, agent_root, _ = _resolve_target(args.agent)
    rows = arcagent.CapabilityImportService(agent_root / "capabilities").list_reviews()
    if args.json:
        from arccli.commands._shared import print_json

        print_json(
            {"agent_id": agent_id, "imports": [row.model_dump(mode="json") for row in rows]}
        )
        return
    if not rows:
        write("No capability-import reviews.")
        return
    print_table(
        ["Import", "Status", "Tools", "Skills", "Review digest"],
        [
            [
                row.import_id,
                row.status.value,
                str(len(row.tools)),
                str(len(row.skills)),
                row.review_digest,
            ]
            for row in rows
        ],
    )


def _show(args: argparse.Namespace) -> None:
    _, agent_root, _ = _resolve_target(args.agent)
    staging = _staging(agent_root, args.import_id)
    content = arcagent.CapabilityImportService(agent_root / "capabilities").read_reviewed_file(
        staging, args.path
    )
    sys.stdout.buffer.write(content)
    if not content.endswith(b"\n"):
        sys.stdout.write("\n")


def _edit(args: argparse.Namespace) -> None:
    _, agent_root, target_did = _resolve_target(args.agent)
    staging = _staging(agent_root, args.import_id)
    content = Path(args.content_file).read_bytes()
    service = arcagent.CapabilityImportService(agent_root / "capabilities")
    manifest = service.edit_reviewed_file(
        staging,
        args.path,
        content,
        target_agent_did=target_did,
    )
    write(f"Updated {args.path}; review digest is now {manifest.review_digest}.")


def _operator_signer() -> Signer:
    """Resolve the same deployment operator signer used by ``arc trust``."""
    from arccli.commands.operator import resolve_operator_signer

    try:
        return resolve_operator_signer()
    except SignerError as exc:
        raise ValueError(f"cannot resolve the operator signer: {exc}") from exc


def _promote(args: argparse.Namespace) -> None:
    agent_id, agent_root, target_did = _resolve_target(args.agent)
    staging = _staging(agent_root, args.import_id)
    signer = _operator_signer()
    operator_did = OperatorApprovalAuthority(signer).did
    service = arcagent.CapabilityImportService(agent_root / "capabilities")
    with audit_chain("arc capability-import", lambda: operator_did) as (sink, _):
        promoted = service.promote(
            staging,
            target_agent_did=target_did,
            operator_did=operator_did,
            signer=signer,
            config_path=agent_root / "arcagent.toml",
            audit_sink=sink,
        )
    paths = ", ".join(
        path.relative_to(agent_root / "capabilities").as_posix() for path in promoted
    )
    write(
        f"Promoted {args.import_id} on {agent_id} — signed, key pinned, and source pinned "
        f"({paths})."
    )


def _revoke(args: argparse.Namespace) -> None:
    agent_id, agent_root, _target_did = _resolve_target(args.agent)
    staging = _staging(agent_root, args.import_id)
    signer = _operator_signer()
    operator_did = OperatorApprovalAuthority(signer).did
    service = arcagent.CapabilityImportService(agent_root / "capabilities")
    with audit_chain("arc capability-import", lambda: operator_did) as (sink, _):
        service.revoke(
            staging,
            operator_did=operator_did,
            config_path=agent_root / "arcagent.toml",
            audit_sink=sink,
        )
    write(f"Revoked {args.import_id} on {agent_id} — signature and trust pins removed.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arc capability-import")
    subs = parser.add_subparsers(dest="subcmd", required=True)
    commands = (
        ("import", "Stage one ZIP for static review; it remains inactive."),
        ("list", "List staged reviews."),
        ("show", "Print one reviewed file."),
        ("edit", "Edit one staged file and regenerate evidence."),
        ("promote", "Promote one reviewed import with the operator signer."),
        ("revoke", "Revoke one promoted import and remove its trust pins."),
    )
    for name, help_text in commands:
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("--agent", default=None, help="Agent id under team/.")
        if name == "import":
            sub.add_argument(
                "archive", type=Path, help="ZIP path, or '-' to read a ZIP from stdin."
            )
            sub.add_argument("--json", action="store_true", help="Print review metadata as JSON.")
        if name == "list":
            sub.add_argument("--json", action="store_true", help="Print review metadata as JSON.")
        if name not in {"import", "list"}:
            sub.add_argument("import_id")
        if name in {"show", "edit"}:
            sub.add_argument("path")
        if name == "edit":
            sub.add_argument("--content-file", required=True, type=Path)
    return parser


_SUBCOMMAND_MAP = {
    "import": _import,
    "list": _list,
    "show": _show,
    "edit": _edit,
    "promote": _promote,
    "revoke": _revoke,
}


def capability_import_handler(args: list[str]) -> None:
    try:
        dispatch(_build_parser(), _SUBCOMMAND_MAP, args)
    except (arcagent.CapabilityImportError, OSError, ValueError) as exc:
        _err(f"arc capability-import: {exc}")
        raise SystemExit(1) from exc


__all__ = ["capability_import_handler"]
