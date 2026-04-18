"""Self-modification tool for extensions — SPEC-017 Phase 7 R-050.

``create_extension`` writes a Python extension file + its ``MODULE.yaml``
sidecar to the agent's extensions directory. The agent must call
``reload_artifacts`` afterwards for the extension to take effect.

Tier gate:
  * ``federal`` → DENIED (NIST 800-53 SI-7(15), CM-5, CM-8)
  * ``enterprise`` → allowed with audit event (operator workflow
    couples this with approval)
  * ``personal`` → allowed

The source is passed through :class:`AstValidator` before landing on
disk so banned patterns never get persisted. The MODULE.yaml is
validated against a minimal schema (must contain ``name`` +
``entry_point``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]  # types-PyYAML not a hard dep

from arcagent.core.errors import ToolError
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._dynamic_loader import AstValidator

_logger = logging.getLogger("arcagent.tools.extension_tools")

AuditSink = Callable[[str, dict[str, Any]], None]
Tier = Literal["federal", "enterprise", "personal"]

# Path-safe, matches module loader's implicit convention
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


def _validate_extension_name(name: str) -> None:
    if not _SAFE_NAME_RE.match(name):
        msg = (
            f"Extension name {name!r} is invalid. Use letters, digits, "
            "dash, or underscore only (must start with a letter)."
        )
        raise ValueError(msg)


def _validate_module_yaml(raw_yaml: str) -> dict[str, Any]:
    """Parse ``MODULE.yaml`` and enforce the minimum schema.

    Required keys: ``name``, ``entry_point``. Anything else is
    accepted — the module loader handles secondary validation at
    discovery time.
    """
    try:
        parsed = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as err:
        raise ToolError(
            code="EXTENSION_MODULE_YAML_INVALID",
            message=f"MODULE.yaml is not valid YAML: {err}",
            details={},
        ) from err
    if not isinstance(parsed, dict):
        raise ToolError(
            code="EXTENSION_MODULE_YAML_INVALID",
            message="MODULE.yaml must be a mapping",
            details={},
        )
    for field in ("name", "entry_point"):
        if not parsed.get(field):
            raise ToolError(
                code="EXTENSION_MODULE_YAML_INVALID",
                message=f"MODULE.yaml missing required field {field!r}",
                details={"missing": field},
            )
    return parsed


def _emit(sink: AuditSink | None, event: str, payload: dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        sink(event, payload)
    except Exception:
        _logger.exception("extension_tools audit sink raised; continuing")


def make_create_extension_tool(
    *,
    extensions_dir: Path,
    tier: Tier,
    audit_sink: AuditSink | None = None,
) -> RegisteredTool:
    """Build the ``create_extension`` :class:`RegisteredTool`.

    Extensions differ from dynamic tools in scope — a MODULE.yaml
    sidecar lets them subscribe to events, register multiple tools,
    and integrate with the convention-based loader.
    """

    async def execute(
        name: str = "",
        python_source: str = "",
        module_yaml: str = "",
        **_: Any,
    ) -> str:
        if tier == "federal":
            _emit(
                audit_sink,
                "self_mod.extension_create_denied",
                {"name": name, "tier": tier, "reason": "federal_policy"},
            )
            raise ToolError(
                code="SELF_MOD_FEDERAL_DENIED",
                message=(
                    "Dynamic extension creation is disabled in the federal "
                    "tier (NIST 800-53 SI-7(15), CM-5, CM-8)."
                ),
                details={"name": name, "tier": tier},
            )

        _validate_extension_name(name)
        AstValidator().validate(python_source)
        manifest = _validate_module_yaml(module_yaml)

        # Refuse to mismatch — if the MODULE.yaml names a different
        # module the loader will later fail in confusing ways.
        if manifest.get("name") != name:
            raise ToolError(
                code="EXTENSION_NAME_MISMATCH",
                message=(
                    f"MODULE.yaml name {manifest['name']!r} does not match "
                    f"the requested name {name!r}"
                ),
                details={"expected": name, "got": manifest.get("name")},
            )

        # Persist. Extensions live in a named subdirectory so the
        # MODULE.yaml discovery works. Overwriting an existing one is
        # acceptable — the loader reloads from disk.
        target_dir = extensions_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "__init__.py").write_text(python_source, encoding="utf-8")
        (target_dir / "MODULE.yaml").write_text(module_yaml, encoding="utf-8")

        _emit(
            audit_sink,
            "self_mod.extension_created",
            {"name": name, "tier": tier, "path": str(target_dir)},
        )
        return f"extension:{name} created at {target_dir}"

    return RegisteredTool(
        name="create_extension",
        description=(
            "Create a new extension (Python module + MODULE.yaml). "
            "Source is AST-validated. Not available in the federal tier. "
            "Call reload_artifacts afterwards for the extension to take effect."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Extension name (directory + identifier).",
                },
                "python_source": {
                    "type": "string",
                    "description": "__init__.py contents for the extension.",
                },
                "module_yaml": {
                    "type": "string",
                    "description": "MODULE.yaml contents; must include name + entry_point.",
                },
            },
            "required": ["name", "python_source", "module_yaml"],
            "additionalProperties": False,
        },
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.extension_tools",
        classification="state_modifying",
        capability_tags=["file_write", "state_mutation"],
    )


__all__ = ["Tier", "make_create_extension_tool"]
