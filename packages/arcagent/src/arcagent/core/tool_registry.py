"""Tool Registry — register, wrap, and convert tools for ArcRun.

Supports 4 transports: native (Python), MCP, HTTP, and process.
Every tool call is wrapped with pre/post events, policy checks,
timeout enforcement, and audit logging.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from arcrun import Tool as ArcRunTool
from arcrun import ToolContext

from arcagent.core.config import NativeToolEntry, ToolsConfig
from arcagent.core.errors import ToolError, ToolVetoedError
from arcagent.core.module_bus import ModuleBus
from arcagent.core.telemetry import AgentTelemetry

_logger = logging.getLogger("arcagent.tool_registry")

_DEFAULT_PREAMBLE = (
    "You have the following tools available. "
    "Use them as needed to accomplish your tasks."
)


class ToolTransport(Enum):
    """Transport type for tool execution."""

    NATIVE = "native"
    MCP = "mcp"
    HTTP = "http"
    PROCESS = "process"


@dataclass
class RegisteredTool:
    """A tool registered in the registry."""

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
        )
        fn.tool = tool  # type: ignore[attr-defined]
        return fn

    return decorator


def _echo_tool(text: str = "") -> str:
    """Built-in echo tool for testing native tool registration."""
    return f"echo: {text}"


def _validate_module_path(module_ref: str, allowed_prefixes: list[str]) -> None:
    """Validate module path format and check against allowlist.

    Module references must be ``module.path:callable_name``.
    If ``allowed_prefixes`` is non-empty, the module path must
    start with one of the allowed prefixes.
    """
    if ":" not in module_ref:
        raise ToolError(
            code="TOOL_INVALID_MODULE",
            message=f"Invalid module reference (missing ':'): {module_ref}",
            details={"module": module_ref},
        )
    module_path = module_ref.rsplit(":", 1)[0]
    if allowed_prefixes and not any(module_path.startswith(prefix) for prefix in allowed_prefixes):
        raise ToolError(
            code="TOOL_MODULE_NOT_ALLOWED",
            message=(f"Module '{module_path}' not in allowed prefixes: {allowed_prefixes}"),
            details={
                "module": module_path,
                "allowed_prefixes": allowed_prefixes,
            },
        )


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


class ToolRegistry:
    """Register tools from 4 transports, apply policy, wrap with audit."""

    def __init__(
        self,
        config: ToolsConfig,
        bus: ModuleBus,
        telemetry: AgentTelemetry | Any,
    ) -> None:
        self._config = config
        self._bus = bus
        self._telemetry = telemetry
        self._tools: dict[str, RegisteredTool] = {}
        self._prompt_cache: str | None = None
        self._preamble: str = config.preamble or _DEFAULT_PREAMBLE

    @property
    def tools(self) -> dict[str, RegisteredTool]:
        return self._tools

    @property
    def is_prompt_cached(self) -> bool:
        """Whether the prompt catalog is currently cached."""
        return self._prompt_cache is not None

    def format_for_prompt(self) -> str:
        """XML-formatted tool catalog for system prompt injection.

        Returns empty string if no tools are registered.
        Cached — invalidated on register().
        """
        if self._prompt_cache is not None:
            return self._prompt_cache

        if not self._tools:
            self._prompt_cache = ""
            return ""

        lines = ["<available-tools>"]
        lines.append(f"  <preamble>{xml_escape(self._preamble)}</preamble>")

        for tool in sorted(self._tools.values(), key=lambda t: t.name):
            safe_name = xml_escape(tool.name, {'"': "&quot;"})
            safe_desc = xml_escape(tool.description)
            attrs = f'name="{safe_name}"'
            if tool.category:
                escaped_cat = xml_escape(tool.category, {'"': "&quot;"})
                attrs += f' category="{escaped_cat}"'

            lines.append(f"  <tool {attrs}>")
            lines.append(f"    <description>{safe_desc}</description>")
            if tool.when_to_use:
                lines.append(f"    <when-to-use>{xml_escape(tool.when_to_use)}</when-to-use>")
            if tool.example:
                lines.append(f"    <example>{xml_escape(tool.example)}</example>")
            lines.append("  </tool>")

        lines.append("</available-tools>")
        self._prompt_cache = "\n".join(lines)
        return self._prompt_cache

    def register(self, tool: RegisteredTool) -> None:
        """Register a tool after policy check."""
        self._check_policy(tool.name)
        self._tools[tool.name] = tool
        self._prompt_cache = None  # Invalidate cached catalog
        _logger.info("Registered tool: %s (%s)", tool.name, tool.transport.value)

    def _check_policy(self, tool_name: str) -> None:
        """Check tool against allow/deny policy.

        Deny takes precedence when a tool appears in both lists.
        """
        policy = self._config.policy

        if tool_name in policy.deny:
            raise ToolError(
                code="TOOL_POLICY_DENIED",
                message=f"Tool '{tool_name}' is in denylist",
                details={"tool": tool_name, "denylist": policy.deny},
            )

        if policy.allow and tool_name not in policy.allow:
            raise ToolError(
                code="TOOL_POLICY_DENIED",
                message=f"Tool '{tool_name}' not in allowlist",
                details={"tool": tool_name, "allowlist": policy.allow},
            )

    def register_native_tools(self, tools: dict[str, NativeToolEntry]) -> None:
        """Import and register Python function tools.

        Module paths are validated against the configured allowlist
        before import to prevent arbitrary code execution.
        """
        allowed = self._config.allowed_module_prefixes
        for name, entry in tools.items():
            _validate_module_path(entry.module, allowed)
            module_path, func_name = entry.module.rsplit(":", 1)
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)

            async def _async_wrapper(_fn: Any = func, **kwargs: Any) -> Any:
                return _fn(**kwargs)

            tool = RegisteredTool(
                name=name,
                description=entry.description,
                input_schema={"type": "object", "properties": {}},
                transport=ToolTransport.NATIVE,
                execute=_async_wrapper,
                source=entry.module,
            )
            self.register(tool)

    def to_arcrun_tools(self) -> list[ArcRunTool]:
        """Convert all registered tools to ``arcrun.Tool`` instances.

        Each tool's execute is wrapped with:
        1. Pre-tool event (may veto)
        2. Timeout enforcement
        3. Execute actual tool
        4. Post-tool event
        5. Audit event

        Timeout is managed by our wrapper (which also fires bus
        events), so ``ArcRunTool.timeout_seconds`` is left as
        ``None`` to avoid double-timeout behaviour.
        """
        result: list[ArcRunTool] = []
        for tool in self._tools.values():
            wrapped = self._create_wrapped_execute(tool)

            async def arcrun_execute(
                args: dict[str, Any],
                ctx: ToolContext,
                _w: Any = wrapped,
            ) -> str:
                raw_result = await _w(args)
                return str(raw_result)

            result.append(
                ArcRunTool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    execute=arcrun_execute,
                    timeout_seconds=None,
                )
            )
        return result

    def _create_wrapped_execute(self, tool: RegisteredTool) -> Any:
        """Create a wrapped execute function for a tool."""
        bus = self._bus
        telemetry = self._telemetry

        async def wrapped_execute(args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            if args is None:
                args = kwargs

            # 0. Validate arguments against schema
            if tool.input_schema:
                _validate_tool_args(tool.name, args, tool.input_schema)

            # 1. Pre-tool event (may veto)
            ctx = await bus.emit(
                "agent:pre_tool",
                {"tool": tool.name, "args": args},
            )
            if ctx.is_vetoed:
                raise ToolVetoedError(
                    message=f"Tool '{tool.name}' vetoed: {ctx.veto_reason}",
                    details={"tool": tool.name, "reason": ctx.veto_reason},
                )

            # 2. Execute with timeout and telemetry span
            start = time.monotonic()
            try:
                async with telemetry.tool_span(tool.name, args):
                    result = await asyncio.wait_for(
                        tool.execute(**args),
                        timeout=tool.timeout_seconds,
                    )
            except TimeoutError as exc:
                raise ToolError(
                    code="TOOL_TIMEOUT",
                    message=f"Tool '{tool.name}' timed out after {tool.timeout_seconds}s",
                    details={"tool": tool.name, "timeout": tool.timeout_seconds},
                ) from exc
            elapsed = time.monotonic() - start

            # 3. Post-tool event
            await bus.emit(
                "agent:post_tool",
                {"tool": tool.name, "result": result, "duration": elapsed},
            )

            # 4. Audit
            telemetry.audit_event(
                "tool.executed",
                {
                    "tool": tool.name,
                    "transport": tool.transport.value,
                    "duration_ms": round(elapsed * 1000),
                },
            )

            return result

        return wrapped_execute

    async def shutdown(self) -> None:
        """Clean up all tool connections."""
        self._tools.clear()
        self._prompt_cache = None
        _logger.info("Tool registry shut down")
