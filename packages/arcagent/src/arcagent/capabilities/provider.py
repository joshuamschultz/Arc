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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcrun import CapabilityResult, CapabilitySpec, Tool, detached_context

_logger = logging.getLogger("arcagent.capability_provider")

# telemetry.audit_event(event_type, details) — the sink the provider emits its
# skill-activation signal through. Optional so the provider stays testable and
# usable without a telemetry stack.
AuditSink = Callable[[str, dict[str, Any]], None]

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
        requires_skill: dict[str, str] | None = None,
        audit: AuditSink | None = None,
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
        self._caller_did = caller_did
        # tool name -> the skill that teaches it (R-014). When such a tool is
        # invoked, that skill is activated into context as part of the call
        # (U13) — the requirement was previously only a system-prompt hint the
        # model had to honour voluntarily via ``use_skill``.
        self._requires_skill: dict[str, str] = dict(requires_skill or {})
        self._audit = audit
        # Skills already pulled this run — activation is idempotent (load the
        # body once, not on every call to the requiring tool).
        self._activated: set[str] = set()

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
        """Dispatch a tool call through its policy-wrapped execute (fail-closed).

        If the tool declares a ``requires_skill``, that skill is activated into
        context as part of the call (U13) — see :meth:`_activate_required_skill`.
        """
        tool = self._tools.get(name)
        if tool is None:
            return CapabilityResult(content=f"unknown capability '{name}'", is_error=True)
        try:
            out = str(await tool.execute(dict(args), detached_context()))
            is_error = False
        except Exception as exc:  # reason: surface as an error result, never crash the loop
            _logger.exception("Capability '%s' raised during invoke", name)
            out, is_error = f"{type(exc).__name__}: {exc}", True
        content, extra = await self._activate_required_skill(name, out, caller_did=caller_did)
        return CapabilityResult(content=content, is_error=is_error, extra=extra)

    async def _activate_required_skill(
        self, tool_name: str, out: str, *, caller_did: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Pull the tool's required skill into context, once per run (U13).

        The skill body enters the conversation via the tool result — the same
        channel ``use_skill`` uses (arcrun capabilities.py) — so the model gains
        the skill's instructions the moment the tool it teaches is called, rather
        than relying on the model to voluntarily call ``use_skill`` first.

        Fail-open: an unreadable/absent skill records ``skill_activated=False``
        and returns the tool output unchanged — a missing skill must never break
        the tool call. Idempotent: the body is injected only on first activation.
        """
        required = self._requires_skill.get(tool_name)
        if required is None:
            return out, None
        already = required in self._activated
        body = None if already else await self.load(required, caller_did=caller_did)
        if body is not None:
            self._activated.add(required)
        activated = already or body is not None
        self._emit_activation(tool_name, required, activated=activated, already=already)
        # Generic passthrough dict — arcrun spools it onto the tool_event verbatim
        # (it never learns "skill"); arcui reads it to show "pulled skill: X".
        extra = {"activated_skill": required, "skill_activated": activated}
        if body is None:
            # Already active, or the load failed — return the tool output as-is.
            return out, extra
        return f'<activated-skill name="{required}">\n{body}\n</activated-skill>\n\n{out}', extra

    def _emit_activation(
        self, tool_name: str, skill: str, *, activated: bool, already: bool
    ) -> None:
        """Audit the tool->skill activation so it is observable (U13)."""
        if self._audit is None:
            return
        self._audit(
            "tool.skill_activated",
            {
                "tool": tool_name,
                "requires_skill": skill,
                "skill_activated": activated,
                "already_active": already,
                "actor_did": self._caller_did,
            },
        )


__all__ = ["AgentCapabilityProvider"]
