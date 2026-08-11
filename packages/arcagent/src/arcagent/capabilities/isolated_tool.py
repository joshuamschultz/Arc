"""Isolated execution boundary for agent-authored Python tools.

Authored source is parsed in the host for inert decorator metadata, but is never
compiled or executed there.  Each invocation crosses ArcRun's tier-routed
execution backend using a JSON-only request/response contract.
"""

from __future__ import annotations

import ast
import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import arcrun

from arcagent.tools._decorator import ToolMetadata

_MAX_SOURCE_BYTES = 256 * 1024
_MAX_ARGUMENT_BYTES = 256 * 1024
_RESULT_MARKER = "__ARC_CAPABILITY_RESULT__="
_LITERAL_FIELDS = {
    "name",
    "description",
    "classification",
    "capability_tags",
    "when_to_use",
    "requires_skill",
    "version",
    "examples",
    "model_hint",
    "signals_completion",
}
_FORBIDDEN_IMPORT_TIME_NAMES = {"open", "breakpoint", "input"}


class IsolatedCapabilityError(RuntimeError):
    """An authored tool failed at, or violated, the isolation boundary."""


@dataclass(frozen=True)
class AuthoredTool:
    """Static metadata plus the function name to invoke in the guest."""

    function_name: str
    metadata: ToolMetadata


IsolatedRunner = Callable[[str, dict[str, Any]], Awaitable[Any]]


def parse_authored_tools(path: Path) -> tuple[AuthoredTool, ...]:
    """Extract literal ``@tool`` declarations without executing source."""
    source_bytes = path.read_bytes()
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise IsolatedCapabilityError(
            f"authored capability exceeds {_MAX_SOURCE_BYTES} byte source limit"
        )
    try:
        source = source_bytes.decode("utf-8")
        tree = ast.parse(source, filename=str(path))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise IsolatedCapabilityError(f"invalid authored capability source: {exc}") from exc

    tools: list[AuthoredTool] = []
    unsupported: list[str] = []
    for node in tree.body:
        # Sync ``def`` is inspected alongside async and class. Only an async
        # function can BE a tool, but the decorator has to be SEEN to be
        # refused — skipping FunctionDef made ``@tool`` on a sync function
        # invisible, and it was rejected only as a side effect of the file then
        # looking empty. Now that an empty file is legitimate, a mis-authored
        # tool has to be caught on its own merits.
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Module-level statement: nothing to decorate, but it would run at
            # import time in the guest, so screen it for the builtins that turn
            # a parse into an effect.
            touched = {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            }
            forbidden = touched & _FORBIDDEN_IMPORT_TIME_NAMES
            if forbidden:
                raise IsolatedCapabilityError(
                    f"forbidden import-time builtin: {sorted(forbidden)[0]}"
                )
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            decorator_name = _decorator_name(call.func if call is not None else decorator)
            if decorator_name in {"hook", "background_task", "capability"}:
                unsupported.append(decorator_name)
                continue
            if decorator_name != "tool":
                continue
            if not isinstance(node, ast.AsyncFunctionDef) or call is None:
                raise IsolatedCapabilityError("@tool must decorate an async function and use ()")
            tools.append(_parse_tool(node, call))
    if unsupported:
        kinds = ", ".join(sorted(set(unsupported)))
        raise IsolatedCapabilityError(
            f"authored {kinds} decorators are unsupported across the isolation boundary"
        )
    # An empty result is "not a capability", not "a broken capability". An
    # extension bundle names exactly one attachment entrypoint in its manifest,
    # so its adapter variants and helper modules are ordinary Python that
    # declares no tool — refusing them failed the whole install. Nothing is
    # executed either way: the file was still size-checked, parsed, and screened
    # for import-time builtins, and the caller's AST + trust gates ran before
    # this. A file the scanner sees no tool in simply registers no tool.
    return tuple(tools)


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _parse_tool(node: ast.AsyncFunctionDef, call: ast.Call) -> AuthoredTool:
    if call.args:
        raise IsolatedCapabilityError("@tool accepts literal keyword arguments only")
    values: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg not in _LITERAL_FIELDS:
            raise IsolatedCapabilityError("@tool contains an unsupported metadata field")
        try:
            values[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError) as exc:
            raise IsolatedCapabilityError(
                f"@tool metadata {keyword.arg!r} must be a literal"
            ) from exc
    name = values.get("name") or node.name
    description = values.get("description") or ast.get_docstring(node) or ""
    if not isinstance(name, str) or not isinstance(description, str):
        raise IsolatedCapabilityError("@tool name and description must be strings")
    schema = _schema_from_ast(node)
    try:
        meta = ToolMetadata(
            name=name,
            description=description,
            input_schema=schema,
            classification=values.get("classification", "state_modifying"),
            capability_tags=tuple(values.get("capability_tags", ())),
            when_to_use=values.get("when_to_use", ""),
            requires_skill=values.get("requires_skill"),
            version=values.get("version", "1.0.0"),
            examples=tuple(values.get("examples", ())),
            model_hint=values.get("model_hint"),
            signals_completion=values.get("signals_completion", False),
        )
    except (TypeError, ValueError) as exc:
        raise IsolatedCapabilityError(f"invalid @tool metadata: {exc}") from exc
    return AuthoredTool(function_name=node.name, metadata=meta)


def _schema_from_ast(node: ast.AsyncFunctionDef) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    positional = [*node.args.posonlyargs, *node.args.args]
    default_offset = len(positional) - len(node.args.defaults)
    for index, argument in enumerate(positional):
        properties[argument.arg] = _annotation_schema(argument.annotation)
        if index < default_offset:
            required.append(argument.arg)
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
        properties[argument.arg] = _annotation_schema(argument.annotation)
        if default is None:
            required.append(argument.arg)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _annotation_schema(annotation: ast.expr | None) -> dict[str, Any]:
    name = _decorator_name(annotation) if annotation is not None else ""
    return {
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "dict": {"type": "object"},
        "list": {"type": "array"},
    }.get(name, {})


class ArcRunIsolatedRunner:
    """Run authored functions through ArcRun's configured isolation backend."""

    def __init__(self, *, tier: str, timeout_seconds: float = 30) -> None:
        self._tool = arcrun.make_execute_tool(
            tier=tier,
            timeout_seconds=timeout_seconds,
            max_output_bytes=64 * 1024,
        )

    async def __call__(self, source: str, request: dict[str, Any]) -> Any:
        encoded_request = json.dumps(request, separators=(",", ":"))
        if len(encoded_request.encode()) > _MAX_ARGUMENT_BYTES:
            raise IsolatedCapabilityError(
                f"tool arguments exceed {_MAX_ARGUMENT_BYTES} byte IPC limit"
            )
        program = _guest_program(source, encoded_request)
        result_text = await self._tool.execute({"code": program}, arcrun.detached_context())
        try:
            envelope = json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise IsolatedCapabilityError("isolation backend returned malformed output") from exc
        if envelope.get("exit_code") != 0:
            stderr = str(envelope.get("stderr", ""))[-2048:]
            raise IsolatedCapabilityError(f"isolated process failed: {stderr}")
        stdout = str(envelope.get("stdout", ""))
        marker = stdout.rfind(_RESULT_MARKER)
        if marker < 0:
            raise IsolatedCapabilityError("isolated process returned no capability result")
        payload = stdout[marker + len(_RESULT_MARKER) :].splitlines()[0]
        try:
            response = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise IsolatedCapabilityError("isolated capability returned malformed JSON") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error = (
                response.get("error", "unknown isolated failure")
                if isinstance(response, dict)
                else "invalid response"
            )
            raise IsolatedCapabilityError(str(error)[:2048])
        return response.get("result")


def make_isolated_execute(
    *, source: str, function_name: str, runner: IsolatedRunner
) -> Callable[..., Awaitable[Any]]:
    """Create a host proxy that sends only JSON-compatible arguments."""

    async def execute(**kwargs: Any) -> Any:
        try:
            json.dumps(kwargs)
        except (TypeError, ValueError) as exc:
            raise IsolatedCapabilityError("tool arguments must be JSON serializable") from exc
        return await runner(source, {"function": function_name, "arguments": kwargs})

    return execute


def _guest_program(source: str, request: str) -> str:
    source_b64 = base64.b64encode(source.encode()).decode("ascii")
    request_b64 = base64.b64encode(request.encode()).decode("ascii")
    # The shim supplies only decorator behavior needed to define the isolated
    # module. It does not expose an ArcAgent object graph to authored code.
    return f'''import asyncio, base64, json, sys, types
def _stamp(**metadata):
    def decorate(value):
        return value
    return decorate
decorators = types.ModuleType("arcagent.tools._decorator")
decorators.tool = _stamp
tools = types.ModuleType("arcagent.tools")
tools._decorator = decorators
arcagent = types.ModuleType("arcagent")
arcagent.tools = tools
sys.modules["arcagent"] = arcagent
sys.modules["arcagent.tools"] = tools
sys.modules["arcagent.tools._decorator"] = decorators
namespace = {{"__name__": "__isolated_capability__"}}
source = base64.b64decode("{source_b64}").decode("utf-8")
request = json.loads(base64.b64decode("{request_b64}"))
try:
    exec(compile(source, "<isolated-capability>", "exec"), namespace)
    target = namespace[request["function"]]
    result = asyncio.run(target(**request["arguments"]))
    payload = {{"ok": True, "result": result}}
except BaseException as exc:
    payload = {{"ok": False, "error": type(exc).__name__ + ": " + str(exc)}}
print("{_RESULT_MARKER}" + json.dumps(payload, separators=(",", ":")))
'''


__all__ = [
    "ArcRunIsolatedRunner",
    "AuthoredTool",
    "IsolatedCapabilityError",
    "IsolatedRunner",
    "make_isolated_execute",
    "parse_authored_tools",
]
