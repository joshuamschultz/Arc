"""Code-repair apply path: commit the patched bundle as an operator revision (SPEC-044 P4).

The golden-task sandbox is the *correctness* gate (it ran before we get here); the
*integrity* gate for the applied artifact is the operator-anchored revision chain
(REQ-012/016, ASI04). :func:`apply_bundle_patch` validates every patched path stays
inside the skill bundle, then hands the whole patch to the injected
:class:`~arcskill.improver.seams.SkillRevisionWriter` as ONE revision. The writer
signs with the operator key and re-verifies the bundle before activation; the
improver holds no signer and never writes a skill file in place, so a failed
commit leaves the active skill untouched.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from arcskill.improver.models import BundlePatch, BundleView
from arcskill.improver.seams import SkillRevisionWriter

_SCRIPT_DIRS = ("scripts", "src")


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


def apply_bundle_patch(
    skill_name: str,
    patch: BundlePatch,
    *,
    writer: SkillRevisionWriter,
    reason: str | None = None,
) -> str:
    """Commit every file in ``patch`` as one new revision of ``skill_name``.

    Every path must be a relative posix path inside the bundle; one escape refuses
    the whole patch before anything is committed (ASI05). Returns the revision
    digest; a writer refusal raises and leaves the active skill as it was.
    """
    for relative in patch.files:
        _check_patch_path(relative)
    return writer.commit(skill_name, dict(patch.files), reason=reason or patch.summary)


def _check_patch_path(relative: str) -> None:
    """Refuse a patch path that is empty, absolute, or climbs out of the bundle."""
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or "\\" in relative
        or str(path) != relative
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"patch path escapes skill bundle: {relative!r}")


__all__ = ["apply_bundle_patch", "build_bundle_view"]
