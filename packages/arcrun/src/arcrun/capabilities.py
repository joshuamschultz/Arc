"""CapabilityProvider — the single contract arcrun's loop runs against.

arcrun owns the loop, not the capabilities (CLAUDE.md: "Don't have arcrun do
things that belong to agent or arcllm"). Instead of a flat ``list[Tool]``, the
loop takes a ``CapabilityProvider`` (ADR-023): it ``advertise()``s a lean
manifest (name · kind · "use when" · schema — no bodies), lazily ``load()``s a
skill's body only when the model reaches for it, and ``invoke()``s a call,
which the provider routes through its own trust/policy layer.

arcrun stays oblivious to where capabilities come from or how they are trusted —
it builds its internal ``ToolRegistry`` from ``advertise()`` and routes dispatch
to ``invoke()``. The event/cancellation/timeout machinery in ``executor.py`` is
unchanged; it simply wraps the ``invoke`` call.

``StaticProvider`` adapts a fixed ``list[Tool]`` to the contract — the simple
case (tests, in-process tool sets) where there is no lazy loading or external
trust layer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arcrun.types import Tool, ToolContext

# Built-in meta-tool the model calls to pull a skill's body into context.
USE_SKILL_TOOL = "use_skill"


@dataclass
class CapabilitySpec:
    """Lean advertise unit — all that enters the model's tool list.

    ``kind`` distinguishes a directly-invocable ``"tool"`` from a ``"skill"``
    whose body is fetched on demand via the ``use_skill`` meta-tool. No bodies,
    no file contents — just enough for the model to decide what to reach for.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    kind: str = "tool"
    parallel_safe: bool = False
    signals_completion: bool = False
    timeout_seconds: float | None = None


@dataclass
class CapabilityResult:
    """Outcome of a capability invocation."""

    content: str
    is_error: bool = False


@runtime_checkable
class CapabilityProvider(Protocol):
    """The contract arcrun's loop runs against (ADR-023).

    Optional extension: a provider MAY also define ``raw_tools() -> list[Tool]``
    for capabilities that must be dispatched with the loop's live ToolContext
    (they read depth/budget/cancellation from it — e.g. spawn) rather than
    through the context-free ``invoke``. ``provider_tools`` dispatches those
    directly and routes everything else in ``advertise()`` through ``invoke``.
    """

    def advertise(self) -> list[CapabilitySpec]:
        """Lean manifest for the model: name · kind · "use when" · schema."""
        ...

    async def load(self, name: str, *, caller_did: str) -> str | None:
        """Lazily fetch the heavy body for one capability (a skill's full
        instructions). ``None`` for plain tools or unknown names."""
        ...

    async def invoke(self, name: str, args: dict[str, Any], *, caller_did: str) -> CapabilityResult:
        """Dispatch a call — runs through the provider's trust/policy layer."""
        ...


def detached_context() -> ToolContext:
    """A minimal ToolContext for providers that wrap plain ``args -> str`` tools.

    arcrun's executor already emits ``tool.start``/``tool.end`` and enforces
    timeout/cancellation around the invoke, so a wrapped tool that ignores its
    context (the common case) runs correctly with this detached one.
    """
    return ToolContext(
        run_id="",
        tool_call_id="",
        turn_number=0,
        event_bus=None,
        cancelled=asyncio.Event(),
    )


class StaticProvider:
    """Adapt a fixed ``list[Tool]`` to the CapabilityProvider contract.

    The simple case: a known set of in-process tools, no lazy skill bodies, no
    external trust layer. ``advertise()`` exposes the tools' schemas; ``invoke``
    runs the matching tool; ``load`` is always ``None``.
    """

    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def raw_tools(self) -> list[Tool]:
        """The wrapped Tools themselves.

        ``provider_tools`` uses these directly for a StaticProvider so the loop
        dispatches them with its live ToolContext (depth/budget/cancellation) —
        there is no opaque trust layer to route through, so wrapping them behind
        a context-free ``invoke`` would needlessly strip their context.
        """
        return list(self._tools.values())

    def advertise(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                kind="tool",
                parallel_safe=t.parallel_safe,
                signals_completion=t.signals_completion,
                timeout_seconds=t.timeout_seconds,
            )
            for t in self._tools.values()
        ]

    async def load(self, name: str, *, caller_did: str) -> str | None:
        return None

    async def invoke(
        self, name: str, args: dict[str, Any], *, caller_did: str
    ) -> CapabilityResult:
        tool = self._tools.get(name)
        if tool is None:
            return CapabilityResult(content=f"tool '{name}' not found", is_error=True)
        out = await tool.execute(args, detached_context())
        return CapabilityResult(content=out)


def _skill_menu(skills: list[CapabilitySpec]) -> str:
    """Render the lean skill menu for the use_skill tool description."""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return "Available skills (call use_skill with one name to load its full instructions):\n" + (
        "\n".join(lines) if lines else "(none)"
    )


def provider_tools(provider: CapabilityProvider, *, caller_did: str) -> list[Tool]:
    """Build the loop's internal ToolRegistry tools from ``provider.advertise()``.

    Directly-invocable capabilities (``kind == "tool"``) become Tools whose
    ``execute`` routes to ``provider.invoke`` (carrying ``caller_did`` for the
    provider's policy layer). Skills (``kind == "skill"``) are not invocable
    tools — they are listed under a single built-in ``use_skill`` meta-tool that
    calls ``provider.load`` to splice the body into the next turn (lazy,
    model-driven retrieval — ADR-023). Unused skills cost ~one menu line.

    A provider may expose ``raw_tools()`` for capabilities that must run with the
    loop's live ToolContext (depth/budget/cancellation — e.g. spawn) or that have
    no opaque trust layer to route through (StaticProvider). Those are dispatched
    directly; everything else in ``advertise()`` routes through ``invoke``.
    """
    raw = getattr(provider, "raw_tools", None)
    raw_tools: list[Tool] = list(raw()) if callable(raw) else []
    raw_names = {t.name for t in raw_tools}

    specs = provider.advertise()
    tools: list[Tool] = list(raw_tools)
    tools.extend(
        _invoke_tool(spec, provider, caller_did=caller_did)
        for spec in specs
        if spec.kind != "skill" and spec.name not in raw_names
    )
    skills = [s for s in specs if s.kind == "skill"]
    if skills:
        tools.append(_use_skill_tool(skills, provider, caller_did=caller_did))
    return tools


def _invoke_tool(spec: CapabilitySpec, provider: CapabilityProvider, *, caller_did: str) -> Tool:
    async def _execute(args: dict[str, Any], ctx: ToolContext, _name: str = spec.name) -> str:
        result = await provider.invoke(_name, args, caller_did=caller_did)
        if result.is_error:
            return f"Error: {result.content}"
        return result.content

    return Tool(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema,
        execute=_execute,
        timeout_seconds=spec.timeout_seconds,
        parallel_safe=spec.parallel_safe,
        signals_completion=spec.signals_completion,
    )


def _use_skill_tool(
    skills: list[CapabilitySpec], provider: CapabilityProvider, *, caller_did: str
) -> Tool:
    async def _execute(args: dict[str, Any], ctx: ToolContext) -> str:
        name = str(args.get("name", ""))
        body = await provider.load(name, caller_did=caller_did)
        if body is None:
            return f"Error: skill '{name}' not found"
        return body

    return Tool(
        name=USE_SKILL_TOOL,
        description=_skill_menu(skills),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the skill to load."}
            },
            "required": ["name"],
        },
        execute=_execute,
    )


__all__ = [
    "USE_SKILL_TOOL",
    "CapabilityProvider",
    "CapabilityResult",
    "CapabilitySpec",
    "StaticProvider",
    "provider_tools",
]
