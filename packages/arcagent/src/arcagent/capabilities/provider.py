"""AgentCapabilityProvider — arcagent's implementation of the arcrun contract.

arcrun's loop runs against a ``CapabilityProvider`` (ADR-023): it ``advertise()``s
a lean manifest, lazily ``load()``s a skill body only when the model reaches for
it, and routes calls through ``invoke()``. arcagent implements that contract over
its existing capability subsystem:

- **advertise** — invocable tools (already policy-wrapped by the core
  ``ToolRegistry``) become lean ``tool`` specs; skills become lean ``skill``
  specs (name + "use when"), so a skill's body never enters the prompt until
  the model calls ``use_skill``.
- **invoke** — dispatches the named tool's policy-wrapped ``execute``; the core
  registry's pipeline (schema → first-DENY-wins policy → veto → audit) runs
  inside, so a denied capability fails closed (AC-4.4).
- **load** — reads the skill's ``SKILL.md`` body on demand (AC-4.2).

Federal tier gates whether **workspace-authored** capabilities are callable at
all: in federal they are neither advertised nor loadable (AC-6.1 / ADR-023 §3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcrun import CapabilityResult, CapabilitySpec, Tool, detached_context

_logger = logging.getLogger("arcagent.capability_provider")

# scan_root of capabilities the agent authored at runtime under <workspace>/capabilities
WORKSPACE_ROOT = "workspace"


@dataclass(frozen=True)
class _Skill:
    """The lean, loadable facts about one skill (no body until load())."""

    name: str
    description: str
    location: Path
    scan_root: str


class AgentCapabilityProvider:
    """Adapts the agent's tools + skills to arcrun's CapabilityProvider.

    Built per run from the core ToolRegistry's policy-wrapped tools and the
    capability registry's skills. ``caller_did`` is the agent's DID — every
    invoke/load carries it for the trust layer.
    """

    def __init__(
        self,
        *,
        tools: list[Tool],
        skills: list[_Skill],
        tier: str,
        caller_did: str,
        ctx_tools: list[Tool] | None = None,
        workspace_authored: frozenset[str] = frozenset(),
    ) -> None:
        federal = tier == "federal"

        def _gated(name: str) -> bool:
            # In federal tier, workspace-authored capabilities are denied at
            # load — neither advertised nor invocable (ADR-023 §3 / AC-6.1).
            return federal and name in workspace_authored

        self._tools: dict[str, Tool] = {t.name: t for t in tools if not _gated(t.name)}
        self._skills: dict[str, _Skill] = {s.name: s for s in skills if not _gated(s.name)}
        # ctx-dependent tools (e.g. spawn) dispatched directly by the loop with
        # its live ToolContext — they read depth/budget from it, so they cannot
        # route through the context-free invoke() path.
        self._ctx_tools: list[Tool] = list(ctx_tools or [])

    def raw_tools(self) -> list[Tool]:
        """Tools the loop must dispatch directly (live ToolContext preserved)."""
        return list(self._ctx_tools)

    def advertise(self) -> list[CapabilitySpec]:
        """Lean manifest: invocable tools + skill menu. No bodies."""
        specs: list[CapabilitySpec] = [
            CapabilitySpec(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                kind="tool",
                parallel_safe=tool.parallel_safe,
                signals_completion=tool.signals_completion,
                timeout_seconds=tool.timeout_seconds,
            )
            for tool in self._tools.values()
        ]
        specs.extend(
            CapabilitySpec(
                name=skill.name,
                description=skill.description,
                input_schema={"type": "object", "properties": {}},
                kind="skill",
            )
            for skill in self._skills.values()
        )
        return specs

    async def load(self, name: str, *, caller_did: str) -> str | None:
        """Read a skill's full body on demand. None for unknown/gated names."""
        skill = self._skills.get(name)
        if skill is None:
            return None
        try:
            return skill.location.read_text(encoding="utf-8")
        except OSError:
            _logger.exception("Failed to read skill body for %s at %s", name, skill.location)
            return None

    async def invoke(
        self, name: str, args: dict[str, Any], *, caller_did: str
    ) -> CapabilityResult:
        """Dispatch a tool call through its policy-wrapped execute (fail-closed)."""
        tool = self._tools.get(name)
        if tool is None:
            return CapabilityResult(content=f"unknown capability '{name}'", is_error=True)
        try:
            out = await tool.execute(dict(args), detached_context())
        except Exception as exc:  # reason: surface as an error result, never crash the loop
            _logger.exception("Capability '%s' raised during invoke", name)
            return CapabilityResult(content=f"{type(exc).__name__}: {exc}", is_error=True)
        return CapabilityResult(content=out)


__all__ = ["AgentCapabilityProvider"]
