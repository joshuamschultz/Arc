"""Static validation of module configs against their declared models.

A deployed agent whose ``[modules.*.config]`` carries a key its module no
longer accepts fails at first load with "Required module configuration
failed" — after the deploy gate reported green. ``validate_module_configs``
is that gate's missing check: it validates every enabled module's config
table against the module's own ``<Name>Config`` model without configuring
anything, so schema drift is caught before a runtime ever flips.
"""

from __future__ import annotations

from arcagent.core.config import ModuleEntry
from arcagent.core.module_config import validate_module_configs


def test_a_removed_key_is_reported_with_module_and_key() -> None:
    errors = validate_module_configs(
        {"tasks": ModuleEntry(enabled=True, config={"dispatch": True, "data_dir": ""})}
    )

    assert len(errors) == 1
    assert "modules.tasks.config" in errors[0]
    assert "data_dir" in errors[0]


def test_valid_configs_pass_and_disabled_modules_are_skipped() -> None:
    modules = {
        "tasks": ModuleEntry(enabled=True, config={"dispatch": True}),
        "workflows": ModuleEntry(enabled=False, config={"data_dir": "stale-but-disabled"}),
    }

    assert validate_module_configs(modules) == []


def test_unknown_modules_are_not_validated() -> None:
    modules = {"some_external_plugin": ModuleEntry(enabled=True, config={"anything": 1})}

    assert validate_module_configs(modules) == []
