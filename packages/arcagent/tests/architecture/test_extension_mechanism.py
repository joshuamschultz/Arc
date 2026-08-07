"""SPEC-062 T-916 / T-915 (structural half) — the mechanism is general, or it is nothing.

COMP-021, serving REQ-280 and REQ-278. Two claims are asserted here by reading the
source tree itself, because both are claims *about* the source tree that no runtime
test can make:

* **No core package names a service.** REQ-280 is not a style rule. The moment a
  vendor name appears in core, adding the eleventh connection stops being
  configuration and becomes a core edit — which is the exact failure the whole spec
  exists to prevent. The scan therefore looks for the identifiers of the services
  this spec targets, names the offending file and line, and excludes nothing but
  test trees.
* **The reference extension is genuinely outside.** T-915 proves at runtime that a
  fixture extension installs, registers, and executes. That proof is worth nothing if
  core quietly knows the fixture's name, so the other half of T-915 lives here: the
  fixture is outside every package source tree, and no core file mentions any of its
  identifiers.

The scanner is proved non-vacuous by a meta-test that plants an offending line in a
temporary file and asserts it is found. A conformance test that cannot fail is the
same dead wiring this project has shipped before ([[feedback_producers_unwired_pattern]]).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PACKAGES = _REPO_ROOT / "packages"

#: The reference extension fixture T-915 drives. Named here only so this test can
#: assert that *nothing under a package source tree* names it.
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_extension"

#: Directory names whose contents are not core source. Test trees are excluded because
#: a test may legitimately name a service it is exercising; ``__pycache__`` is noise.
_EXCLUDED_DIRS = frozenset({"tests", "test", "fixtures", "__pycache__", ".venv", "node_modules"})

#: Identifiers of the external services this spec targets. A connector reaches these
#: through :class:`~arcagent.extension.attachment.ExtensionAttachment` and through
#: nothing else, so none of them may appear in a package source file.
#:
#: Bare "google" and "microsoft" are deliberately absent: both name LLM *providers*
#: that ``arcllm`` legitimately implements adapters for, and conflating a model
#: provider with a connected service would make this test assert something REQ-280
#: does not say. The service-specific tokens below have no such second reading.
_SERVICE_PATTERNS: tuple[str, ...] = (
    r"atlassian",
    r"jira",
    r"confluence",
    r"dropbox",
    r"1password",
    r"onepassword",
    r"agentmail",
    r"gmail",
    r"outlook",
    r"onedrive",
    r"sharepoint",
    r"msgraph",
    r"office\s*365",
    r"google\s+(?:drive|calendar|workspace)",
    r"microsoft\s+(?:graph|365)",
)

#: Everything that identifies the reference extension. ``build_native_attachment`` is
#: deliberately NOT here: it is the generic factory convention core asks *every*
#: direct-implementation extension for, so core naming it is the mechanism working.
_FIXTURE_PATTERNS: tuple[str, ...] = (
    r"reference_service",
    r"reference_attachment",
    r"ReferenceAttachment",
    r"reference_echo",
    r"reference_store",
)


def _package_sources() -> list[Path]:
    """Every Python source file shipped by a package, test trees excluded."""
    files: list[Path] = []
    for src in sorted(_PACKAGES.glob("*/src")):
        files.extend(
            path
            for path in sorted(src.rglob("*.py"))
            if not _EXCLUDED_DIRS.intersection(path.parts)
        )
    return files


def _violations(files: Iterable[Path], patterns: Iterable[str]) -> Iterator[tuple[Path, int, str]]:
    """Yield ``(path, line number, line)`` for every line matching any pattern."""
    compiled = [re.compile(rf"\b{pattern}\b", re.IGNORECASE) for pattern in patterns]
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(rule.search(line) for rule in compiled):
                yield path, number, line.strip()


def _render(violations: Iterable[tuple[Path, int, str]]) -> str:
    return "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line}" for path, number, line in violations
    )


# --- the scanner is not vacuous ---------------------------------------------


def test_the_service_scan_finds_a_planted_vendor_name(tmp_path: Path) -> None:
    """A scan that cannot fail proves nothing. Plant one and prove it is caught."""
    planted = tmp_path / "planted.py"
    planted.write_text('"""Atlassian rotates its refresh token."""\n', encoding="utf-8")

    found = list(_violations([planted], _SERVICE_PATTERNS))

    assert [(path.name, number) for path, number, _ in found] == [("planted.py", 1)]


def test_the_service_scan_ignores_an_unrelated_file(tmp_path: Path) -> None:
    """And it must not fire on ordinary prose, or every green run is luck."""
    clean = tmp_path / "clean.py"
    clean.write_text('"""One hook, many connections."""\n', encoding="utf-8")

    assert list(_violations([clean], _SERVICE_PATTERNS)) == []


def test_the_scan_actually_reads_the_package_sources() -> None:
    """Guards the silent-zero failure: an empty file list makes every scan pass."""
    files = _package_sources()

    assert len(files) > 500, "package source discovery collapsed; every scan below is vacuous"
    assert any(path.parts[-2:] == ("extension", "attachment.py") for path in files)


# --- REQ-280: no core package names a service -------------------------------


def test_no_core_package_names_a_connector_target_service() -> None:
    """The governing architecture claim: the mechanism is general, not a set of integrations.

    A vendor name in core means the next connection is a code change, and REQ-278's
    "without modifying any file inside arcagent or any other core package" is false.
    """
    violations = list(_violations(_package_sources(), _SERVICE_PATTERNS))

    assert not violations, (
        "a connector-target service is named inside a core package "
        f"(REQ-280):\n{_render(violations)}"
    )


# --- T-915 structural half: the fixture is genuinely outside ----------------


def test_the_reference_extension_exists_where_the_conformance_test_expects_it() -> None:
    """Guards against a scan below passing because the fixture simply is not there."""
    assert (_FIXTURE_DIR / "extension.toml").is_file()
    assert (_FIXTURE_DIR / "reference_attachment.py").is_file()


def test_the_reference_extension_lives_outside_every_package_source_tree() -> None:
    """An extension inside ``src/`` would prove nothing about extensions outside it."""
    source_trees = [src.resolve() for src in _PACKAGES.glob("*/src")]

    assert not [src for src in source_trees if src in _FIXTURE_DIR.parents]


def test_no_core_package_names_the_reference_extension() -> None:
    """Adding this connection must have required no edit inside a package source tree.

    If core knows the fixture's tools, its module, or its class, the end-to-end proof
    in ``test_extension_conformance.py`` is a special case wearing a mechanism's
    clothes.
    """
    violations = list(_violations(_package_sources(), _FIXTURE_PATTERNS))

    assert not violations, (
        f"a core package names the reference extension (REQ-278):\n{_render(violations)}"
    )


def test_the_fixture_patterns_would_catch_a_core_reference(tmp_path: Path) -> None:
    """The same non-vacuity proof for the fixture scan."""
    planted = tmp_path / "planted.py"
    planted.write_text("from reference_attachment import ReferenceAttachment\n", encoding="utf-8")

    assert len(list(_violations([planted], _FIXTURE_PATTERNS))) == 1


# --- the hook contract itself names nothing concrete ------------------------


@pytest.mark.parametrize(
    "module",
    ["attachment.py", "bridge.py", "loader.py", "manifest.py", "catalog.py"],
)
def test_the_hook_and_its_governance_name_no_service(module: str) -> None:
    """Narrower than the sweep above, and the first place a regression would land."""
    path = _PACKAGES / "arcagent" / "src" / "arcagent" / "extension" / module
    violations = list(_violations([path], _SERVICE_PATTERNS))

    assert not violations, _render(violations)
