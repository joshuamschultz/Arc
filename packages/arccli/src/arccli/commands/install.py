"""``arc install`` — take a synced checkout to ready-to-start, and prove it.

Four stages, and the middle two are the reason the command exists::

    preflight   operator key, team root, data dir  (nats-server reported)
    bundles     build + sign a bundle for every module the fleet is missing
    modules     install every module an agent's config enables, for every agent
    verify      re-read the configs and refuse to call the box ready

Modules do not ship inside the wheel. They reach a deployment only as signed
bundles materialized under ``${ARC_CONFIG_DIR:-~/.arc}/modules/``, which means a
``git pull && uv sync`` — the whole of what most people think "install" is —
leaves every agent with **zero** modules. Those agents still boot, still answer
chat, and still look green, while their schedulers never fire and their tasks
never dispatch. That is the failure this command makes impossible, and it is not
hypothetical: it is the state a live deployment was found in.

**Why a separate verb rather than a flag on ``arc up``.** ``arc up`` ends by
handing the process to ``arc ui start``, which blocks for the lifetime of the
server. There is no flag combination that installs and returns: ``--check``
reports without writing and ``--no-install`` skips the stage outright. The
install-and-exit path genuinely did not exist. Naming it ``arc install`` also
makes the deploy sequence two readable verbs — install, then start — that a
newcomer finds in ``arc --help`` and a systemd unit can put in ``ExecStartPre``.

**Why not everything.** This never syncs the Python environment: the environment
is what provides the ``arc`` binary running this code, so ``uv sync`` is the step
*before* it, not inside it. It never mints an operator key either — the operator
key is the anchor that decides what this deployment trusts, and a bring-up that
mints its own anchor has verified nothing (``arc init`` mints it, deliberately).

**One command, for the whole fleet, that survives a partial failure.** Every
agent under the team root is handled in one invocation; a module whose source
cannot be packaged is reported and the rest continue; an already-staged bundle
is left alone rather than demanding ``--force``. Re-running after something went
wrong is the ordinary case, so it is the cheap one.

There is no logic of its own here. Every stage calls the same function
``arc up`` calls, so the two can never disagree about what "installed" means and
``arc up`` finds nothing left to bootstrap after this command exits zero.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arccli.commands import up
from arccli.commands._shared import err as _err
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out


def _preflight_or_exit(team_root: Path | None) -> tuple[Path, list[up.Check]]:
    """Run and print the preflight; exit when a check installing *depends on* failed.

    Returns the resolved team root alongside the checks, because surviving this
    function is exactly what proves the root is not ``None`` — the team-root
    check is one of the blocking ones.

    A failed ``nats-server`` check does not stop the install: the modules an
    agent's config asks for have nothing to do with the broker binary, and a box
    left with neither is strictly worse than a box left with one. It is still a
    FAIL line, and it still turns the final exit code non-zero, so nothing about
    it is quiet.
    """
    checks = up.preflight(team_root)
    _print_table(["Check", "Status", "Detail"], [[c.name, c.status, c.detail] for c in checks])
    blocking = [check for check in checks if not check.ok and check.needed_to_install]
    if team_root is None or blocking:
        sys.stdout.flush()  # keep the stderr verdict under the table it refers to
        _err("\narc install: preflight failed — nothing was installed. Fix the FAIL lines above.")
        sys.exit(1)
    return team_root, checks


def stage_bundles(states: list[up.AgentState]) -> list[list[str]]:
    """Build a signed bundle for every module the fleet is missing. Report each.

    This is the step an operator had to discover the hard way: ``arc module
    install --all`` has nothing to install on a fresh box, because the bundle
    store is empty until somebody runs ``arc module bundle`` — and nothing said
    so. Doing it here means the verb never has to be typed.

    Only the *missing* modules are staged, which is what keeps a second run from
    writing anything. A failure is collected rather than raised: one unusable
    module source must not cost the rest of the fleet its capabilities, and the
    verify stage refuses the deployment on whatever is still missing afterwards.
    """
    from arccli.commands.module import BundleStageError, stage_bundle

    needed = sorted({module for state in states for module in state.missing})
    if not needed:
        return [["-", "nothing to build — every enabled module is already installed"]]

    rows: list[list[str]] = []
    for module in needed:
        try:
            rows.append([module, stage_bundle(module)])
        except BundleStageError as exc:
            rows.append([module, f"REFUSED: {exc}"])
    return rows


def migrate_layout_or_exit() -> None:
    """Move a pre-split home and a fleet in the code checkout into the lifecycle layout.

    This runs FIRST because every stage below it resolves a path: preflight loads
    the operator key and the team root, the bundle stage reads the staged-bundle
    store, and the module stage writes under the runtime. Migrating after any of
    them would have them answer from the old layout and then quietly disagree
    with the new one — and a stage that *creates* what it did not find would mint
    a second agent identity beside the real one, at which point the migration can
    only refuse, because both roots hold real data.

    A failure here stops the install rather than continuing on a home whose
    signing key may be half-moved — the migration itself has already rolled back
    by the time this sees the error, so the box is left exactly as it was.
    """
    from arctrust.home_migration import MigrationError, migrate_arc_home

    try:
        result = migrate_arc_home()
    except MigrationError as exc:
        _err(f"arc install: {exc}")
        sys.exit(1)
    if result.already_migrated:
        return
    _out("Layout")
    _print_table(
        ["Moved", "To"],
        [[str(src.name), str(dst)] for src, dst in result.moved],
    )
    _out("")


def _install(args: argparse.Namespace) -> None:
    """Install what every agent's config enables, then verify it really landed."""
    migrate_layout_or_exit()
    if args.migrate_only:
        return
    _out("Preflight")
    team_root, checks = _preflight_or_exit(up.resolve_team_root(args.team_root))

    _out("\nBundles")
    _print_table(["Module", "Bundle"], stage_bundles(up.agent_states(team_root)))

    _out("\nModules")
    installed = up.bootstrap_modules(up.agent_states(team_root))
    _print_table(["Agent", "Module(s)", "Action", "Detail"], installed)

    # Re-read from disk rather than trusting the install report: the question is
    # whether the agent's own discovery predicate finds the module now, not
    # whether a write claimed to succeed.
    _out("\nVerify")
    whole = up.print_verify(up.agent_states(team_root), [])

    unmet = [check for check in checks if not check.ok]
    if not whole:
        _err(
            "\narc install: this deployment is still missing a capability its config asks "
            "for. Starting it now would run agents without it, silently."
        )
        sys.exit(1)
    if unmet:
        _err(
            "\narc install: every module is installed, but this deployment cannot run yet — "
            f"{', '.join(check.name for check in unmet)}. Fix the FAIL lines above."
        )
        sys.exit(1)

    _out("\narc install: this deployment is ready. Start it with: arc up")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc install",
        description="Install every module each agent's config enables, and verify the result.",
        add_help=True,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--team-root",
        dest="team_root",
        default=None,
        help="Directory of agents. Default: ./team when it exists.",
    )
    parser.add_argument(
        "--migrate-only",
        dest="migrate_only",
        action="store_true",
        help=(
            "Run only the layout migration and stop. For a deploy that must move an "
            "existing home and fleet BEFORE any stage creates what it cannot find."
        ),
    )
    return parser


def install_handler(args: list[str]) -> None:
    """Top-level handler for ``arc install [flags]`` (registry dispatch)."""
    _install(_build_parser().parse_args(args))
