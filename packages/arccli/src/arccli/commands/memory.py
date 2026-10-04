"""``arc memory`` — operator maintenance over an agent's glass-box memory files.

Subcommands:

    arc memory dedup [--apply] <workspace> [<workspace> ...]
    arc memory dedup --agent <id> [--dry-run | --apply]
    arc memory status [<workspace> ...]
    arc memory backend [--index-backend sqlite|postgres] [--dsn DSN] [<workspace> ...]
    arc memory embed-backfill [--agent <id>] [--shared] [--run] [<workspace> ...]

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

``embed-backfill`` shows, per document pool, how many connected-document chunks
still lack a vector (an embedder outage at write time leaves them lexical-only).
The serving process (``arc ui``) backfills them in the background on its own;
the readout is read-only and safe beside it. ``--run`` finishes a store in this
process instead, for use while the service is STOPPED: it refuses any store the
service owns, because a second process writing one ``index.db`` makes the
service rebuild that store's whole vector sidecar from ``vec0`` again and again.
The backfill itself lives in :mod:`arcmemory.index.backfill`; this is a renderer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from arcgateway import team_roster
from arcmemory.entity_dedup import AgentDedupReport, dedup_agent_memory
from arcmemory.hygiene import DedupReport, dedup_workspace, discover_workspaces
from arcmemory.index.backfill import (
    DEFAULT_BATCH_SIZE,
    backfill_owner,
    backfill_store,
    read_embed_backlog,
)
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
    for change in report.kinds.changes:
        _out(
            f"    {change.slug}: type {change.old_type} -> {change.new_type}; "
            f"tags {list(change.old_tags)} -> {list(change.new_tags)}"
        )
    _out(
        f"  identity: {plan.entities} entities, {len(plan.certain)} certain, "
        f"{len(plan.exact)} same-name, {len(plan.ambiguous)} to confirm (LLM), "
        f"{len(plan.blocked)} blocked (unreadable classification label)"
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
        if report.proposals:
            _out(
                f"  {len(report.proposals)} proposal(s) left for a person: "
                "arcui > Knowledge > Entities > Review duplicates"
            )


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


_INDEX_DB = Path("memory") / "index.db"
#: Seconds between progress lines during ``embed-backfill --run``.
_PROGRESS_EVERY_S = 10.0
_PROFILE_FILE = ".embedding-profile"


def _embed_backfill(args: argparse.Namespace) -> None:
    """Report each store's doc-pool vector backlog, or (``--run``) finish it here."""
    if args.run and not args.agent:
        err("arc memory embed-backfill: --run needs --agent <id> (whose embedder to use).")
        raise SystemExit(2)
    agent = _resolve_agent(args.agent) if args.agent else None
    stores = _backfill_stores(args, agent)
    if not stores:
        err("arc memory embed-backfill: no memory index found; pass a workspace or --agent.")
        raise SystemExit(2)
    if not args.run:
        for store in stores:
            _render_backlog(store)
        return
    assert agent is not None  # noqa: S101 - --run without --agent exited above
    refused = sum(not _run_backfill(store, agent, args.batch_size) for store in stores)
    if refused:
        raise SystemExit(1)


def _backfill_stores(args: argparse.Namespace, agent: tuple[Path, str] | None) -> list[Path]:
    """The stores named: workspaces given, the agent's own, and (``--shared``) shared ones."""
    stores: list[Path] = []
    for raw in args.workspaces:
        stores.extend(_index_stores(Path(raw).expanduser()))
    if agent is not None and not args.workspaces:
        stores.append(agent[0] / "workspace")
    if args.shared:
        stores.extend(_shared_stores(agent))
    return list(dict.fromkeys(store for store in stores if (store / _INDEX_DB).is_file()))


def _index_stores(root: Path) -> list[Path]:
    """``root`` when it holds a memory index, else every store with one beneath it."""
    if (root / _INDEX_DB).is_file():
        return [root]
    found = {db.parent.parent for db in root.rglob("index.db") if db.parent.name == "memory"}
    return sorted(found)


def _shared_stores(agent: tuple[Path, str] | None) -> list[Path]:
    """The fleet's connection stores; with an agent, only those embedded its way."""
    root = _team_root() / "shared" / "connected"
    stores = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    if agent is None:
        return stores
    mine = _agent_profile(agent[0])
    kept = []
    for store in stores:
        try:
            theirs = (store / _PROFILE_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            theirs = ""
        if theirs and theirs != mine:
            err(f"  skip {store}: embedded another way than this agent ({theirs})")
            continue
        kept.append(store)
    return kept


def _agent_embed_settings(agent_root: Path) -> tuple[str, str, str]:
    """The agent's ``embed_*`` settings exactly as its memory module resolves them."""
    from arcagent.modules.memory.config import MemoryConfig as AgentMemoryConfig

    backend = AgentMemoryConfig(**_memory_section(agent_root)).backend
    return (
        str(backend.get("embed_backend", "local")),
        str(backend.get("embed_model", "")),
        str(backend.get("embed_base_url", "")),
    )


def _agent_profile(agent_root: Path) -> str:
    from arcagent.modules.connected_data.ingest import profile_of

    return profile_of(*_agent_embed_settings(agent_root))


def _render_backlog(store: Path) -> None:
    """Print one store's per-pool coverage and who owns its backfill."""
    db_path = store / _INDEX_DB
    owner = backfill_owner(db_path)
    backlog = read_embed_backlog(db_path)
    _out(f"\n{store}  (backfill owner: {'none' if owner is None else f'pid {owner}'})")
    if not backlog:
        _out("  no document pools.")
        return
    for scope, pool in backlog.items():
        _out(f"  {scope}: {pool.embedded} of {pool.total} embedded, {pool.pending} pending")
    pending = sum(pool.pending for pool in backlog.values())
    total = sum(pool.total for pool in backlog.values())
    _out(f"  all pools: {total - pending} of {total} embedded, {pending} pending")


def _run_backfill(store: Path, agent: tuple[Path, str], batch_size: int) -> bool:
    """Backfill one store here; False when another process owns it (refused)."""
    agent_root, did = agent
    db_path = store / _INDEX_DB
    owner = backfill_owner(db_path)
    if owner is not None:
        _refuse(store, owner)
        return False
    pending = sum(pool.pending for pool in read_embed_backlog(db_path).values())
    _out(f"\n{store}: {pending} chunk(s) pending")
    backend, model, base_url = _agent_embed_settings(agent_root)
    embedder = build_embedder(did, backend, model, base_url=base_url)
    section = _memory_section(agent_root)
    config = memory_config_for(section.get("backend") or {}, section.get("tier", "personal"))
    outcome = asyncio.run(
        backfill_store(
            store,
            config,
            embedder,
            batch_size=batch_size,
            on_progress=_progress_printer(pending),
        )
    )
    if outcome.status == "blocked":
        _refuse(store, backfill_owner(db_path))
        return False
    remaining = sum(pool.pending for pool in read_embed_backlog(db_path).values())
    _out(f"  {outcome.embedded} vector(s) written, {remaining} pending ({outcome.status}).")
    if outcome.status == "embedder_down":
        err("  the embedder stayed unavailable; run `arc memory status` for the fix.")
    return True


def _refuse(store: Path, owner: int | None) -> None:
    who = f"pid {owner}" if owner is not None else "another process"
    err(
        f"  refused {store}: {who} owns its backfill (normally `arc ui`, which backfills "
        "it in the background). Watch it with `arc memory embed-backfill` (no --run), "
        "or stop the service first."
    )


def _progress_printer(pending: int) -> Any:
    """A progress callback printing embedded count, rate and ETA every few seconds."""
    started = time.monotonic()
    last = [started]

    def report(embedded: int) -> None:
        now = time.monotonic()
        if now - last[0] < _PROGRESS_EVERY_S:
            return
        last[0] = now
        rate = embedded / max(now - started, 1e-9)
        eta_min = max(pending - embedded, 0) / rate / 60 if rate else float("inf")
        _out(f"  {embedded} of {pending} embedded ({rate:.0f}/s, ~{eta_min:.0f} min left)")

    return report


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
    backfill_p = subs.add_parser(
        "embed-backfill",
        help="Show (or, with the service stopped, finish) the doc-pool vector backfill.",
    )
    backfill_p.add_argument(
        "workspaces",
        nargs="*",
        metavar="<workspace>",
        help="Dirs holding memory/index.db (or roots to search). Default: the agent's own.",
    )
    backfill_p.add_argument(
        "--agent",
        default=None,
        help="Agent id under team/: its workspace, and the embedder --run uses.",
    )
    backfill_p.add_argument(
        "--shared",
        action="store_true",
        help="Include the fleet's shared connection stores (team/shared/connected/*).",
    )
    backfill_p.add_argument(
        "--run",
        action="store_true",
        help="Embed to completion in this process. Only while `arc ui` is stopped.",
    )
    backfill_p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Texts per embed call (default {DEFAULT_BATCH_SIZE}).",
    )
    return parser


_SUBCOMMANDS = {
    "dedup": _dedup,
    "okf-migrate": _okf_migrate,
    "status": _status,
    "backend": _backend,
    "embed-backfill": _embed_backfill,
}


def memory_handler(args: list[str]) -> None:
    """Entry point for ``arc memory <subcommand>`` (registry dispatch)."""
    dispatch(_build_parser(), _SUBCOMMANDS, args)


__all__ = ["memory_handler"]
