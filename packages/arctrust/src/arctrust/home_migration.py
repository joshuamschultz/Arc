"""One-time move of a flat ``~/.arc`` into ``runtime/`` + ``config/`` + ``state/``.

Deployments that predate the lifecycle split have everything at the root::

    ~/.arc/  operator/ identity/ trust/ store/ nats/ bundles/ modules/
             arcagent.toml arcllm.toml gateway.toml connections.toml arc.env

This module moves each entry to the root that matches its lifecycle, once, so
that a later "download the new one and overwrite ``runtime/``" cannot destroy
the operator signing key, the trust store, or the arcstore database.

It is a **filesystem** migration for live deployments, not a compatibility shim:
nothing here teaches Arc to read two layouts. After it runs, every surface reads
the split layout through :mod:`arctrust.paths` and this module is inert.

Three rules make it safe to run on a box that must keep working:

* **Move, never copy-then-delete.** Every step is a single ``os.replace``, so a
  crash leaves each entry wholly at the source or wholly at the destination.
  This module deletes nothing — ever.
* **Refuse to overwrite.** If any destination is already occupied the whole
  migration aborts before the first move, and says which one.
* **Roll back on failure.** A failure part-way reverses the moves already made.
  A half-migrated home that cannot find its signing key is worse than no
  migration at all.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from arctrust import paths


class MigrationError(RuntimeError):
    """The migration refused to start, or failed and rolled back.

    Either way the Arc home is left in the layout it had before the call.
    """


#: Flat entry name -> the accessor naming its new home. Anything not listed
#: here — ``team/``, operator notes, a future root — is left exactly alone.
_LAYOUT: dict[str, str] = {
    # config/ — preserved across an update
    "arcagent.toml": "config_file",
    "arcllm.toml": "config_file",
    "arcrun.toml": "config_file",
    "gateway.toml": "config_file",
    "connections.toml": "config_file",
    "arc.env": "env_file",
    # state/ — never touched by an update
    "operator": "operator_dir",
    "identity": "identity_dir",
    "trust": "trust_dir",
    "store": "store_dir",
    "nats": "nats_dir",
    "bundles": "bundles_dir",
    "extensions": "extensions_dir",
    "capabilities": "capabilities_dir",
    "blueprints": "blueprints_dir",
    "skills": "skills_dir",
    "gateway": "gateway_dir",
    "audit": "audit_dir",
    "users.json": "users_file",
    # runtime/ — replaced wholesale on update
    "modules": "module_root",
}


@dataclass(frozen=True)
class MigrationResult:
    """What the migration did, for the caller to report.

    Attributes:
        moved: ``(source, destination)`` pairs, in the order they were moved.
        already_migrated: True when there was nothing left in the flat layout.
    """

    moved: list[tuple[Path, Path]] = field(default_factory=list)
    already_migrated: bool = False


def _destination(name: str) -> Path:
    """Resolve one flat entry's new path through its typed accessor."""
    accessor = _LAYOUT[name]
    if accessor == "config_file":
        return paths.config_file(name)
    resolver: Callable[[], Path] = getattr(paths, accessor)
    return resolver()


def plan_migration() -> list[tuple[Path, Path]]:
    """Return the ``(source, destination)`` moves this Arc home still needs.

    Empty means the home is absent, already split, or holds nothing recognised —
    all of which are a successful no-op.
    """
    home = paths.arc_home()
    if not home.is_dir():
        return []
    moves: list[tuple[Path, Path]] = []
    for name in _LAYOUT:
        source = home / name
        if source.exists() or source.is_symlink():
            moves.append((source, _destination(name)))
    return moves


def migrate_arc_home() -> MigrationResult:
    """Move a flat Arc home into the split layout. Idempotent.

    Returns:
        What moved. ``already_migrated`` is True when nothing needed to.

    Raises:
        MigrationError: a destination was occupied, or a move failed — in both
            cases every entry is back where it started.
    """
    moves = plan_migration()
    if not moves:
        return MigrationResult(already_migrated=True)

    occupied = [str(dst) for _, dst in moves if dst.exists() or dst.is_symlink()]
    if occupied:
        raise MigrationError(
            "Arc home migration aborted — destination already exists: "
            f"{', '.join(occupied)}. Nothing was moved. Resolve the conflict by hand: "
            "the flat entry and the split entry are both real data and only you "
            "know which is current."
        )

    done: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            done.append((source, destination))
    except OSError as exc:
        _roll_back(done)
        raise MigrationError(
            f"Arc home migration failed while moving into {paths.arc_home()}: {exc}. "
            f"Rolled back {len(done)} move(s); the deployment is unchanged and usable."
        ) from exc
    return MigrationResult(moved=done)


def _roll_back(done: list[tuple[Path, Path]]) -> None:
    """Reverse completed moves, newest first.

    Best-effort by construction: a rollback step that itself fails must not mask
    the original error, and the entry it could not return is still whole at its
    destination — so it is reported in the raised message, not lost.
    """
    failures: list[str] = []
    for source, destination in reversed(done):
        try:
            os.replace(destination, source)
        except OSError as exc:  # reason: report, never mask the original failure
            failures.append(f"{destination} -> {source}: {exc}")
    if failures:
        raise MigrationError(
            "Arc home migration failed AND could not fully roll back. "
            f"These entries are at their new paths, intact: {'; '.join(failures)}"
        )


__all__ = ["MigrationError", "MigrationResult", "migrate_arc_home", "plan_migration"]
