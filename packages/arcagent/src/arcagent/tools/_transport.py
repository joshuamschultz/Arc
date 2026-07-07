"""Tool transport types and the native_tool decorator.

The static building blocks that describe a tool: the transport enum, the
RegisteredTool dataclass, the schema-builder decorator, argument-schema
validation, and the small built-ins exposed to tests. These are tool-domain
primitives, so they live with the tools package rather than in the nucleus.

Re-exported through ``arcagent.core.tool_registry`` so existing
imports
(``from arcagent.core.tool_registry import RegisteredTool, ToolTransport,
   native_tool``) keep working unchanged.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from arcagent.core.errors import ToolError

ToolClassification = Literal["read_only", "state_modifying"]


_DEFAULT_PREAMBLE = (
    "You have the following tools available. Use them as needed to accomplish your tasks."
)


class ToolTransport(Enum):
    """Transport type for tool execution."""

    NATIVE = "native"
    MCP = "mcp"
    HTTP = "http"
    PROCESS = "process"


@dataclass
class RegisteredTool:
    """A tool registered in the registry.

    ``classification`` is the SPEC-017 R-020 contract: read-only tools
    may run in parallel batches; state-modifying tools force sequential
    execution. Default is ``"state_modifying"`` (fail-closed) so
    unannotated tools never accidentally race.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    transport: ToolTransport
    execute: Any  # Callable[..., Awaitable[Any]]
    timeout_seconds: int = 30
    source: str = ""
    when_to_use: str = ""
    example: str = ""
    category: str = ""
    classification: ToolClassification = "state_modifying"
    # Capability tags power non-compositional safety checks (SPEC-017
    # SDD §5.2). Examples: "file_read", "file_write", "network_egress",
    # "subprocess", "state_mutation". Empty = no declared capabilities.
    capability_tags: list[str] = field(default_factory=list)
    # SPEC-043 REQ-010c — whether this model-callable capability is skill-backed.
    # arcrun sees every capability as a plain Tool with no skill notion; this
    # marker lets arcagent's tier resolver widen the federal approval set to the
    # full effecting-capability surface (skills + tools), while enterprise covers
    # tools only. Set when a skill is registered as a callable tool.
    skill_backed: bool = False
    # SPEC-017 R-030 — when True, an invocation of this tool ends the
    # ReAct turn and its (validated) arguments become the loop's
    # completion payload. Preserved through ``ToolRegistry.to_arcrun_tools``
    # so ``agent.run()`` remains the documented entry point for
    # structured-output agents (the alternative — bypassing ``agent.run``
    # and calling ``arcrun.run`` directly — forces callers to reach into
    # arcagent internals).
    signals_completion: bool = False


# -- Type map for native_tool decorator schema generation --
_PY_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def native_tool(
    *,
    name: str = "",
    description: str = "",
    source: str = "",
    timeout_seconds: int = 30,
    params: dict[str, str | dict[str, Any]] | None = None,
    required: list[str] | None = None,
    when_to_use: str = "",
    example: str = "",
    category: str = "",
    signals_completion: bool = False,
) -> Callable[..., Any]:
    """Decorator that converts an async function into a RegisteredTool.

    Eliminates boilerplate — schema is built from function signature
    and the optional ``params`` dict. The decorated function gains a
    ``.tool`` attribute holding the RegisteredTool.

    Usage::

        @native_tool(
            description="Send a message",
            source="messaging",
            params={"to": "Recipient URI", "body": "Message body"},
            required=["to", "body"],
        )
        async def messaging_send(to="", body="", **kwargs):
            ...

    ``params`` values can be a string (used as description) or a dict
    with full JSON Schema property fields (type, enum, default, etc).
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        tool_desc = description or fn.__doc__ or ""

        # Build input schema from function signature + params hints.
        properties: dict[str, Any] = {}
        sig = inspect.signature(fn)
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "kwargs") or param.kind == param.VAR_KEYWORD:
                continue

            prop: dict[str, Any] = {}

            # Infer JSON Schema type from annotation or default value.
            annotation = param.annotation
            if annotation is not inspect.Parameter.empty and annotation in _PY_TYPE_MAP:
                prop["type"] = _PY_TYPE_MAP[annotation]
            elif param.default is not inspect.Parameter.empty and param.default is not None:
                default_type = type(param.default)
                if default_type in _PY_TYPE_MAP:
                    prop["type"] = _PY_TYPE_MAP[default_type]

            # Merge caller-supplied param metadata.
            if params and param_name in params:
                hint = params[param_name]
                if isinstance(hint, str):
                    prop["description"] = hint
                elif isinstance(hint, dict):
                    prop.update(hint)

            if prop:
                properties[param_name] = prop

        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required

        tool = RegisteredTool(
            name=tool_name,
            description=tool_desc,
            input_schema=schema,
            transport=ToolTransport.NATIVE,
            execute=fn,
            timeout_seconds=timeout_seconds,
            source=source,
            when_to_use=when_to_use,
            example=example,
            category=category,
            signals_completion=signals_completion,
        )
        fn.tool = tool  # type: ignore[attr-defined]  # reason: decorator attaches RegisteredTool to wrapped Callable; mypy can't model dynamic attrs
        return fn

    return decorator


def _echo_tool(text: str = "") -> str:
    """Built-in echo tool for testing native tool registration."""
    return f"echo: {text}"


def _validate_tool_args(
    tool_name: str,
    args: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    """Validate tool arguments against input schema.

    Checks that all required properties are present and that no
    unknown properties are passed (when additionalProperties is false).
    """
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Check required fields
    for field_name in required:
        if field_name not in args:
            raise ToolError(
                code="TOOL_INVALID_ARGS",
                message=f"Tool '{tool_name}' missing required argument: {field_name}",
                details={"tool": tool_name, "missing": field_name},
            )

    # Check for unknown arguments (if additionalProperties is explicitly false)
    if not schema.get("additionalProperties", True) and properties:
        unknown = set(args) - set(properties)
        if unknown:
            raise ToolError(
                code="TOOL_INVALID_ARGS",
                message=f"Tool '{tool_name}' received unknown arguments: {unknown}",
                details={"tool": tool_name, "unknown": list(unknown)},
            )
