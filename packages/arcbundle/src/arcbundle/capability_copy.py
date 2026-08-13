"""Per-agent copy of a module's capability surface, and its exact inverse.

A module lands in two places for two different reasons. Its **runtime** stays
once at the deployment root, read-only and outside every agent's tool fence —
that is what :mod:`arcbundle.materializer` writes. Its **capability surface**
(the tools an agent may call and the skills injected into its prompt) is copied
per agent into ``<agent_dir>/capabilities/<module>/``, because a skill that
improves for one agent must not silently change what every other agent on the
box is told to do.

Three consequences follow from copying rather than loading in place:

* The copies sit inside the agent's existing ``capabilities`` root, so the
  loader adjudicates them as UNTRUSTED unchanged. No trusted root is introduced
  — a writable directory whose contents are trusted is exactly the hole that
  classification was drawn to close (SDD alternatives D-648, D-649).
* The layout inside the copy mirrors :func:`append_capability_scan_roots`:
  tools directly under the root, skills under ``skills/``. Anything else is
  simply not discovered.
* ``_runtime.py`` is never copied. Runtime is deployment state; putting it where
  an agent can write would hand the agent its own execution path (ASI05/ASI06).

Signature sidecars travel with the artifacts they sign, so a bundle whose
capability files were signed upstream stays signed after the copy and loads
without a further operator action.

Leaf-safe: this module knows an agent only as a directory path. It imports no
``arcagent`` name, so a bundle can still be staged on a box with no agent stack.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from arcbundle._paths import require_safe_name
from arcbundle.errors import BundleMaterializeError

__all__ = [
    "CAPABILITIES_DIR",
    "CAPABILITY_FILE",
    "MODULE_COPIES_DIR",
    "SIGNATURE_SIDECAR_SUFFIX",
    "SKILLS_DIR",
    "capability_dir",
    "copy_capabilities",
    "remove_capabilities",
]

_logger = logging.getLogger("arcbundle.capability_copy")

#: The per-agent root the loader already scans (its ``agent`` / ``agent-skills``
#: pair). Copies go under it so the loader's set of roots never grows.
CAPABILITIES_DIR = "capabilities"

#: The one file in a module folder that carries its tools. It is also half of
#: what makes a folder a module at all (the other half, ``_runtime.py``, is
#: deliberately left behind).
CAPABILITY_FILE = "capabilities.py"

#: Skills live one level down, matching where ``create_skill`` writes and where
#: the loader's ``*-skills`` root looks.
SKILLS_DIR = "skills"

#: The namespace every per-agent module copy lands under, keeping module names
#: out of collision range of conventional subdirectories like ``skills/``.
#: See :func:`capability_dir` for what happened without it.
MODULE_COPIES_DIR = "modules"

#: The detached-signature sidecar convention the capability loader re-verifies.
#: Spelled here rather than imported because arcbundle is a leaf and must not
#: reach into ``arcagent``; the convention is on-disk, not in code.
SIGNATURE_SIDECAR_SUFFIX = ".arcsig"

# The source tree is hardened to 0444 inside 0555 — correct for a deployment
# root only the operator writes. Inheriting those bits here would produce a copy
# that cannot be removed and cannot be signed in place, because an operator
# approving a capability writes a sidecar beside it. The copy is an ordinary
# file in an ordinary directory; its trust comes from the loader's adjudication
# of the root it sits in, never from a mode bit.
_FILE_MODE = 0o644
_DIR_MODE = 0o755

_STAGING_INFIX = ".staging-"


def capability_dir(agent_dir: Path, module: str) -> Path:
    """Return ``<agent_dir>/capabilities/modules/<module>`` — the one path rule.

    Both halves of the operation resolve the destination through this function,
    so a copy and its inverse can never disagree about what they are aiming at.

    The ``modules/`` level is not decoration. ``<agent_dir>/capabilities/skills/``
    is already a conventional loader root — it is where ``create_skill`` writes
    and what the ``*-skills`` scan root reads. Copying a module named ``skills``
    straight under ``capabilities/`` therefore aimed the copy AT that root, and
    its ``capabilities.py`` landed where only skill folders belong: the loader
    read it as agent-authored source, denied it at the trust gate, and the
    resulting permanent scan error blocked every reload commit. Any module whose
    name collides with a conventional subdirectory would do the same, so the fix
    is a namespace of their own rather than a reserved-name list that the next
    convention would outgrow.

    ``capabilities/modules/`` is itself inert to the scanner: a root walk looks
    at top-level ``.py`` files and directories carrying a ``SKILL.md``, and this
    directory is neither.
    """
    require_safe_name(module, field="module")
    return Path(agent_dir) / CAPABILITIES_DIR / MODULE_COPIES_DIR / module


def copy_capabilities(module_dir: Path, agent_dir: Path, *, module: str) -> Path:
    """Copy ``module_dir``'s tools and skills into the agent's capability root.

    Args:
        module_dir: The materialized module tree at the deployment root.
        agent_dir: The agent's config root — the directory holding its
            ``arcagent.toml``.
        module: Module name, validated as a single path component so a config
            entry cannot aim the write outside the agent's capability root.

    Returns:
        The destination directory, ``<agent_dir>/capabilities/modules/<module>``.

    Raises:
        BundleMaterializeError: ``module_dir`` is not a directory, carries no
            capability surface to copy, or the write could not complete.
    """
    source = Path(module_dir)
    if not source.is_dir():
        raise BundleMaterializeError(f"not a materialized module directory: {source}")

    dest = capability_dir(agent_dir, module)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{module}{_STAGING_INFIX}"))
    except OSError as exc:
        raise BundleMaterializeError(f"cannot stage into {dest.parent}: {exc}") from exc

    try:
        copied = _stage(source, staging)
        _normalize_modes(staging)
        if not copied:
            raise BundleMaterializeError(
                f"module {module!r} at {source} has no {CAPABILITY_FILE} and no "
                f"{SKILLS_DIR}/ to copy"
            )
        _publish(staging, dest)
    except OSError as exc:
        raise BundleMaterializeError(
            f"cannot copy {module!r} capabilities to {dest}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # A refused copy must leave the agent dir exactly as it was, and the
        # ``modules/`` namespace this function creates is part of "as it was".
        # rmdir only succeeds while it is empty, so a sibling module's copy is
        # never at risk.
        with suppress(OSError):
            dest.parent.rmdir()

    _logger.info(
        "module capabilities copied: module=%s files=%d source=%s dest=%s",
        module,
        copied,
        source,
        dest,
    )
    return dest


def remove_capabilities(agent_dir: Path, *, module: str) -> bool:
    """Delete an agent's copy of a module's capabilities. The inverse of the copy.

    Args:
        agent_dir: The agent's config root.
        module: Module name, validated as a single path component.

    Returns:
        True when a tree was deleted, False when there was nothing to delete —
        an agent that never enabled the module is already in the desired state,
        which is a completion rather than a failure.

    Raises:
        BundleMaterializeError: The tree exists but could not be deleted, or any
            part of it survived. A caller must exit non-zero on this (REQ-338):
            a surviving copy keeps a removed module's tools callable.
    """
    dest = capability_dir(agent_dir, module)
    if dest.is_symlink():
        raise BundleMaterializeError(f"refusing to follow a symlink at {dest}")
    if not dest.exists():
        return False

    try:
        shutil.rmtree(dest)
    except OSError as exc:
        raise BundleMaterializeError(f"could not remove {dest}: {exc}") from exc

    if dest.exists():
        raise BundleMaterializeError(f"{dest} survived removal")

    _logger.info("module capabilities removed: module=%s path=%s", module, dest)
    return True


def _stage(source: Path, staging: Path) -> int:
    """Copy the capability surface into ``staging`` and return the file count.

    Nothing else in the module folder is considered. An allowlist of two names
    rather than "everything except ``_runtime.py``" keeps a file added to a
    module later from silently becoming agent-writable code.
    """
    copied = 0

    capability_file = source / CAPABILITY_FILE
    if capability_file.is_file() and not capability_file.is_symlink():
        copied += _copy_artifact(capability_file, staging / CAPABILITY_FILE)

    skills = source / SKILLS_DIR
    if skills.is_dir() and not skills.is_symlink():
        staged_skills = staging / SKILLS_DIR
        shutil.copytree(skills, staged_skills, symlinks=False, ignore_dangling_symlinks=True)
        copied += sum(1 for path in staged_skills.rglob("*") if path.is_file())

    return copied


def _normalize_modes(root: Path) -> None:
    """Give the staged tree ordinary permissions, whatever the source had.

    Bottom-up so a directory is still writable while its own entries are being
    changed. Without this the copy arrives read-only inside read-only
    directories, and both halves of the lifecycle break silently: the inverse
    remove cannot unlink, and an operator approving the capability cannot write
    the signature sidecar that would let it load.
    """
    os.chmod(root, _DIR_MODE)
    for current, dirs, names in os.walk(root):
        for name in dirs:
            os.chmod(Path(current) / name, _DIR_MODE)
        for name in names:
            os.chmod(Path(current) / name, _FILE_MODE)


def _copy_artifact(source: Path, target: Path) -> int:
    """Copy one artifact plus its signature sidecar, returning the file count.

    The sidecar rides along because it is what makes the copy load: it signs the
    artifact's bytes, and those bytes are unchanged by being copied.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    count = 1

    sidecar = source.with_name(source.name + SIGNATURE_SIDECAR_SUFFIX)
    if sidecar.is_file() and not sidecar.is_symlink():
        shutil.copy2(sidecar, target.with_name(target.name + SIGNATURE_SIDECAR_SUFFIX))
        count += 1
    return count


def _publish(staging: Path, dest: Path) -> None:
    """Swap the staged tree into place, replacing any earlier copy.

    The earlier copy is removed before the rename because ``os.replace`` refuses
    a non-empty directory target. The brief gap that leaves is a state the loader
    already handles — the module simply reads as not copied — which is why this
    does not carry the backup dance :mod:`arcbundle.materializer` needs for the
    read-only deployment tree.
    """
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    os.replace(staging, dest)
