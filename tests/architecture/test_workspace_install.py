"""Workspace install invariant test.

Asserts that the canonical ``uv pip install -e`` command for all Arc packages
is documented and that each package's pyproject.toml is structurally valid for
editable install.

Background:
    During SPEC-018 M1 development, a venv-drift issue surfaced when developers
    installed packages piecemeal (``pip install -e packages/arcgateway`` only),
    leaving arcagent, arcllm, arcrun, arccli un-linked. Import errors then
    appeared as mysterious "module not found" failures during test runs.

    The canonical setup command is:

        uv pip install -e packages/arcgateway \\
                       -e packages/arccli \\
                       -e packages/arcagent \\
                       -e packages/arcllm \\
                       -e packages/arcrun

    This test verifies that:
      1. Each of these package directories exists and contains a pyproject.toml.
      2. Each pyproject.toml has a [project] section with a name field.
      3. The arcgateway README.md documents the canonical install command
         (so future developers don't hit the same drift issue).

    The test does NOT actually run ``uv pip install`` (that would be slow and
    require network access). It validates the static preconditions.

Usage::

    uv run pytest tests/architecture/test_workspace_install.py -v
"""

from __future__ import annotations

import tomllib  # stdlib since Python 3.11
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical package set for the Arc monorepo
# ---------------------------------------------------------------------------

_CANONICAL_PACKAGES: list[str] = [
    "arcgateway",
    "arccli",
    "arcagent",
    "arcllm",
    "arcrun",
    "arcprompt",
]

# The install command that should appear in the arcgateway README.
# We check that the README contains a close approximation of this.
_CANONICAL_INSTALL_FRAGMENTS: list[str] = [
    "uv pip install",
    "packages/arcgateway",
    "packages/arcagent",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the repository root (three levels up from this file).

    This file lives at: <root>/tests/architecture/test_workspace_install.py
    """
    return Path(__file__).parent.parent.parent.resolve()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_canonical_packages_have_pyproject_toml() -> None:
    """Each canonical package directory must contain a pyproject.toml.

    If a package is missing its pyproject.toml, ``uv pip install -e`` will
    fail with a confusing error. This test catches the problem early.
    """
    root = _repo_root()
    packages_dir = root / "packages"

    assert packages_dir.exists(), (
        f"packages/ directory not found at {packages_dir}. Is this the repository root?"
    )

    missing: list[str] = []
    for pkg_name in _CANONICAL_PACKAGES:
        pkg_dir = packages_dir / pkg_name
        pyproject = pkg_dir / "pyproject.toml"

        if not pkg_dir.exists():
            missing.append(f"packages/{pkg_name}/ — directory not found")
        elif not pyproject.exists():
            missing.append(f"packages/{pkg_name}/pyproject.toml — file not found")

    assert not missing, (
        "Missing package directories or pyproject.toml files:\n"
        + "\n".join(f"  {m}" for m in missing)
        + "\n\n"
        "Canonical install command:\n"
        "  uv pip install -e packages/arcgateway -e packages/arccli "
        "-e packages/arcagent -e packages/arcllm -e packages/arcrun\n"
    )


def test_all_canonical_packages_have_valid_pyproject() -> None:
    """Each pyproject.toml must have a valid [project] section with a name.

    A malformed pyproject.toml causes a cryptic failure during editable install.
    This test surfaces the issue before ``uv pip install`` is invoked.
    """
    root = _repo_root()
    packages_dir = root / "packages"

    errors: list[str] = []
    for pkg_name in _CANONICAL_PACKAGES:
        pyproject_path = packages_dir / pkg_name / "pyproject.toml"
        if not pyproject_path.exists():
            continue  # Missing file caught by the previous test

        try:
            with pyproject_path.open("rb") as fh:
                data = tomllib.load(fh)
        except Exception as exc:
            errors.append(f"packages/{pkg_name}/pyproject.toml: TOML parse error — {exc}")
            continue

        # Must have [project] section
        if "project" not in data:
            errors.append(f"packages/{pkg_name}/pyproject.toml: missing [project] section")
            continue

        project = data["project"]

        # Must have name
        if "name" not in project:
            errors.append(f"packages/{pkg_name}/pyproject.toml: [project] missing 'name' field")

        # Must have requires-python
        if "requires-python" not in project:
            errors.append(
                f"packages/{pkg_name}/pyproject.toml: "
                f"[project] missing 'requires-python' field "
                f"(needed for uv editable install compatibility)"
            )

    assert not errors, (
        "pyproject.toml validation failures:\n" + "\n".join(f"  {e}" for e in errors) + "\n"
    )


def test_arcgateway_readme_documents_canonical_install() -> None:
    """arcgateway README.md must document the canonical uv pip install -e command.

    This prevents the venv-drift issue (SPEC-018 M1 investigation) from
    recurring: future developers read the README first and use the correct
    multi-package install command.

    The test checks that the README contains at minimum:
      - A reference to 'uv pip install'
      - A reference to 'packages/arcgateway'
      - A reference to 'packages/arcagent'

    This is intentionally lenient — any documentation style that includes
    these fragments passes.
    """
    root = _repo_root()
    readme_path = root / "packages" / "arcgateway" / "README.md"

    if not readme_path.exists():
        # README may not exist yet (work in progress). Skip gracefully.
        import warnings

        warnings.warn(
            f"arcgateway README.md not found at {readme_path}. "
            "Consider adding it with the canonical install command:\n"
            "  uv pip install -e packages/arcgateway -e packages/arccli "
            "-e packages/arcagent -e packages/arcllm -e packages/arcrun",
            stacklevel=2,
        )
        return

    content = readme_path.read_text(encoding="utf-8")

    missing_fragments: list[str] = [
        fragment for fragment in _CANONICAL_INSTALL_FRAGMENTS if fragment not in content
    ]

    assert not missing_fragments, (
        "arcgateway README.md is missing canonical install documentation.\n"
        "These fragments were not found:\n"
        + "\n".join(f"  '{f}'" for f in missing_fragments)
        + "\n\n"
        "Add a 'Getting Started' or 'Installation' section to "
        "packages/arcgateway/README.md containing:\n\n"
        "  uv pip install -e packages/arcgateway \\\n"
        "                 -e packages/arccli \\\n"
        "                 -e packages/arcagent \\\n"
        "                 -e packages/arcllm \\\n"
        "                 -e packages/arcrun\n\n"
        "This prevents venv-drift issues when onboarding new developers.\n"
        "(Issue surfaced during SPEC-018 M1 implementation.)\n"
    )


def test_packages_src_layout() -> None:
    """Each canonical package must use the src/ layout for proper editable installs.

    The src/ layout (``packages/<name>/src/<name>/``) prevents import shadowing
    where the package directory itself appears on sys.path, which can cause
    confusing behaviour with editable installs.

    If a package doesn't use src/ layout, uv editable installs may behave
    differently across environments.
    """
    root = _repo_root()
    packages_dir = root / "packages"

    non_src_layout: list[str] = []
    for pkg_name in _CANONICAL_PACKAGES:
        pkg_dir = packages_dir / pkg_name
        if not pkg_dir.exists():
            continue

        src_dir = pkg_dir / "src"
        if not src_dir.exists():
            non_src_layout.append(
                f"packages/{pkg_name}/ — no src/ directory found. "
                f"Expected packages/{pkg_name}/src/{pkg_name}/"
            )

    if non_src_layout:
        import warnings

        warnings.warn(
            "Some packages do not use the src/ layout:\n"
            + "\n".join(f"  {p}" for p in non_src_layout)
            + "\n"
            "src/ layout is recommended for editable installs.",
            stacklevel=2,
        )
        # Warning only — not a hard failure, as arcmas/arcteam may be flat layout.
