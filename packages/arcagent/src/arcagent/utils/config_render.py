"""Render the full, documented Arc config surface straight from its models.

H-039: an operator must be able to open ``arcagent.toml``/``arcllm.toml``/
``arcrun.toml`` and find EVERY option Arc supports — including one that is off
— rather than discover it only by reading Pydantic source. Before this module
that surface was a hand-typed template string (``arccli``'s old
``_DEFAULT_CONFIG``): a field added to a model needed a second, manual edit to
ever reach a generated file, and the two silently drifted.

This module is the single generator. It walks a Pydantic model's
``model_fields`` — recursing into nested ``BaseModel`` fields as TOML
sub-tables — and renders one line per field, at its default, with a doc
comment. A field added to ``ArcAgentConfig`` (or any module's ``<Name>Config``,
resolved through :func:`arcagent.core.module_config.config_model_for`) appears
in every generated file the next time it is rendered — no second list to keep
in sync. Reused by the new-agent scaffold (``arccli.commands.agent._common``)
and by arcui's config surface (``arcui.routes.agent_detail.config``), so both
show the same option set.

Doc comments come, in priority order, from: (1) ``Field(description=...)``,
(2) a comment block written directly above the field in ITS OWN source, (3) a
same-line trailing ``# ...`` comment on the field's own definition line. All
three read the model; none is a second copy of it. A field with none of the
three renders with no comment — a cue to document it at the model, not here.

TOML has no null: a field whose default is ``None`` cannot be written as a
live key, so it renders commented (``# key =``) — present and documented, just
off, exactly the shape H-039 asked for at the module level and now gets
everywhere.
"""

from __future__ import annotations

import inspect
import re
import tomllib
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from arcagent.core.config import ArcAgentConfig, ArcRunConfig, BudgetConfig, EvalConfig, LLMConfig
from arcagent.core.module_config import BUILTIN_MODULE_DEFAULTS, config_model_for
from arcagent.utils.toml_writer import format_header, format_scalar

# arcagent.toml's sibling files own these ArcAgentConfig fields — arcllm.toml
# takes [llm]/[eval]/[budget] (arcagent.core.config_loading.compose_raw_config
# is the one place that draws this line; mirrored here as a frozenset rather
# than re-deriving it, since that function's own list is a raw-dict merge
# detail, not a model fact) and arcrun.toml takes ``arcrun`` at its OWN
# document root (no ``[arcrun]`` header — see ``render_arcrun_toml``).
_SIBLING_FILE_FIELDS = frozenset({"llm", "eval", "budget", "arcrun"})

_UNSET = object()


def _direct_nested_model(annotation: Any) -> type[BaseModel] | None:
    """The bare ``BaseModel`` a field's annotation names, or ``None``.

    Deliberately narrower than "any BaseModel reachable from this
    annotation": ``dict[str, ModuleEntry]``/``dict[str, AgentRoute]``-style
    fields are dynamic, operator-populated collections (``routes``,
    ``modules``, ``mcp_servers``, ...) — they render as an empty table/array,
    never as one fixed sub-section shaped like their value type.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


_TRAILING_COMMENT = re.compile(r"#\s*(.+)$")
_FIELD_LINE = re.compile(r"^ {4}([a-zA-Z_]\w*)\s*:\s*[^=]+=.*$")


def _source_comments(model_cls: type[BaseModel]) -> dict[str, str]:
    """Best-effort field -> comment map read from the model's own source.

    Looks for two shapes around a ``    name: Type = value`` class-body line:
    a same-line trailing ``# comment``, or a contiguous ``#``-only block
    directly above the line with no blank line between. Neither requires a
    code change — a model that already comments its fields is documented for
    free. ``Field(description=...)`` (checked first by the caller) always
    wins when both exist.
    """
    try:
        source_lines = inspect.getsource(model_cls).splitlines()
    except (OSError, TypeError):
        return {}
    comments: dict[str, str] = {}
    for index, line in enumerate(source_lines):
        match = _FIELD_LINE.match(line)
        if match is None:
            continue
        name = match.group(1)
        trailing = _TRAILING_COMMENT.search(line)
        if trailing:
            comments[name] = trailing.group(1).strip()
            continue
        block: list[str] = []
        cursor = index - 1
        while cursor >= 0:
            stripped = source_lines[cursor].strip()
            if not stripped.startswith("#"):
                break
            block.append(stripped.lstrip("#").strip())
            cursor -= 1
        if block:
            comments[name] = " ".join(reversed(block))
    return comments


def _field_comment(field: FieldInfo, name: str, source_comments: dict[str, str]) -> str:
    if field.description:
        return field.description
    return source_comments.get(name, "")


def _default_value(field: FieldInfo) -> Any:
    if field.default_factory is not None:
        return field.default_factory()  # type: ignore[call-arg]
    return field.default


def render_model_lines(
    model_cls: type[BaseModel],
    *,
    overrides: dict[str, Any] | None = None,
    path: tuple[str, ...] = (),
    exclude: frozenset[str] = frozenset(),
) -> list[str]:
    """Render one model's fields as TOML lines (no header for itself).

    Scalars come first, then each nested ``BaseModel`` field's own
    ``[dotted.path]`` sub-table — the ordering TOML requires (a bare key can
    never follow a table header for the same table). ``overrides`` supplies
    values for fields with no usable Pydantic default (required fields like
    ``AgentConfig.name``) and lets a caller apply a deliberate scaffold
    decision (e.g. the web module's keyless ``extract_provider``) without
    hand-copying every OTHER field just to change one. ``exclude`` skips a
    field entirely — for a field a DIFFERENT, already-correct generator owns
    (``LLMConfig.modules``: a free-form dict the ``[llm.modules.*]`` commented
    surface documents header-by-header; a live ``modules = {}`` line here
    would collide with that surface the moment one of its headers is
    uncommented).
    """
    overrides = overrides or {}
    source_comments = _source_comments(model_cls)
    scalar_lines: list[str] = []
    nested: list[tuple[str, type[BaseModel], dict[str, Any]]] = []

    for name, field in model_cls.model_fields.items():
        if name in exclude:
            continue
        override_value = overrides.get(name, _UNSET)
        nested_model = _direct_nested_model(field.annotation)
        if nested_model is not None:
            sub_overrides = override_value if isinstance(override_value, dict) else {}
            nested.append((name, nested_model, sub_overrides))
            continue

        if override_value is not _UNSET:
            value = override_value
        elif not field.is_required():
            value = _default_value(field)
        else:
            raise ValueError(
                f"{model_cls.__name__}.{name} has no default and no override — "
                "config_render needs an explicit value for every required field"
            )

        comment = _field_comment(field, name, source_comments)
        if isinstance(value, tuple):
            value = list(value)
        if value is None:
            # TOML has no null. Render commented: present, documented, off —
            # never silently omitted (H-039).
            line = f"# {name} ="
        else:
            line = f"{name} = {format_scalar(value)}"
        if comment:
            line += f"  # {comment}"
        scalar_lines.append(line)

    lines = list(scalar_lines)
    for name, nested_model, sub_overrides in nested:
        lines.append("")
        lines.append(f"[{format_header([*path, name])}]")
        lines.extend(render_model_lines(nested_model, overrides=sub_overrides, path=(*path, name)))
    return lines


def render_section(
    model_cls: type[BaseModel],
    path: tuple[str, ...],
    *,
    overrides: dict[str, Any] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> str:
    """Render one model as a complete ``[path]`` section (header + fields)."""
    header = f"[{format_header(list(path))}]" if path else ""
    lines = [header] if header else []
    lines.extend(render_model_lines(model_cls, overrides=overrides, path=path, exclude=exclude))
    return "\n".join(lines)


def render_modules_block(*, module_overrides: dict[str, dict[str, Any]] | None = None) -> str:
    """Render ``[modules.<name>]`` + ``[modules.<name>.config]`` for every
    built-in module — including one shipped OFF — from its own ``<Name>Config``.

    ``BUILTIN_MODULE_DEFAULTS`` (which modules start enabled) is a product
    decision, not a model fact, so it lives beside this generator rather than
    being invented here (see its docstring). Every field of every module's
    config model is still rendered by :func:`render_model_lines` off the
    model itself — this function only supplies the on/off default and, via
    ``module_overrides``, the handful of deliberate scaffold choices (e.g.
    the web module's keyless ``extract_provider``) that differ from a bare
    Pydantic default.
    """
    module_overrides = module_overrides or {}
    parts: list[str] = []
    for name, enabled in BUILTIN_MODULE_DEFAULTS.items():
        block = [f"[modules.{name}]", f"enabled = {format_scalar(enabled)}", "priority = 100"]
        model = config_model_for(name)
        if model is not None:
            block.append("")
            block.append(f"[modules.{name}.config]")
            block.extend(
                render_model_lines(
                    model,
                    overrides=module_overrides.get(name),
                    path=("modules", name, "config"),
                )
            )
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def render_arcagent_toml(
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
    module_overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render every ``ArcAgentConfig`` field that lives in ``arcagent.toml``.

    Every field EXCEPT the ones the sibling files own (``_SIBLING_FILE_FIELDS``)
    and ``modules`` (dynamic — rendered by :func:`render_modules_block`, keyed
    off :data:`~arcagent.core.module_config.BUILTIN_MODULE_DEFAULTS` since
    Pydantic carries no "which modules ship on" fact) is walked straight off
    ``ArcAgentConfig.model_fields``: add a field to the model and it appears
    here on the next render, no second edit required.
    """
    overrides = overrides or {}
    parts: list[str] = []
    for name, field in ArcAgentConfig.model_fields.items():
        if name in _SIBLING_FILE_FIELDS or name == "modules":
            continue
        nested_model = _direct_nested_model(field.annotation)
        if nested_model is None:  # pragma: no cover - every remaining field nests today
            continue
        parts.append(render_section(nested_model, (name,), overrides=overrides.get(name)))
    parts.append(render_modules_block(module_overrides=module_overrides))
    return "\n\n".join(parts) + "\n"


def render_arcllm_sections(*, llm_overrides: dict[str, Any] | None = None) -> str:
    """Render arcllm.toml's model-owned sections: ``[llm]``, ``[eval]``, ``[budget]``.

    The per-agent ``[llm.modules.*]`` override surface is a SEPARATE, already
    model-driven generator (``arccli.commands._arcllm_surface``, derived from
    arcllm's own packaged ``config.toml``) — arcagent must never import
    arcllm, so that piece stays in the layer that already depends on it and
    is appended by the caller, not rendered here.
    """
    parts = [
        render_section(
            LLMConfig, ("llm",), overrides=llm_overrides, exclude=frozenset({"modules"})
        ),
        render_section(EvalConfig, ("eval",)),
        render_section(BudgetConfig, ("budget",)),
    ]
    return "\n\n".join(parts) + "\n"


def default_agent_config_dict(
    *, name: str = "agent", tier: str = "personal", did: str = ""
) -> dict[str, Any]:
    """The bare-default ``arcagent.toml`` surface, parsed to a plain dict.

    Uses the SAME generator as the new-agent scaffold, but with none of that
    scaffold's opinionated business overrides (``org = "local"``, "ship
    memory/tasks/messaging on", the web module's keyless provider, ...) —
    just each model's own Pydantic default. Reused by arcui's "show full
    config" surface (:mod:`arcui.routes.agent_detail.config`) to fill in a
    key an on-disk file happens not to carry (an older agent scaffolded
    before a field existed, or one hand-edited down to a minimal file) so
    the option is still visible, just at its default. Round-trips through
    ``tomllib`` rather than ``model_dump()`` directly so a caller sees
    EXACTLY what the generated TOML would parse to (including the "field
    defaults to None -> commented out, key absent" shape), not a richer
    Python-side view the file itself never carries.
    """
    text = render_arcagent_toml(
        overrides={
            "agent": {"name": name},
            "identity": {"did": did},
            "security": {"tier": tier},
            "telemetry": {"service_name": name},
        }
    )
    return tomllib.loads(text)


def render_arcrun_toml() -> str:
    """Render arcrun.toml: ``ArcRunConfig`` fields at the file's OWN root.

    No ``[arcrun]`` header — ``compose_raw_config`` reads this file's raw
    top-level keys directly as ``ArcRunConfig``'s fields (the ``arcrun.toml``
    file IS the model's root), unlike ``[llm]``/``[eval]``/``[budget]`` which
    are sub-tables inside arcllm.toml.
    """
    return "\n".join(render_model_lines(ArcRunConfig)) + "\n"


__all__ = [
    "default_agent_config_dict",
    "render_arcagent_toml",
    "render_arcllm_sections",
    "render_arcrun_toml",
    "render_model_lines",
    "render_modules_block",
    "render_section",
]
