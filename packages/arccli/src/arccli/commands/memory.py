"""``arc memory`` — operator maintenance over an agent's glass-box memory files.

Three subcommands:

    arc memory dedup [--apply] <workspace> [<workspace> ...]
    arc memory dedup --agent <id> [--dry-run | --apply]
    arc memory status [<workspace> ...]
    arc memory backend [--index-backend sqlite|postgres] [--dsn DSN] [<workspace> ...]

``dedup`` merges memory cards whose slugs diverged before slug canonicalization
landed (one real thing became several files: "Custom ERP.md", "custom-erp.md"). The
merge algorithm itself lives in :mod:`arcmemory.hygiene` — all memory logic stays in
arcmemory so a deployment can swap the whole package out. This module is a THIN CLI:
it discovers workspaces, calls :func:`arcmemory.hygiene.dedup_workspace`, and renders
the report. Dry-run by default; ``--apply`` writes. After applying, restart each
agent (recovery rebuilds its index) so stale surface rows drop.

``dedup --agent <id>`` additionally runs the IDENTITY de-dup the nightly pass runs
(:func:`arcmemory.entity_dedup.dedup_agent_memory`): kind/tag cleanup, then series
("Thesis 5") folds, cross-type name-token and embedding candidates, and LLM
confirmation of the ambiguous ones — wired from the agent's own
``[modules.memory.config]`` (tier, embedder, distiller) and bound to its DID.

``status`` answers "is semantic recall actually on?" — hybrid recall fuses a vector
(semantic) list with BM25 and the cue graph, and it degrades to the latter two when
the embedder is absent. That degrade is deliberate and silent to the *agent*; it must
never be silent to the *operator*. Same thin-CLI rule: the probe is
:func:`arcmemory.status.semantic_status`, this module only renders it. Exits 1 when
the channel is down so a deploy check can gate on it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tomllib
from pathlib import Path
from typing import Any

from arcgateway import team_roster
from arcmemory.entity_dedup import AgentDedupReport, dedup_agent_memory
from arcmemory.hygiene import DedupReport, dedup_workspace, discover_workspaces
from arcmemory.provider import build_distiller, build_embedder, memory_config_for
from arcmemory.status import (
    IndexBackendHealth,
    SemanticStatus,
    probe_index_backend,
    semantic_status,
)

from arccli.commands._shared import dispatch, err, print_kv
from arccli.commands._shared import write as _out
from arccli.commands.trust import _team_root

_INSTALL_HINT = (
    "Fix (on-device, the default): run `uv sync --all-packages` — arcmemory[local] "
    "is a root dependency, so this installs the embedder (pulls torch, ~2GB) — then "
    "restart the agents. Fix (remote endpoint): set embed_backend = 'provider' and "
    "embed_base_url in [modules.memory.config.backend], and export ARC_EMBED_API_KEY."
)


def _okf_migrate(args: argparse.Namespace) -> None:
    """Re-render pre-OKF memory documents through the canonical writer.

    Dry-run by default (reports what would migrate, writes nothing).
    ``--apply`` rewrites in place. Idempotent: valid files are skipped, so a
    second run finds nothing to migrate.
    """
    from arcmemory.hygiene import okf_migrate_workspace

    apply: bool = args.apply
    mode = "APPLY" if apply else "dry-run"
    total_migrated = 0
    total_failed = 0
    n_workspaces = 0

    for raw in args.workspaces:
        root = Path(raw).expanduser()
        workspaces = discover_workspaces(root)
        if not workspaces:
            err(f"  no memory workspace found under {root}")
            continue
        for workspace in workspaces:
            n_workspaces += 1
            report = okf_migrate_workspace(workspace, apply=apply)
            _out(
                f"\n{report.workspace}  ({mode})\n"
                f"  {len(report.migrated)} migrated, {report.already_valid} already valid, "
                f"{len(report.failed)} failed"
            )
            for path, reason in report.failed:
                err(f"  FAIL {path}: {reason}")
            total_migrated += len(report.migrated)
            total_failed += len(report.failed)

    verb = "migrated" if apply else "to migrate"
    _out(f"\n{total_migrated} file(s) {verb} across {n_workspaces} workspace(s).")
    if total_failed:
        err(f"{total_failed} file(s) could not be parsed — left untouched.")
        raise SystemExit(1)
    if apply and total_migrated:
        _out("Restart each affected agent so its index rebuilds over the rewritten files.")


def _dedup(args: argparse.Namespace) -> None:
    """Merge duplicate memory cards into their canonical-slug files.

    Dry-run by default (reports what would merge, writes nothing). ``--apply``
    performs the merge and deletes the variant files. Idempotent: a second run
    finds nothing to merge.
    """
    if args.agent:
        _dedup_agent(args.agent, apply=args.apply)
        return
    if not args.workspaces:
        err("arc memory dedup: pass --agent <id> or one or more <workspace> paths.")
        raise SystemExit(2)
    apply: bool = args.apply
    mode = "APPLY" if apply else "dry-run"
    total_groups = 0
    total_deleted = 0
    n_workspaces = 0

    for raw in args.workspaces:
        root = Path(raw).expanduser()
        workspaces = discover_workspaces(root)
        if not workspaces:
            err(f"  no memory workspace found under {root}")
            continue
        for workspace in workspaces:
            n_workspaces += 1
            report = dedup_workspace(workspace, apply=apply)
            _render_workspace(report, mode)
            total_groups += report.groups
            total_deleted += sum(s.files_deleted for s in report.stores)

    verb = "merged" if apply else "to merge"
    _out(
        f"\n{total_groups} duplicate group(s) {verb}, "
        f"{total_deleted} file(s) {'deleted' if apply else 'to delete'} "
        f"across {n_workspaces} workspace(s)."
    )
    if apply and total_groups:
        _out("Restart each affected agent so its index rebuilds and stale surface rows drop.")


def _resolve_agent(agent_id: str) -> tuple[Path, str]:
    """``(agent_root, did)`` for ``agent_id`` under the team dir, or exit naming the known ones."""
    entries = team_roster.list_team(team_root=_team_root(), online_ids=set())
    match = next((entry for entry in entries if entry.agent_id == agent_id), None)
    if match is None or not match.did:
        known = ", ".join(entry.agent_id for entry in entries) or "(none)"
        err(f"arc memory: unknown agent {agent_id!r}. Known agents: {known}")
        raise SystemExit(1)
    return Path(match.workspace_path), match.did


def _memory_section(agent_root: Path) -> dict[str, Any]:
    """The agent's ``[modules.memory.config]`` table (empty when absent)."""
    data = tomllib.loads((agent_root / "arcagent.toml").read_text(encoding="utf-8"))
    section = data.get("modules", {}).get("memory", {}).get("config", {})
    return section if isinstance(section, dict) else {}


def _dedup_agent(agent_id: str, *, apply: bool) -> None:
    """Slug-variant merge, kind cleanup and identity de-dup over one agent's memory."""
    agent_root, did = _resolve_agent(agent_id)
    workspace = agent_root / "workspace"
    mode = "APPLY" if apply else "dry-run"
    section = _memory_section(agent_root)
    backend: dict[str, Any] = section.get("backend") or {}
    config = memory_config_for(backend, section.get("tier", "personal"))
    embedder = build_embedder(
        did,
        str(backend.get("embed_backend", "local")),
        str(backend.get("embed_model", "")),
        base_url=str(backend.get("embed_base_url", "")),
    )
    # A dry run never calls the model, so it needs no distiller (or provider key).
    confirmer = (
        build_distiller(
            str(backend.get("distill_provider", "")),
            str(backend.get("distill_model", "")),
            did,
            agent_id,
        )
        if apply
        else None
    )
    _render_workspace(dedup_workspace(workspace, apply=apply), mode)
    report = asyncio.run(
        dedup_agent_memory(
            workspace, did, apply=apply, config=config, embedder=embedder, confirmer=confirmer
        )
    )
    _render_identity(report, apply=apply)
    if apply and report.result.merged:
        _out("Restart the agent so its index rebuilds over the merged cards.")


def _render_identity(report: AgentDedupReport, *, apply: bool) -> None:
    """Print the kind cleanup and the identity de-dup plan/outcome."""
    plan = report.result.plan
    _out(f"  kinds/tags: {report.kinds.changed} card(s) {'cleaned' if apply else 'to clean'}")
    _out(
        f"  identity: {plan.entities} entities, {len(plan.certain)} certain, "
        f"{len(plan.exact)} same-name, {len(plan.ambiguous)} to confirm (LLM), "
        f"{len(plan.blocked)} blocked by classification"
    )
    for group in plan.certain:
        folded = ", ".join(group.folded)
        _out(f"    certain  {group.survivor} <- {folded}  ({group.entity_type}: {group.name})")
    for cluster in plan.exact:
        _out(f"    same-name  {', '.join(cluster)}")
    for cluster in plan.ambiguous:
        _out(f"    confirm  {', '.join(cluster)}")
    for pair in plan.blocked:
        _out(f"    blocked  {', '.join(pair)}")
    if apply:
        _out(f"  {len(report.result.merged)} merged")


def _render_workspace(report: DedupReport, mode: str) -> None:
    """Print one workspace's per-store merge plan/outcome."""
    _out(f"\n{report.workspace}  ({mode})")
    if report.groups == 0:
        _out("  no duplicates.")
        return
    for store_report in report.stores:
        if not store_report.merges:
            continue
        _out(
            f"  {store_report.store}: {len(store_report.merges)} group(s), "
            f"{store_report.files_deleted} file(s) to delete"
        )
        for merge in store_report.merges:
            _out(f"    merge {merge.sources} -> {merge.canonical}.md")


def _status(args: argparse.Namespace) -> None:
    """Report whether the semantic (vector) channel of hybrid recall is live.

    Probes the real embedder seam — the same call ``retrieve`` makes — so a LIVE
    verdict means the path works, not that a package merely imports.
    """
    workspaces: list[Path] = []
    for raw in args.workspaces:
        root = Path(raw).expanduser()
        found = discover_workspaces(root)
        if not found:
            err(f"  no memory workspace found under {root}")
        workspaces.extend(found)

    embedder = build_embedder("did:arc:operator", args.backend, args.model)
    status = asyncio.run(semantic_status(workspaces, embedder=embedder, backend=args.backend))
    _render_status(status)
    if not status.live:
        sys.exit(1)


def _render_status(status: SemanticStatus) -> None:
    """Print the verdict, the two halves, and per-workspace vector coverage."""
    verdict = "LIVE" if status.live else "DEGRADED"
    _out(f"\nsemantic recall: {verdict}")
    print_kv(
        [
            ("sqlite-vec extension", "loaded" if status.vec_extension else "NOT LOADED"),
            ("embedder backend", status.embedder_backend),
            ("embedder", "answering" if status.embedder_live else "NOT AVAILABLE"),
            ("embedding dims", str(status.embedder_dims or "-")),
            ("detail", status.detail),
        ]
    )
    if status.degraded_reasons:
        _out("  degrade warnings fired: " + ", ".join(status.degraded_reasons))

    for workspace in status.workspaces:
        _out(
            f"  {workspace.workspace}: {workspace.indexed_chunks} chunks, "
            f"{workspace.embedded_chunks} embedded, "
            f"{workspace.insight_triggers} insight triggers"
        )

    if status.live:
        _out("\nHybrid recall is fusing vec + bm25 + graph.")
        return
    err("\nHybrid recall is running on BM25 + graph only — paraphrase and")
    err("cross-domain matches are being missed, and entity dedup is a no-op.")
    err(_INSTALL_HINT)


def _backend(args: argparse.Namespace) -> None:
    """Report whether the configured index backend is reachable.

    The index backend is swappable: ``sqlite`` (per-agent file, always up) or
    ``postgres`` (a shared pgvector server that a deployment must be able to prove
    is reachable before trusting the switch). All backend logic lives in
    :func:`arcmemory.status.probe_index_backend`; this is a thin renderer.
    ``postgres`` exits 1 when the server does not answer so a deploy check can gate
    on it — the same gate contract as ``status``.
    """
    if args.index_backend == "postgres":
        health = asyncio.run(probe_index_backend("postgres", dsn=args.dsn or None))
        _render_backend(health)
        if not health.connected:
            sys.exit(1)
        return

    workspaces: list[Path] = []
    for raw in args.workspaces:
        root = Path(raw).expanduser()
        found = discover_workspaces(root)
        if not found:
            err(f"  no memory workspace found under {root}")
        workspaces.extend(found)

    if not workspaces:
        _out("\nindex backend: sqlite (config default) — a per-agent file, always local.")
        _out("Pass a workspace to report its vector channel, or --index-backend postgres.")
        return

    for workspace in workspaces:
        health = asyncio.run(probe_index_backend("sqlite", workspace=workspace))
        _out(f"\n{workspace}")
        _render_backend(health)


def _render_backend(health: IndexBackendHealth) -> None:
    """Print one backend's reachability verdict and vector-channel status."""
    print_kv(
        [
            ("backend", health.backend),
            ("connected", "yes" if health.connected else "NO"),
            ("vector channel", "available" if health.vec_available else "unavailable"),
            ("detail", health.detail),
        ]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc memory",
        description="Operator maintenance over an agent's glass-box memory files.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    dedup_p = subs.add_parser(
        "dedup",
        help="Merge pre-canonicalization duplicate memory cards into their canonical file.",
    )
    dedup_p.add_argument(
        "workspaces",
        nargs="*",
        metavar="<workspace>",
        help="Dir containing memory/ (or a root to search for nested workspaces).",
    )
    dedup_p.add_argument(
        "--agent",
        default=None,
        help="Agent id under team/: also run identity de-dup (kinds, series, cross-type).",
    )
    dedup_mode = dedup_p.add_mutually_exclusive_group()
    dedup_mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform the merges (default: dry-run).",
    )
    dedup_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the plan and write nothing (the default).",
    )
    okf_p = subs.add_parser(
        "okf-migrate",
        help="Re-render pre-OKF memory documents through the canonical OKF writer.",
    )
    okf_p.add_argument(
        "workspaces",
        nargs="+",
        metavar="<workspace>",
        help="Dir containing memory/ (or a root to search for nested workspaces).",
    )
    okf_p.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite files in place (default: dry-run).",
    )
    status_p = subs.add_parser(
        "status",
        help="Report whether semantic (vector) recall is live, or degraded to BM25 + graph.",
    )
    status_p.add_argument(
        "workspaces",
        nargs="*",
        metavar="<workspace>",
        help="Dirs containing memory/ to report vector coverage for (optional).",
    )
    status_p.add_argument(
        "--backend",
        default="local",
        help="Embedding backend to probe: local (default), provider, or none.",
    )
    status_p.add_argument("--model", default="", help="Embedding model to probe.")

    backend_p = subs.add_parser(
        "backend",
        help="Report whether the configured index backend (sqlite/postgres) is reachable.",
    )
    backend_p.add_argument(
        "workspaces",
        nargs="*",
        metavar="<workspace>",
        help="Dirs containing memory/ to report the sqlite vector channel for (optional).",
    )
    backend_p.add_argument(
        "--index-backend",
        default="sqlite",
        choices=("sqlite", "postgres"),
        help="Index backend to probe: sqlite (default, per-agent file) or postgres.",
    )
    backend_p.add_argument(
        "--dsn",
        default="",
        help="Postgres DSN (falls back to ARC_MEMORY_PG_DSN when omitted).",
    )
    return parser


_SUBCOMMANDS = {
    "dedup": _dedup,
    "okf-migrate": _okf_migrate,
    "status": _status,
    "backend": _backend,
}


def memory_handler(args: list[str]) -> None:
    """Entry point for ``arc memory <subcommand>`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMANDS, args)


__all__ = ["memory_handler"]
