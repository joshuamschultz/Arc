"""T-937 — adapters are discovered by scanning for a PLATFORM descriptor.

SPEC-065 REQ-308, COMP-003. Today ``registry.discover_plugins`` iterates
``importlib.metadata.entry_points(group="arcgateway.adapters")``, so a platform
is only available if a *distribution* was installed and its entry point
registered. REQ-308 replaces that with a directory scan: an adapter is a folder
under ``arcgateway/adapters/`` exporting a module-level ``PLATFORM`` descriptor,
and adding one is adding a folder.

The claim under test is "no registry edit". A test that merely calls the scan
cannot prove it, because the scan could be a hardcoded list. So the decisive
case writes a *brand new* adapter folder into the real adapters package at run
time and then asserts, of the loaded roster, both that the new platform is in
it and that its name appears nowhere in ``registry.py``'s source. A hardcoded
list cannot pass both halves.

Names this suite assumes of the T-937 implementation (COMP-003/COMP-004 name
them; if the green implementation chooses others, they change here in one
place):

    registry.discover_adapters() -> list[AdapterSpec]
    registry.AdapterSpec(name=..., requires=..., supports=..., build=...)
    arcgateway/adapters/<platform>/__init__.py exporting ``PLATFORM``
"""

from __future__ import annotations

import importlib
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

import arcgateway.adapters as adapters_pkg
from arcgateway.adapters import registry

#: Platforms REQ-308/T-940 require to be discoverable as in-tree folders.
_REQUIRED_PLATFORMS = {"telegram", "slack", "mattermost"}

#: Private modules inside the adapters package. None is a platform, so a scan
#: that lists the directory instead of looking for the descriptor fails here.
_NOT_PLATFORMS = {"base", "registry", "install", "_backoff", "_reconnect", "_text"}

_ADAPTERS_DIR = Path(adapters_pkg.__file__).parent


def _discover() -> list[object]:
    """The roster, via whatever the scan is called."""
    return list(registry.discover_adapters())


def _names(specs: list[object]) -> set[str]:
    return {getattr(spec, "name", "") for spec in specs}


@pytest.fixture
def probe_folder() -> Iterator[str]:
    """Drop a brand-new adapter folder into the real adapters package.

    Unique per run, and removed in teardown whatever the test does, so the
    checkout is left exactly as it was found.
    """
    name = f"probe_{uuid.uuid4().hex[:8]}"
    folder = _ADAPTERS_DIR / name
    folder.mkdir()
    (folder / "__init__.py").write_text(
        "from arcgateway.adapters.registry import AdapterSpec\n"
        "\n"
        "def _build(ctx):\n"
        "    raise NotImplementedError\n"
        "\n"
        f"PLATFORM = AdapterSpec(name={name!r}, requires=(), supports=(), build=_build)\n",
        encoding="utf-8",
    )
    importlib.invalidate_caches()
    try:
        yield name
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        importlib.invalidate_caches()


def test_platforms_are_discovered_by_scanning_for_the_descriptor() -> None:
    """Every in-tree platform folder is in the roster; no distribution needed."""
    found = _names(_discover())

    missing = _REQUIRED_PLATFORMS - found
    assert not missing, (
        f"platforms not discovered by directory scan: {sorted(missing)} "
        f"(found: {sorted(found)})"
    )


def test_the_scan_looks_for_a_descriptor_not_a_directory_listing() -> None:
    """Support modules in the adapters package are not platforms.

    ``base``/``registry``/``_reconnect`` live alongside the platform folders. A
    scan that enumerated the directory would offer them as platforms; one that
    looks for ``PLATFORM`` cannot.
    """
    found = _names(_discover())

    leaked = _NOT_PLATFORMS & found
    assert not leaked, f"non-platform modules discovered as adapters: {sorted(leaked)}"


def test_a_new_folder_is_available_with_no_registry_edit(probe_folder: str) -> None:
    """Adding a folder adds a platform — and registry.py never learned its name.

    Both halves matter. Discovery alone would pass against a hardcoded roster;
    the source check alone would pass against a registry that discovers nothing.
    """
    found = _names(_discover())

    assert probe_folder in found, (
        f"a new adapter folder {probe_folder!r} was not discovered — adding a "
        f"platform still needs more than adding a folder (found: {sorted(found)})"
    )

    source = Path(registry.__file__).read_text(encoding="utf-8")
    assert probe_folder not in source, (
        "the new platform's name appears in registry.py — it was not discovered "
        "by a scan"
    )


def test_a_deleted_folder_leaves_the_roster(probe_folder: str) -> None:
    """Deleting the folder deletes the platform, with no registry edit either."""
    assert probe_folder in _names(_discover())

    shutil.rmtree(_ADAPTERS_DIR / probe_folder)
    importlib.invalidate_caches()

    assert probe_folder not in _names(_discover()), (
        f"{probe_folder!r} survived the deletion of its folder"
    )


def test_every_discovered_platform_carries_a_usable_spec() -> None:
    """A roster entry is an AdapterSpec, not a bare name or a module."""
    specs = _discover()
    assert specs, "the scan discovered no adapters at all"

    for spec in specs:
        assert isinstance(spec, registry.AdapterSpec), (
            f"roster entry {spec!r} is not an AdapterSpec"
        )
        registry.validate_adapter_name(spec.name)
        assert callable(spec.build), f"{spec.name}: spec.build is not callable"
