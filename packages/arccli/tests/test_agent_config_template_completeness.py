"""The scaffolded agent config must expose every module setting that exists.

A module gains a field, nobody adds it to ``render_agent_config``, and every
agent built from then on silently runs the default with no line in its TOML to
find, read or change. Older agents drift further with each release. This gate
compares the rendered template against the declared config model of every
module in ``arcagent.modules`` and fails on the first missing key.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, get_args

import arcagent.modules
import pytest
from arcagent.core.module_config import ModuleConfig, config_model_for
from pydantic import BaseModel, ValidationError

from arccli.commands.agent._common import render_agent_config

# capability_import is a service package under modules/, not a configurable
# module: it declares no config model and no [modules.*] table.
_NOT_A_CONFIGURABLE_MODULE = {"capability_import"}


def _module_names() -> list[str]:
    root = Path(arcagent.modules.__file__).parent
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.name.startswith("_")
        and entry.name not in _NOT_A_CONFIGURABLE_MODULE
    )


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel behind a field annotation, unwrapping optionals/unions."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _is_open_table(annotation: Any) -> bool:
    """True for free-form mapping fields whose keys the model does not declare."""
    origin = getattr(annotation, "__origin__", None)
    return origin is dict or annotation is dict


def _declared_keys(model: type[BaseModel], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(field.annotation)
        if nested is not None:
            keys |= _declared_keys(nested, f"{path}.")
        else:
            keys.add(path)
    return keys


def _open_table_keys(model: type[BaseModel], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(field.annotation)
        if nested is not None:
            keys |= _open_table_keys(nested, f"{path}.")
        elif _is_open_table(field.annotation):
            keys.add(path)
    return keys


def _optional_keys(model: type[BaseModel], prefix: str = "") -> set[str]:
    """Fields whose default is ``None`` — TOML has no null, so they document as comments."""
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(field.annotation)
        if nested is not None:
            keys |= _optional_keys(nested, f"{path}.")
        elif field.default is None:
            keys.add(path)
    return keys


def _commented_keys(text: str, module_name: str) -> set[str]:
    """Leaf names shown as ``# key = ...`` inside this module's scaffold blocks."""
    keys: set[str] = set()
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped[1:-1]
            in_block = header == f"modules.{module_name}" or header.startswith(
                f"modules.{module_name}."
            )
            continue
        if not in_block or not stripped.startswith("#"):
            continue
        body = stripped.lstrip("#").strip()
        name, sep, _ = body.partition("=")
        if sep and name.strip().isidentifier():
            keys.add(name.strip())
    return keys


def _flatten(table: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in table.items():
        path = f"{prefix}{key}"
        keys.add(path)
        if isinstance(value, dict):
            keys |= _flatten(value, f"{path}.")
    return keys


@pytest.fixture(scope="module")
def scaffold() -> str:
    return render_agent_config(name="probe", did="did:arc:test:probe")


@pytest.fixture(scope="module")
def rendered(scaffold: str) -> dict[str, Any]:
    return tomllib.loads(scaffold)


@pytest.mark.parametrize("module_name", _module_names())
def test_template_declares_every_module_setting(
    module_name: str, rendered: dict[str, Any], scaffold: str
) -> None:
    model = config_model_for(module_name)
    assert model is not None, (
        f"module {module_name!r} declares no "
        f"{''.join(part.capitalize() for part in module_name.split('_'))}Config "
        "on ModuleConfig — "
        "the scaffold cannot express its settings and validate_module_configs "
        "cannot check them"
    )
    assert issubclass(model, ModuleConfig)

    block = rendered.get("modules", {}).get(module_name)
    assert block is not None, f"[modules.{module_name}] missing from the agent scaffold"
    assert "enabled" in block, f"[modules.{module_name}] must declare `enabled`"

    present = _flatten(block.get("config", {}))
    # A free-form mapping field is satisfied by its table, whatever keys it holds.
    for open_key in _open_table_keys(model):
        if any(key.startswith(f"{open_key}.") for key in present):
            present.add(open_key)

    # A field defaulting to None has no TOML value to write; it earns its line
    # as a commented example so an operator can still find and set it.
    documented = _commented_keys(scaffold, module_name)
    for optional in _optional_keys(model):
        if optional.rsplit(".", 1)[-1] in documented:
            present.add(optional)

    missing = sorted(_declared_keys(model) - present)
    assert not missing, (
        f"[modules.{module_name}.config] is missing {missing} — every field of "
        f"{model.__name__} needs a line in the scaffold"
    )


def test_template_has_no_unknown_module_settings(rendered: dict[str, Any]) -> None:
    """The reverse drift: a scaffold key no module accepts fails at agent load."""
    stale: dict[str, list[str]] = {}
    for name, block in rendered.get("modules", {}).items():
        model = config_model_for(name)
        if model is None:
            continue
        declared = _declared_keys(model) | _open_table_keys(model)
        unknown = sorted(
            key
            for key in _flatten(block.get("config", {}))
            if key not in declared
            and not any(key.startswith(f"{open_key}.") for open_key in _open_table_keys(model))
            and not any(other.startswith(f"{key}.") for other in declared)
        )
        if unknown:
            stale[name] = unknown
    assert not stale, f"scaffold declares settings no module accepts: {stale}"


def test_scaffold_passes_the_runtime_module_config_gate(tmp_path: Path, scaffold: str) -> None:
    """Every scaffolded value must satisfy the model the runtime validates against.

    ``test_template_has_no_unknown_module_settings`` catches a key no model
    accepts; this catches a key that exists but holds a value the model
    refuses — the failure an agent only meets at first load.
    """
    import arcagent

    config_path = tmp_path / "arcagent.toml"
    config_path.write_text(scaffold, encoding="utf-8")

    config = arcagent.load_config(config_path)

    # Not validate_module_configs: that skips disabled modules, and a disabled
    # module's block is exactly what an operator edits before turning it on.
    refused: dict[str, str] = {}
    for name, entry in config.modules.items():
        model = config_model_for(name)
        if model is None:
            continue
        try:
            model.model_validate(entry.config)
        except ValidationError as exc:
            refused[name] = str(exc)
    assert not refused, f"scaffolded values the module model refuses: {refused}"
