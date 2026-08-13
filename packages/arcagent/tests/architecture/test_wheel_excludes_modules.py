"""The built wheel carries no module source (SPEC-066 T-970, REQ-336).

This is the assertion the whole spec reduces to. Everything else — signing,
verification, atomic materialize, the per-agent capability copy — is machinery
for getting a module onto a box *deliberately*. None of it means anything while
``pip install arc-agent`` still writes ``arcagent/modules/browser/`` to a federal
enclave that was never approved for a browser. Absence has to be a directory
listing, not a config flag someone could flip.

So the test builds the real artifact and reads the real archive. Asserting the
hatchling ``exclude`` key is present would prove only that the config says what
we typed; it would not catch ``artifacts`` re-including the tree, a packages
entry re-adding it, or a future hatchling changing precedence between the two.
The archive is the thing that ships, so the archive is the thing under test.

The source tree KEEPS ``src/arcagent/modules/`` — it is the catalog that
``arc module bundle`` and ``arc module install --from-source`` package from.
Only the built artifact loses it. A test that confused the two would push the
migration in exactly the wrong direction.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

# The repository root: .../packages/arcagent/tests/architecture/<this file>
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SOURCE_CATALOG = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent" / "modules"

_HAVE_UV = shutil.which("uv") is not None


def _build_wheel(out_dir: Path, env_extra: dict[str, str] | None = None) -> Path:
    """Build the real ``arc-agent`` wheel into ``out_dir`` and return its path."""
    import os

    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(
        ["uv", "build", "--package", "arc-agent", "--wheel", "--out-dir", str(out_dir)],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel in {out_dir}, got {wheels}"
    return wheels[0]


def test_the_source_catalog_still_exists() -> None:
    """The tree stays in the repo — the wheel is what loses it, not the checkout.

    Without this, deleting ``src/arcagent/modules/`` outright would make every
    assertion below pass while destroying the catalog bundles are built from.
    """
    assert _SOURCE_CATALOG.is_dir()
    assert any(child.is_dir() for child in _SOURCE_CATALOG.iterdir())


@pytest.mark.skipif(not _HAVE_UV, reason="needs the uv build frontend on PATH")
@pytest.mark.slow
def test_built_wheel_contains_no_module_source(tmp_path: Path) -> None:
    """Build the wheel for real and assert not one module file rode along.

    "No module code" is the requirement, and it is not the same as "no
    ``arcagent/modules`` entries at all". Exactly one file legitimately remains:
    the package ``__init__.py``, which contains no module code and exists so the
    dotted names an installed module imports itself by keep resolving. Every
    module lives one level below that, so the assertion is drawn there.
    """
    wheel = _build_wheel(tmp_path / "dist")
    names = zipfile.ZipFile(wheel).namelist()

    smuggled = [
        name
        for name in names
        if name.startswith("arcagent/modules/") and name != "arcagent/modules/__init__.py"
    ]
    assert not smuggled, f"the wheel still ships module source: {smuggled[:10]}"

    # A wheel that excluded everything would also pass the line above. Pin the
    # nucleus as present so "no modules" cannot be satisfied by "no package".
    assert "arcagent/core/agent.py" in names
    assert "arcagent/core/module_discovery.py" in names
    assert any(name.endswith(".md") and "/context/" in name for name in names), (
        "packaged context templates went missing with the modules"
    )


@pytest.mark.skipif(not _HAVE_UV, reason="needs the uv build frontend on PATH")
@pytest.mark.slow
def test_the_modules_package_itself_still_ships(tmp_path: Path) -> None:
    """The one file that must survive the exclusion.

    Module code addresses itself by dotted name (``from arcagent.modules.browser
    import _runtime``) with no relative imports anywhere in the catalog. Drop
    this file and every installed module fails on its first import, while the
    signed bundle it came from verifies perfectly — a failure that would look
    like a verification bug on every box instead of a packaging bug on one.
    """
    wheel = _build_wheel(tmp_path / "dist")
    names = zipfile.ZipFile(wheel).namelist()

    assert "arcagent/modules/__init__.py" in names


@pytest.mark.skipif(not _HAVE_UV, reason="needs the uv build frontend on PATH")
@pytest.mark.slow
def test_the_wheel_is_byte_identical_regardless_of_tier(tmp_path: Path) -> None:
    """One artifact for every deployment (D-066-3).

    A per-tier wheel would mean a federal box runs a binary that was never built
    or tested anywhere else. Nothing in the build may read the tier, so building
    under a personal and a federal environment must produce the same bytes.
    """
    personal = _build_wheel(tmp_path / "personal", {"ARC_SECURITY_TIER": "personal"})
    federal = _build_wheel(tmp_path / "federal", {"ARC_SECURITY_TIER": "federal"})

    assert hashlib.sha256(personal.read_bytes()).hexdigest() == (
        hashlib.sha256(federal.read_bytes()).hexdigest()
    )
