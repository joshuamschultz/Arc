"""Tool Registry — register, wrap, and convert tools for ArcRun.

Supports 4 transports: native (Python), MCP, HTTP, and process.
Every tool call is wrapped with pre/post events, policy checks,
timeout enforcement, and audit logging.

Sibling modules
---------------
- ``arcagent.core.tool_transport``      — ToolTransport enum,
  RegisteredTool dataclass, ``native_tool`` decorator,
  ``_validate_tool_args``, ``_echo_tool``, ``ToolClassification``.
- ``arcagent.core.tool_policy_bridge``  — caller-DID binding helpers
  (``_is_memory_tool``, ``_bind_caller_did``) plus the
  ``_MEMORY_TOOL_PREFIXES`` / ``_IDENTITY_ARG_NAMES`` constants.

Names from the siblings are re-exported through this module so existing
imports
(``from arcagent.core.tool_registry import RegisteredTool, ToolTransport,
   native_tool, _bind_caller_did``) keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal
from xml.sax.saxutils import escape as xml_escape

from arcrun import Tool as ArcRunTool
from arcrun import ToolContext

from arcagent.core.config import ToolsConfig
from arcagent.core.errors import ToolError, ToolVetoedError
from arcagent.core.module_bus import ModuleBus
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_policy import (
    PolicyContext,
    PolicyDenied,
    PolicyPipeline,
    ToolCall,
)
from arcagent.core.tool_policy_bridge import (
    _IDENTITY_ARG_NAMES,
    _MEMORY_TOOL_PREFIXES,
    _bind_caller_did,
    _is_memory_tool,
)
from arcagent.core.tool_transport import (
    _DEFAULT_PREAMBLE,
    _PY_TYPE_MAP,
    RegisteredTool,
    ToolClassification,
    ToolTransport,
    _echo_tool,
    _validate_tool_args,
    native_tool,
)

_logger = logging.getLogger("arcagent.tool_registry")


__all__ = [
    "_DEFAULT_PREAMBLE",
    "_IDENTITY_ARG_NAMES",
    "_MEMORY_TOOL_PREFIXES",
    "_PY_TYPE_MAP",
    "RegisteredTool",
    "ToolClassification",
    "ToolRegistry",
    "ToolTransport",
    "_bind_caller_did",
    "_echo_tool",
    "_is_memory_tool",
    "_validate_tool_args",
    "native_tool",
]


class ToolRegistry:
    """Register tools from 4 transports, apply policy, wrap with audit.

    When constructed with a :class:`PolicyPipeline`, every dispatch
    runs through it — first-DENY-wins, fail-closed. ``policy_pipeline``
    defaults to ``None`` so tests and bootstrap code can register tools
    without standing up a full pipeline; production wiring in
    ``ArcAgent`` always passes one explicitly. The transport-layer
    ``_bind_caller_did`` defence (ASI-03) runs regardless of whether a
    policy pipeline is configured.
    """

    def __init__(
        self,
        config: ToolsConfig,
        bus: ModuleBus,
        telemetry: AgentTelemetry | Any,
        policy_pipeline: PolicyPipeline | None = None,
        *,
        agent_did: str = "did:arc:unknown",
        tier: Literal["federal", "enterprise", "personal"] = "personal",
        policy_version: str = "v0",
        ui_reporter: Any | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._telemetry = telemetry
        self._policy_pipeline = policy_pipeline
        self._agent_did = agent_did
        self._tier = tier
        self._policy_version = policy_version
        # Duck-typed UIEventReporter — no import of arcui; None = disabled.
        self._ui_reporter: Any | None = ui_reporter
        self._tools: dict[str, RegisteredTool] = {}
        self._prompt_cache: str | None = None
        self._preamble: str = config.preamble or _DEFAULT_PREAMBLE

    def get_classification(self, tool_name: str) -> ToolClassification:
        """Return a tool's classification for dispatch planning.

        Used by arcrun's parallel dispatch to decide whether a batch
        can run concurrently (all read_only) or must run sequential.
        Unknown tools are conservatively treated as state_modifying.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return "state_modifying"
        return tool.classification

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
        """Register a tool, filtered by allow/deny policy.

        Policy semantics:
          - empty allow + empty deny  → all tools register (default)
          - non-empty allow            → only listed tools register
          - non-empty deny             → listed tools are skipped
          - deny takes precedence over allow when both name the same tool

        Denied tools are skipped silently: they do not enter the registry,
        a warning is logged, and a `tool.policy_denied` audit event fires.
        Registration never raises on policy denial — letting an agent start
        cleanly with a least-privilege deny=[`write`,`bash`] config without
        crashing when built-in tools attempt to register.
        """
        if not self._policy_allows(tool.name):
            policy = self._config.policy
            # DEBUG, not WARNING: deny/allow filtering is intentional config-driven
            # behavior, not an error. The audit event below preserves the trail.
            _logger.debug(
                "policy filter: excluded tool %r from registry (allow=%s, deny=%s)",
                tool.name,
                list(policy.allow),
                list(policy.deny),
            )
            self._telemetry.audit_event(
                "tool.policy_denied",
                {
                    "tool": tool.name,
                    "allowlist": list(policy.allow),
                    "denylist": list(policy.deny),
                },
            )
            return
        self._tools[tool.name] = tool
        self._prompt_cache = None  # Invalidate cached catalog
        _logger.info("Registered tool: %s (%s)", tool.name, tool.transport.value)

    def unregister(self, tool_name: str) -> bool:
        """Remove a tool from the registry. Returns True if removed.

        Used by reload paths to drop stale capability-loaded tools
        before re-registering the latest set. Cache is invalidated on
        any removal.
        """
        if tool_name not in self._tools:
            return False
        del self._tools[tool_name]
        self._prompt_cache = None
        _logger.info("Unregistered tool: %s", tool_name)
        return True

    def _policy_allows(self, tool_name: str) -> bool:
        """Return True iff the tool is permitted by current policy.

        Deny takes precedence when a tool appears in both lists.
        """
        policy = self._config.policy
        if tool_name in policy.deny:
            return False
        if policy.allow and tool_name not in policy.allow:
            return False
        return True

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
        """Create a wrapped execute function for a tool.

        Dispatch order — each layer is a single, named guard:
          0. Argument schema validation
          1. Policy pipeline (SPEC-017 R-010/R-011) — first-DENY-wins,
             fail-closed. Denied calls never reach ``execute()``.
          2. Pre-tool event (may veto for e.g. human-in-the-loop)
          3. Execute with timeout + telemetry span
          4. Post-tool event
          5. Audit
        """
        bus = self._bus
        telemetry = self._telemetry
        pipeline = self._policy_pipeline
        agent_did = self._agent_did
        tier = self._tier
        policy_version = self._policy_version
        ui_reporter = self._ui_reporter

        async def wrapped_execute(args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            if args is None:
                args = kwargs

            # 0. Validate arguments against schema
            if tool.input_schema:
                _validate_tool_args(tool.name, args, tool.input_schema)

            # 1. Policy pipeline — the single, authoritative deny path.
            # No sudo mode, no bypass flag. Exceptions in layers are
            # caught by the pipeline and returned as DENY (fail-closed).
            if pipeline is not None:
                call = ToolCall(
                    tool_name=tool.name,
                    arguments=args,
                    agent_did=agent_did,
                    session_id="",
                    classification="unclassified",
                )
                ctx_pol = PolicyContext(
                    tier=tier,
                    policy_version=policy_version,
                    bundle_age_seconds=0.0,
                )
                decision = await pipeline.evaluate(call, ctx_pol)
                if decision.is_deny():
                    raise PolicyDenied(decision)

            # 2. Pre-tool event (may veto)
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

            # 4. Audit — actor_did and tier are mandatory for every tool
            # dispatch so the audit trail answers ASI03: who called what.
            # Unknown DID ("did:arc:unknown") is flagged as a security event.
            if agent_did == "did:arc:unknown":
                telemetry.audit_event(
                    "security.unidentified_tool_call",
                    {
                        "tool": tool.name,
                        "actor_did": agent_did,
                        "tier": tier,
                        "warning": "Tool called without a real agent DID — configure identity.",
                    },
                )
            telemetry.audit_event(
                "tool.executed",
                {
                    "tool": tool.name,
                    "transport": tool.transport.value,
                    "duration_ms": round(elapsed * 1000),
                    "actor_did": agent_did,
                    "tier": tier,
                },
            )

            # Bridge to arcui agent layer — duck-typed, no import of arcui.
            if ui_reporter is not None:
                try:
                    ui_reporter.emit_agent_event(
                        event_type="tool_call",
                        data={
                            "tool_name": tool.name,
                            "actor_did": agent_did,
                            "outcome": "allow",
                            "duration_ms": round(elapsed * 1000),
                            "tier": tier,
                        },
                    )
                except Exception:  # reason: fail-open — log + continue
                    _logger.debug(
                        "ui_reporter.emit_agent_event failed for tool_call", exc_info=True
                    )

            return result

        return wrapped_execute

    async def shutdown(self) -> None:
        """Clean up all tool connections."""
        self._tools.clear()
        self._prompt_cache = None
        _logger.info("Tool registry shut down")
