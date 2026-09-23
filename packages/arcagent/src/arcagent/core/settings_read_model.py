"""Settings read model — effective values + source, per field (SPEC-085 COMP-002, REQ-459).

Composes on :func:`arcagent.core.config_schema.emit_form_schema` for the
section/field walk (same dotted ids, same section grouping) and adds, per
leaf field, the *effective* value read off a loaded :class:`ArcAgentConfig`
instance and where it came from.

Source is one of:
  * ``"default"`` — the effective value equals the field's declared pydantic
    default (no override applied anywhere in the load chain).
  * ``"file"``    — the effective value differs from the pydantic default,
    i.e. some TOML layer (user-wide or per-agent) set it.

Overlay-source detection (distinguishing a per-agent TOML from a user-wide
one, or a future signed overlay) is a Phase-2 concern — deliberately not
implemented here. ``_resolve_source`` is the single extension point: a
Phase-2 caller can widen the return type beyond ``{"default", "file"}``
without touching the walk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from arcagent.core.config import ArcAgentConfig
from arcagent.core.config_schema import emit_form_schema


def settings_read_model(config: ArcAgentConfig, config_path: Path) -> dict[str, Any]:
    """Return every setting grouped by section, with its value and source.

    ``config`` is an already-loaded, already-validated config instance;
    ``config_path`` is the per-agent TOML it was loaded from (echoed back
    for the caller — this function does not re-read the file).
    """
    schema = emit_form_schema()
    sections: list[dict[str, Any]] = []
    for section in schema["sections"]:
        fields: list[dict[str, Any]] = []
        for field in section["fields"]:
            dotted_id = field["id"]
            value, default = _resolve_value_and_default(config, dotted_id)
            fields.append(
                {
                    "id": dotted_id,
                    "value": _json_safe(value),
                    "source": _resolve_source(value, default),
                }
            )
        sections.append({"id": section["id"], "fields": fields})
    return {"config_path": str(config_path), "sections": sections}


def _resolve_value_and_default(config: BaseModel, dotted_id: str) -> tuple[Any, Any]:
    """Walk ``dotted_id`` against the live instance and its owning class.

    Mirrors ``config_schema._walk_fields``'s descent: every path segment but
    the last names a nested model attribute, so the leaf's ``FieldInfo`` —
    and therefore its declared default — always lives on the type of the
    second-to-last instance reached.
    """
    parts = dotted_id.split(".")
    instance: Any = config
    model_cls: type[BaseModel] = type(config)
    for part in parts[:-1]:
        instance = getattr(instance, part)
        model_cls = type(instance)
    leaf_name = parts[-1]
    value = getattr(instance, leaf_name)
    info: FieldInfo = model_cls.model_fields[leaf_name]
    default = info.get_default(call_default_factory=True)
    return value, default


def _json_safe(value: Any) -> Any:
    """Recursively coerce a live config value into plain JSON-safe data.

    A leaf field's runtime value can still be a tuple (``ValidatorsConfig``'s
    immutable collections) or a dict of nested pydantic models
    (``tools.mcp_servers``) even though the field itself is not walked as a
    section — coerce those into plain list/dict data for the read model, the
    same JSON-safety contract ``config_schema.emit_form_schema`` upholds.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _resolve_source(value: Any, default: Any) -> str:
    """Phase-1 source resolution: default-equality only.

    Phase-2 hook: once overlay provenance is tracked through the load
    chain, this is where a ``"user"``/``"overlay"``/``"env"`` distinction
    would be added — the walk above does not need to change.
    """
    return "default" if value == default else "file"


__all__ = ["settings_read_model"]
