"""Every test tree must be addressable by a module name no other tree claims.

CI runs pytest one package at a time. Under that shape each package's ``tests``
directory is the only ``tests`` in its rootdir, so eight packages can all claim
the bare name ``tests`` and every per-package run still passes. Point pytest at
the whole repo and they collide: ``Plugin already registered under a different
name``, collection aborts, and roughly half the suite never runs at all — while
CI stays green the entire time.

That is what these tests guard. They are cheap and structural on purpose: the
expensive proof (actually collecting the repo) already runs whenever anyone
types ``pytest``, and the point here is to fail on the *cause* the moment it is
reintroduced, rather than on a symptom nobody sees until the whole-repo run is
attempted months later.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "packages"

# `from tests.x import y`, `from conftest import y`, `from security.x import y` —
# top-level names that only ever resolved because the default prepend import mode
# put the test directory itself on sys.path. Indented matches count: several of
# these live inside test bodies, which is exactly how a first sweep missed them.
_BARE_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(?:tests|conftest|security)(?:[.\s]|$)",
    re.MULTILINE,
)


def _test_dirs() -> list[Path]:
    """Scoped to ``packages/*/tests`` — the eight trees that actually collided.

    ``evaluations/*/tests`` keeps its ``__init__.py`` and does not collide, so it
    is deliberately out of scope; widening the glob would fail on trees that are
    demonstrably fine. Whole-repo collection is the real proof and runs on every
    invocation — this only pins the cause where it was found.
    """
    dirs = [p for p in PACKAGES.glob("*/tests") if p.is_dir()]
    assert dirs, "no package test trees found — the glob is wrong, not the repo"
    return dirs


def test_no_package_test_tree_is_an_importable_package() -> None:
    """An ``__init__.py`` here re-collides every package on the name ``tests``.

    ``--import-mode=importlib`` names a test module after its path relative to
    rootdir — ``packages.arcagent.tests.conftest`` — but only for a directory
    that is not already an importable package. One ``__init__.py`` short-
    circuits that to the package root and the bare name ``tests`` comes back.
    """
    offenders = [
        str(d.relative_to(REPO_ROOT) / "__init__.py")
        for d in _test_dirs()
        if (d / "__init__.py").exists()
    ]
    assert not offenders, (
        "test trees must not be importable packages, or they all claim the name "
        f"`tests` and the whole-repo run dies in collection: {offenders}"
    )


def test_no_test_imports_a_sibling_by_a_bare_top_level_name() -> None:
    """Helpers are imported by their real path or not at all.

    ``from conftest import MockModel`` resolves only when the test directory is
    itself on sys.path. It is not under importlib mode, so the import raises
    ``ModuleNotFoundError`` in the whole-repo run while passing per package.
    """
    offenders = []
    for d in _test_dirs():
        for f in d.rglob("*.py"):
            for match in _BARE_IMPORT.finditer(f.read_text(encoding="utf-8")):
                line = f.read_text(encoding="utf-8").count("\n", 0, match.start()) + 1
                offenders.append(f"{f.relative_to(REPO_ROOT)}:{line} {match.group().strip()}")
    assert not offenders, (
        "import sibling test helpers by their full path "
        "(`from packages.<pkg>.tests.conftest import ...`), not a bare top-level "
        f"name that only prepend mode could resolve: {offenders}"
    )


def test_every_test_bearing_package_pins_the_import_mode_it_relies_on() -> None:
    """The full paths above only resolve with importlib mode + rootdir on the path.

    A package that keeps the default prepend mode resolves a bare ``tests`` from
    its own rootdir, which is precisely the ambiguity this all exists to remove.
    """
    missing = []
    for d in _test_dirs():
        pyproject = d.parent / "pyproject.toml"
        if not pyproject.exists():
            continue
        text = pyproject.read_text(encoding="utf-8")
        if "[tool.pytest.ini_options]" not in text:
            continue
        for setting in ("--import-mode=importlib", "consider_namespace_packages", "pythonpath"):
            if setting not in text:
                missing.append(f"{pyproject.relative_to(REPO_ROOT)} lacks {setting}")
    assert not missing, missing
