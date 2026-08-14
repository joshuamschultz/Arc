"""Every state subdirectory the resolver names must be in the migration map.

``paths.py`` names where a thing lives; ``home_migration._LAYOUT`` names how it
gets there from a pre-split home. Those two lists are maintained by hand, and a
missing entry is SILENT: the accessor answers ``<home>/state/<name>``, the data
stays at ``<home>/<name>``, and nothing errors — the deployment simply behaves as
though that data never existed.

It has already happened twice. ``extensions`` was caught in review. ``workflows``
was not, and shipped: migrating a live deployment hid three real workflow bundles
(``client-update-workflow``, ``client-update-workflow-v3``, ``seo``) from the
runner, with no error on any surface.

This test is the thing that makes the two lists impossible to drift apart. It
derives the accessor list from the source rather than restating it, so a NEW
accessor added tomorrow fails here until its migration entry exists.
"""

from __future__ import annotations

import ast
from pathlib import Path

from arctrust import home_migration, paths

#: Accessors that deliberately have no migration entry, with the reason.
_NO_MIGRATION_NEEDED: dict[str, str] = {
    # A parent of migrated children, not a payload directory of its own.
    "gateway_runtime_dir": "lives under gateway/, which migrates as a whole",
}


def _state_subdir_accessors() -> dict[str, str]:
    """Map accessor name -> the ``<arc_state>/<leaf>`` it resolves to.

    Read out of the source: any function whose body returns
    ``arc_state(...) / "<leaf>"`` is, by construction, naming a directory that a
    pre-split home would have had at its root.
    """
    tree = ast.parse(Path(paths.__file__).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Return) or not isinstance(sub.value, ast.BinOp):
                continue
            binop = sub.value
            left, right = binop.left, binop.right
            is_state_call = (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "arc_state"
            )
            if is_state_call and isinstance(right, ast.Constant) and isinstance(right.value, str):
                found[node.name] = right.value
    assert found, "no state-subdirectory accessors found — the AST scan is wrong, not paths.py"
    return found


def test_every_state_subdir_accessor_has_a_migration_entry() -> None:
    """A state dir the resolver names but the migration forgets is silent data loss."""
    accessors = _state_subdir_accessors()
    migrated = set(home_migration._LAYOUT)

    missing = {
        name: leaf
        for name, leaf in accessors.items()
        if leaf not in migrated and name not in _NO_MIGRATION_NEEDED
    }

    assert not missing, (
        "these accessors resolve under state/ but nothing moves the pre-split "
        "directory there, so an existing deployment's data goes invisible after "
        f"migrating — add each to home_migration._LAYOUT: {missing}"
    )


def test_every_migration_entry_names_a_real_accessor() -> None:
    """The inverse: a _LAYOUT entry pointing at an accessor that does not exist
    would fail only when someone migrated a home that happened to contain it."""
    unknown = {
        name: accessor
        for name, accessor in home_migration._LAYOUT.items()
        if not hasattr(paths, accessor)
    }

    assert not unknown, f"_LAYOUT names accessors that do not exist in paths.py: {unknown}"
