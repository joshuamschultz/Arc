"""Config form-schema emitter (SPEC-085 COMP-001, REQ-458/466).

Walks the root :class:`arcagent.core.config.ArcAgentConfig` pydantic model —
via ``model_fields``, never an instance — and returns a plain,
JSON-serializable dict describing every settable field: id, type, help text,
enum choices, numeric constraints, and whether an operator may edit it.

Each top-level ``ArcAgentConfig`` field becomes one *section* (e.g.
``security``, ``tools``); nested pydantic models recurse into that same
section's flat field list, with dotted ids (``tools.policy.timeout_seconds``)
tracking the nesting depth. Only leaf (non-model) fields are emitted — a
nested model is never itself a leaf field, it is a container the walk
descends into.

No instance of ``ArcAgentConfig`` is constructed here: this is a schema over
the *class*, safe to call before any agent config exists.
"""

from __future__ import annotations

import types
import typing
from typing import Any

import annotated_types
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from arcagent.core.config import ArcAgentConfig
from arcagent.core.config_loading import _ENV_DENYLIST_PREFIXES

_NONE_TYPE = type(None)


def emit_form_schema() -> dict[str, Any]:
    """Walk ``ArcAgentConfig`` and return a plain-data form schema.

    Zero-argument by design — the schema always describes the canonical
    config class, never a particular loaded instance.
    """
    sections: list[dict[str, Any]] = []
    for name, info in ArcAgentConfig.model_fields.items():
        annotation = _unwrap_optional(info.annotation)
        if _is_model_class(annotation):
            fields = _walk_fields(annotation, name)
        else:
            fields = [_build_field(name, info, annotation)]
        sections.append({"id": name, "fields": fields})
    return {"sections": sections}


def _walk_fields(model_cls: type[BaseModel], prefix: str) -> list[dict[str, Any]]:
    """Recursively flatten ``model_cls``'s fields into dotted leaf entries."""
    fields: list[dict[str, Any]] = []
    for name, info in model_cls.model_fields.items():
        dotted_id = f"{prefix}.{name}"
        annotation = _unwrap_optional(info.annotation)
        if _is_model_class(annotation):
            fields.extend(_walk_fields(annotation, dotted_id))
            continue
        fields.append(_build_field(dotted_id, info, annotation))
    return fields


def _build_field(dotted_id: str, info: FieldInfo, annotation: Any) -> dict[str, Any]:
    editable = _is_editable(dotted_id)
    return {
        "id": dotted_id,
        "type": _annotation_type_name(annotation),
        "help": info.description or "",
        "enum": None,
        "constraints": _constraints(info),
        "tier": "all",
        "advanced": not editable,
        "editable": editable,
    }


def _is_editable(dotted_id: str) -> bool:
    """False when the dotted id matches a denylisted env-override prefix.

    Reuses ``config_loading._ENV_DENYLIST_PREFIXES`` verbatim — the same set
    that blocks ``ARCAGENT_*`` env overrides for security-sensitive keys — so
    the generated form never exposes a field the env-override layer refuses.
    """
    env_path = dotted_id.replace(".", "__").lower()
    return not any(env_path.startswith(prefix) for prefix in _ENV_DENYLIST_PREFIXES)


def _constraints(info: FieldInfo) -> dict[str, Any] | None:
    """Extract ``{"min": ge, "max": le}`` from field metadata, else ``None``."""
    min_value: Any = None
    max_value: Any = None
    found = False
    for item in info.metadata:
        if isinstance(item, annotated_types.Ge):
            min_value = item.ge
            found = True
        elif isinstance(item, annotated_types.Le):
            max_value = item.le
            found = True
    if not found:
        return None
    return {"min": min_value, "max": max_value}


def _unwrap_optional(annotation: Any) -> Any:
    """Collapse ``X | None`` / ``Optional[X]`` to ``X``; pass through otherwise."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [arg for arg in typing.get_args(annotation) if arg is not _NONE_TYPE]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_model_class(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _annotation_type_name(annotation: Any) -> str:
    """Derive the emitted ``type`` string for a leaf (non-model) annotation."""
    origin = typing.get_origin(annotation)
    if origin is not None:
        if origin is dict:
            return "dict"
        if origin in (list, tuple, set, frozenset):
            return "list"
        return "str"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    return "str"


__all__ = ["emit_form_schema"]
