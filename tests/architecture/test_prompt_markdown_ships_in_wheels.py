"""Packaging guard: externalized prompt markdown must ship inside each wheel.

editable-system-prompts REQ-140 / T-752: prompts are loaded at runtime, not
optional data. Hatchling drops files not tracked by VCS *or* not declared via
``artifacts`` from the built wheel — the SPEC-047 blueprint lesson. Every
package that ships a ``context/`` prompt directory must declare
``artifacts = ["src/<pkg>/**/*.md"]`` so the markdown lands in the wheel; this
test builds each such wheel and asserts the markdown is present inside it.

The set of prompt-shipping packages is discovered dynamically (any package with
``src/<pkg>/context/*.md``), so a newly migrated package is covered automatically.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_PACKAGES_DIR = _REPO_ROOT / "packages"


def _prompt_shipping_packages() -> list[str]:
    """Return package names that ship at least one ``context/*.md`` prompt file."""
    names: list[str] = []
    for pkg_dir in sorted(_PACKAGES_DIR.iterdir()):
        if not pkg_dir.is_dir():
            continue
        pkg = pkg_dir.name
        context = pkg_dir / "src" / pkg / "context"
        if context.is_dir() and any(context.glob("*.md")):
            names.append(pkg)
    return names


@pytest.mark.parametrize("package", _prompt_shipping_packages())
def test_context_markdown_ships_in_wheel(package: str) -> None:
    """Building ``package`` produces a wheel containing every ``context/*.md`` it declares."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the wheel for this packaging check")

    context = _PACKAGES_DIR / package / "src" / package / "context"
    expected = {p.name for p in context.glob("*.md")}
    assert expected, f"{package} has a context/ dir but no *.md — nothing to assert"

    with tempfile.TemporaryDirectory() as tmp:
        # `uv` is the absolute path from shutil.which — a fixed, trusted dev tool.
        result = subprocess.run(
            [uv, "build", "--wheel", str(_PACKAGES_DIR / package), "--out-dir", tmp],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0, f"wheel build failed for {package}:\n{result.stderr}"
        wheels = list(Path(tmp).glob("*.whl"))
        assert len(wheels) == 1, f"expected exactly one wheel for {package}, got {wheels}"

        with zipfile.ZipFile(wheels[0]) as zf:
            shipped = {
                Path(n).name
                for n in zf.namelist()
                if n.startswith(f"{package}/context/") and n.endswith(".md")
            }

    missing = expected - shipped
    assert not missing, (
        f"{package} wheel is MISSING prompt markdown: {sorted(missing)}.\n"
        f'Declare `artifacts = ["src/{package}/**/*.md"]` under '
        f"[tool.hatch.build.targets.wheel] in packages/{package}/pyproject.toml (REQ-140)."
    )
