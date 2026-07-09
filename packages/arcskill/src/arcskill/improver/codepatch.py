"""Code-repair apply path — write, agent-DID re-sign, re-verify, reload (SPEC-044 P4).

The golden-task sandbox is the *correctness* gate (it ran before we get here); this
module is the *integrity* gate for the applied artifact (REQ-012/016, ASI04). Every
file a :class:`~arcskill.improver.models.BundlePatch` touches is written, signed with
the injected agent-DID :class:`~arcskill.improver.seams.Signer` (SPEC-033 sidecar), and
**re-verified** against that signature before the skill is reloaded. A missing or
mismatched signature fails closed: the original bytes are restored and
:class:`BundleReverifyError` is raised — the mutated skill never reloads unsigned.

``arctrust`` owns the ``.arcsig`` sidecar format, so the provider-free improver
re-verifies through arctrust directly (no arcagent import). The hub's Sigstore
``verify_bundle`` gate applies to hub-*installed* bundles, not agent-DID-signed local
skills — see the SPEC-044 report deviation note.
"""

from __future__ import annotations

import logging
from pathlib import Path

from arctrust import verify_artifact
from arctrust.artifact import ArtifactSignature

from arcskill.improver._util import atomic_write_text
from arcskill.improver.models import BundlePatch, BundleView
from arcskill.improver.seams import Signer

_logger = logging.getLogger("arcskill.improver.codepatch")

_SIDECAR_SUFFIX = ".arcsig"  # on-disk convention shared with arcagent (SPEC-033)
_SCRIPT_DIRS = ("scripts", "src")


class BundleReverifyError(Exception):
    """A patched bundle file failed agent-DID re-verification (fail-closed)."""


def build_bundle_view(skill_name: str, skill_path: Path) -> BundleView:
    """Read the current skill bundle (SKILL.md text + script bytes) into a view."""
    skill_dir = skill_path.parent
    text = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
    scripts: dict[str, bytes] = {}
    for sub in _SCRIPT_DIRS:
        base = skill_dir / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            scripts[path.relative_to(skill_dir).as_posix()] = path.read_bytes()
    return BundleView(skill_name, text, skill_dir, scripts=scripts)


def apply_bundle_patch(skill_dir: Path, patch: BundlePatch, *, signer: Signer | None) -> None:
    """Write + sign + re-verify every file in ``patch`` under ``skill_dir`` (fail-closed).

    On any re-verification failure the original bytes (and sidecars) are restored and
    :class:`BundleReverifyError` is raised, so a bundle only ever reloads fully signed.
    ``signer=None`` (personal, relaxable) writes the patch without a sidecar.
    """
    snapshot: dict[Path, bytes | None] = {}
    sidecar_snapshot: dict[Path, str | None] = {}
    try:
        for rel, content in patch.files.items():
            target = _safe_join(skill_dir, rel)
            snapshot[target] = target.read_bytes() if target.exists() else None
            sidecar = target.with_name(target.name + _SIDECAR_SUFFIX)
            sidecar_snapshot[sidecar] = (
                sidecar.read_text(encoding="utf-8") if sidecar.exists() else None
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, content.decode("utf-8"))
            if signer is not None:
                signer.sign(target, content)
                if not _reverify(target, content):
                    raise BundleReverifyError(
                        f"re-verification failed for patched file {rel!r} (fail-closed)"
                    )
    except BundleReverifyError:
        _restore(snapshot, sidecar_snapshot)
        raise


def _reverify(path: Path, content: bytes) -> bool:
    """Re-verify ``content`` against ``path``'s agent-DID ``.arcsig`` sidecar."""
    sidecar = path.with_name(path.name + _SIDECAR_SUFFIX)
    if not sidecar.exists():
        return False
    try:
        manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    except Exception:  # reason: a corrupt/forged sidecar is unsigned (fail-closed)
        return False
    return verify_artifact(content, manifest)


def _restore(snapshot: dict[Path, bytes | None], sidecar_snapshot: dict[Path, str | None]) -> None:
    """Roll back written files + sidecars to their pre-patch state."""
    for path, original in snapshot.items():
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
    for sidecar, original_text in sidecar_snapshot.items():
        if original_text is None:
            sidecar.unlink(missing_ok=True)
        else:
            sidecar.write_text(original_text, encoding="utf-8")


def _safe_join(root: Path, rel: str) -> Path:
    """Join ``rel`` under ``root``, rejecting path traversal (ASI05)."""
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise ValueError(f"patch path escapes skill bundle: {rel!r}")
    return target


__all__ = ["BundleReverifyError", "apply_bundle_patch", "build_bundle_view"]
