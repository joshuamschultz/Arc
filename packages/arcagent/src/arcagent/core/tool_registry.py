"""Tool Registry — register, wrap, and convert tools for ArcRun.

Supports 4 transports: native (Python), MCP, HTTP, and process.
Every tool call is wrapped with pre/post events, policy checks,
timeout enforcement, and audit logging.

Sibling modules
---------------
- ``arcagent.tools._transport``         — ToolTransport enum,
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
from contextlib import nullcontext
from typing import Any, Literal
from xml.sax.saxutils import escape as xml_escape

from arcrun import Tool as ArcRunTool
from arcrun import ToolContext
from arctrust import AgentIdentity

from arcagent.core.config import ToolsConfig
from arcagent.core.errors import ToolError, ToolVetoedError
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    SessionCapabilityLedger,
    current_session_id,
    legs_for_tags,
)
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_policy import (
    PolicyContext,
    PolicyDenied,
    PolicyPipeline,
    ToolCall,
    sign_call,
)
from arcagent.core.tool_policy_bridge import (
    _IDENTITY_ARG_NAMES,
    _MEMORY_TOOL_PREFIXES,
    _bind_caller_did,
    _is_memory_tool,
)
from arcagent.tools._policy_fill import build_clearance_context, build_provider_usage
from arcagent.tools._transport import (
    _DEFAULT_PREAMBLE,
    _PY_TYPE_MAP,
    RegisteredTool,
    ToolClassification,
    ToolTransport,
    _echo_tool,
    _validate_tool_args,
    native_tool,
)
from arcagent.tools.human_gate import HumanGate

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
        identity: AgentIdentity | None = None,
        agent_did: str = "did:arc:unknown",
        tier: Literal["federal", "enterprise", "personal"] = "personal",
        policy_version: str = "v0",
        capability_ledger: SessionCapabilityLedger | None = None,
        human_gate: HumanGate | None = None,
        provider_label: str | None = None,
        resource_classifications: dict[str, str] | None = None,
        classification_strict: bool = False,
    ) -> None:
        self._config = config
        self._bus = bus
        self._telemetry = telemetry
        self._policy_pipeline = policy_pipeline
        # SPEC-035 — lethal-trifecta accumulation + human-approval gate. Both
        # optional so bootstrap/tests can register tools without them; ArcAgent
        # wires them in production. When absent, dispatch behaves as before.
        self._capability_ledger = capability_ledger
        self._human_gate = human_gate
        # Signing identity for tool dispatch. When a policy pipeline is
        # configured, every ToolCall is signed with this key so the pipeline's
        # IdentityLayer can authenticate it — an unsigned call is denied
        # fail-closed. ``agent_did`` defaults from it when an identity is given.
        self._identity = identity
        self._agent_did = identity.did if identity is not None else agent_did
        self._tier = tier
        self._policy_version = policy_version
        # SPEC-038 — trusted provider label (config-sourced, never response.model)
        # for ProviderUsage attribution, and per-tool resource classifications
        # for the no-read-up ClassificationLayer. ``classification_strict`` fails
        # closed on unknown labels at federal.
        self._provider_label = provider_label
        self._resource_classifications = resource_classifications or {}
        self._classification_strict = classification_strict
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
                # Thread the live RunState (arcrun budget accounting) into the
                # wrapped executor so it can build ProviderUsage for the
                # ProviderLayer (SPEC-038 REQ-004). arcrun exposes state;
                # arcagent bridges; arctrust decides.
                raw_result = await _w(args, parent_state=ctx.parent_state)
                return str(raw_result)

            result.append(
                ArcRunTool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    execute=arcrun_execute,
                    timeout_seconds=None,
                    signals_completion=tool.signals_completion,
                    # SPEC-043 REQ-034 — carry the deployment's classification onto
                    # the arcrun tool so parallel_dispatch's BatchClassifier can
                    # decide concurrency; unclassified stays state_modifying (the
                    # arcrun default), i.e. sequential (fail-closed).
                    classification=tool.classification,
                )
            )
        return result

    @staticmethod
    def _record_admission(
        ledger: SessionCapabilityLedger | None,
        session_id: str,
        tool_legs: frozenset[str],
        clearance_ctx: Any,
    ) -> None:
        """Record an admitted call's legs + max-read class under the lock (REQ-032).

        The atomic read-modify-write half of the admission critical section: on
        ALLOW (or a granted one-shot) the tool's trifecta legs join the session
        union so the NEXT call sees them, and the session's max-read
        classification is raised for the no-exfil egress gate (SPEC-038 F2).
        """
        if ledger is None:
            return
        if tool_legs:
            ledger.record(session_id, tool_legs)
        if clearance_ctx is not None:
            ledger.record_read(session_id, clearance_ctx.resource_classification)

    async def _resolve_forbidden_composition(
        self,
        call: ToolCall,
        ctx_pol: PolicyContext,
        decision: Any,
        human_gate: HumanGate | None,
        tool_legs: frozenset[str],
        accumulated: frozenset[str],
    ) -> ToolCall:
        """Pause a trifecta-completing deny for human approval, or fail closed.

        SPEC-035 REQ-014/015. On any non-composition deny, or when no gate is
        wired, the deny stands. On a forbidden-composition deny the gate is
        asked for a one-shot, operator-signed approval; a granted token is
        re-evaluated (arctrust honors it exactly once) and lets the single call
        through. Denial/timeout → deny (fail closed).
        """
        if decision.rule_id != "global.forbidden_composition" or human_gate is None:
            raise PolicyDenied(decision)
        union = frozenset(tool_legs) | frozenset(accumulated)
        approval = await human_gate.request(call, legs=union)
        if approval is None:
            raise PolicyDenied(decision)
        approved_call = call.model_copy(update={"approval": approval})
        pipeline = self._policy_pipeline
        if pipeline is None:  # unreachable in practice — fail closed defensively
            raise PolicyDenied(decision)
        decision2 = await pipeline.evaluate(approved_call, ctx_pol)
        if decision2.is_deny():
            raise PolicyDenied(decision2)
        return approved_call

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
        identity = self._identity
        agent_did = self._agent_did
        tier = self._tier
        policy_version = self._policy_version
        ledger = self._capability_ledger
        human_gate = self._human_gate
        provider_label = self._provider_label
        resource_label = self._resource_classifications.get(tool.name)
        classification_strict = self._classification_strict
        # Session-scoped trifecta legs this tool contributes (deployment map).
        tool_legs = legs_for_tags(tool.capability_tags)

        async def wrapped_execute(
            args: dict[str, Any] | None = None,
            *,
            parent_state: Any = None,
            **kwargs: Any,
        ) -> Any:
            if args is None:
                args = kwargs

            # 0. Validate arguments against schema
            if tool.input_schema:
                _validate_tool_args(tool.name, args, tool.input_schema)

            # 0.5 ASI-03 / LLM-01 transport defence — strip UNDECLARED LLM-supplied
            # identity fields from memory-tool arguments before they reach the
            # policy pipeline or execute(). Runs regardless of whether a policy
            # pipeline is configured. Identity fields the tool legitimately
            # declares (e.g. ``user_profile_read(user_did=...)``) are preserved;
            # only injected, undeclared identity args are dropped. ``caller_did``
            # is forwarded only to tools whose schema declares it.
            if _is_memory_tool(tool.name):
                declared = frozenset(tool.input_schema.get("properties", {}))
                args = _bind_caller_did(
                    tool.name, args, agent_did, declared=declared, telemetry=telemetry
                )
                if "caller_did" not in declared:
                    args.pop("caller_did", None)

            # 1. Policy pipeline — the single, authoritative deny path.
            # No sudo mode, no bypass flag. Exceptions in layers are
            # caught by the pipeline and returned as DENY (fail-closed).
            if pipeline is not None:
                session_id = current_session_id()
                # SPEC-038 REQ-004/010 — bridge the live arcrun usage onto the
                # ProviderLayer seam with a TRUSTED config-sourced provider label
                # (never response.model). Lights up SPEC-034's inert layer.
                provider_usage = build_provider_usage(parent_state, provider_label)
                # SPEC-038 REQ-023 — no-read-up labels for the ClassificationLayer:
                # caller clearance from identity, resource classification from the
                # per-tool config label. None when either is absent (layer no-ops).
                clearance_ctx = build_clearance_context(
                    identity, resource_label, classification_strict
                )
                call = ToolCall(
                    tool_name=tool.name,
                    arguments=args,
                    agent_did=agent_did,
                    session_id=session_id,
                    classification=resource_label or "unclassified",
                    capability_tags=tool_legs,
                )
                # Sign the call so the pipeline's IdentityLayer can authenticate
                # it (proves this dispatch came from the key-holding agent, not
                # an injected call). No identity → call stays unsigned → denied.
                if identity is not None:
                    call = sign_call(call, identity)
                # SPEC-043 REQ-032 — snapshot→evaluate→record is atomic per
                # session under the admission lock: concurrent dispatch cannot
                # interleave the TOCTOU window, so two calls whose union completes
                # a forbidden composition are evaluated in sequence (the second
                # sees the union → GlobalLayer denies). The lock covers ONLY the
                # O(1) decision; tool.execute and the human-approval await below
                # run outside it (no over-locking, no human timeout under lock).
                lock = (
                    ledger.admission_lock(session_id)
                    if ledger is not None
                    else nullcontext()
                )
                async with lock:
                    accumulated = (
                        ledger.snapshot(session_id) if ledger is not None else frozenset()
                    )
                    # session_capabilities carries the accumulated trifecta legs
                    # so GlobalLayer sees the cross-call union (SPEC-035 REQ-012).
                    ctx_pol = PolicyContext(
                        tier=tier,
                        policy_version=policy_version,
                        bundle_age_seconds=0.0,
                        session_capabilities=accumulated,
                        provider_usage=provider_usage,
                        clearance=clearance_ctx,
                    )
                    decision = await pipeline.evaluate(call, ctx_pol)
                    denied = decision.is_deny()
                    if not denied:
                        self._record_admission(ledger, session_id, tool_legs, clearance_ctx)
                if denied:
                    # Human approval awaits OUTSIDE the lock (REQ-032): a granted
                    # one-shot re-evaluates and, on ALLOW, records the legs.
                    call = await self._resolve_forbidden_composition(
                        call, ctx_pol, decision, human_gate, tool_legs, accumulated
                    )
                    async with lock:
                        self._record_admission(ledger, session_id, tool_legs, clearance_ctx)

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

            # 3. Execute with timeout and telemetry span
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

            # 4. Post-tool event
            await bus.emit(
                "agent:post_tool",
                {"tool": tool.name, "result": result, "duration": elapsed},
            )

            # 5. Audit — actor_did and tier are mandatory for every tool
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

            return result

        return wrapped_execute

    async def shutdown(self) -> None:
        """Clean up all tool connections."""
        self._tools.clear()
        self._prompt_cache = None
        _logger.info("Tool registry shut down")
