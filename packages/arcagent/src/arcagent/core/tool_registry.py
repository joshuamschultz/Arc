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
from dataclasses import dataclass, field
from typing import Any, Literal
from xml.sax.saxutils import escape as xml_escape

import arcrun
from arcprompt import load_stock
from arctrust import AgentIdentity

from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.errors import ToolError, ToolVetoedError
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    SessionCapabilityLedger,
    current_session_id,
    legs_for_call,
    legs_for_tags,
)
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tier import Tier
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
from arcagent.tools._egress_policy import EgressVerdict, egress_verdict, is_extension_origin
from arcagent.tools._policy_fill import build_clearance_context, build_provider_usage
from arcagent.tools._transport import (
    _PY_TYPE_MAP,
    RegisteredTool,
    ToolClassification,
    ToolTransport,
    _echo_tool,
    _validate_tool_args,
    native_tool,
)
from arcagent.tools.human_gate import HumanGate, summarize_arguments

_logger = logging.getLogger("arcagent.tool_registry")


__all__ = [
    "_IDENTITY_ARG_NAMES",
    "_MEMORY_TOOL_PREFIXES",
    "_PY_TYPE_MAP",
    "RegisteredTool",
    "ToolClassification",
    "ToolDispatchContext",
    "ToolRegistry",
    "ToolTransport",
    "_bind_caller_did",
    "_echo_tool",
    "_is_memory_tool",
    "_validate_tool_args",
    "native_tool",
]


@dataclass
class ToolDispatchContext:
    """Typed state passed through one tool-dispatch envelope."""

    tool: RegisteredTool
    args: dict[str, Any]
    parent_state: Any = None
    session_id: str = ""
    call_legs: frozenset[str] = field(default_factory=frozenset)
    arg_summary: str = ""
    clearance: Any = None
    call: ToolCall | None = None
    policy_context: PolicyContext | None = None
    decision: Any = None
    accumulated: frozenset[str] = field(default_factory=frozenset)
    admission_lock: Any = None
    result: Any = None
    elapsed: float = 0.0


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
        self._preamble: str = config.preamble or load_stock("arcagent", "tool_manifest_preamble")

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
    def policy(self) -> ToolConfig:
        """The operator's tool policy — allow/deny and per-tool egress permissions.

        Public because the extension load path has to apply the same
        ``egress_allow`` this registry applies, and a second copy read from
        somewhere else is the one that drifts.
        """
        return self._config.policy

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
        """Register a tool, filtered by allow/deny policy and the egress gate.

        Policy semantics:
          - empty allow + empty deny  → all tools register (default)
          - non-empty allow            → only listed tools register
          - non-empty deny             → listed tools are skipped
          - deny takes precedence over allow when both name the same tool

        D-580 adds one more subtraction: a tool that sends data out registers
        only if its tier and origin permit it. This is the single load-path
        chokepoint every origin crosses — builtins, modules, capability roots and
        extension attachments all arrive here — so the gate lives here rather
        than being re-implemented per origin. It can only subtract: deny is
        checked first, so an ``egress_allow`` entry never re-admits a denied tool.

        Denied tools are skipped silently: they do not enter the registry,
        a warning is logged, and a `tool.policy_denied` audit event fires.
        Registration never raises on policy denial — letting an agent start
        cleanly with a least-privilege deny=[`write`,`bash`] config without
        crashing when built-in tools attempt to register.
        """
        egress = self._egress_verdict(tool)
        if self._policy_allows(tool.name) and egress.allowed:
            self._tools[tool.name] = tool
            self._prompt_cache = None  # Invalidate cached catalog
            _logger.info("Registered tool: %s (%s)", tool.name, tool.transport.value)
            return

        policy = self._config.policy
        # DEBUG, not WARNING: deny/allow filtering is intentional config-driven
        # behavior, not an error. The audit event below preserves the trail.
        _logger.debug(
            "policy filter: excluded tool %r from registry (allow=%s, deny=%s, egress=%s)",
            tool.name,
            list(policy.allow),
            list(policy.deny),
            egress.reason or "permitted",
        )
        self._telemetry.audit_event(
            "tool.policy_denied",
            {
                "tool": tool.name,
                "allowlist": list(policy.allow),
                "denylist": list(policy.deny),
                "egress_refusal": egress.message,
            },
        )

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

    def replace_owned(
        self,
        owned_names: set[str],
        replacements: list[RegisteredTool],
    ) -> set[str]:
        """Replace one owner's tools without exposing a partially rebuilt set.

        Registration and policy filtering are synchronous, so the event loop
        cannot interleave a dispatch between removal and completion. If a
        registration unexpectedly raises, the exact previous mapping and
        prompt cache are restored before the exception escapes.
        """
        previous_tools = dict(self._tools)
        previous_cache = self._prompt_cache
        try:
            for name in owned_names:
                self._tools.pop(name, None)
            accepted: set[str] = set()
            for tool in replacements:
                self.register(tool)
                if tool.name in self._tools:
                    accepted.add(tool.name)
            self._prompt_cache = None
            return accepted
        except Exception:
            self._tools = previous_tools
            self._prompt_cache = previous_cache
            raise

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

    def _egress_verdict(self, tool: RegisteredTool) -> EgressVerdict:
        """The D-580 tier + origin egress decision, read from the tool's own signals."""
        return egress_verdict(
            tool_name=tool.name,
            capability_tags=tool.capability_tags,
            tier=Tier(self._tier),
            from_extension=is_extension_origin(source=tool.source, scan_root=tool.scan_root),
            egress_allow=self._config.policy.egress_allow,
        )

    def to_arcrun_tools(self) -> list[arcrun.Tool]:
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
        result: list[arcrun.Tool] = []
        for tool in self._tools.values():
            wrapped = self._create_wrapped_execute(tool)

            async def arcrun_execute(
                args: dict[str, Any],
                ctx: arcrun.ToolContext,
                _w: Any = wrapped,
            ) -> str:
                # Thread the live RunState (arcrun budget accounting) into the
                # wrapped executor so it can build ProviderUsage for the
                # ProviderLayer (SPEC-038 REQ-004). arcrun exposes state;
                # arcagent bridges; arctrust decides.
                raw_result = await _w(args, parent_state=ctx.parent_state)
                return str(raw_result)

            result.append(
                arcrun.Tool(
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
        *,
        tool_name: str = "",
        arg_summary: str = "",
    ) -> None:
        """Record an admitted call's legs + max-read class under the lock (REQ-032).

        The atomic read-modify-write half of the admission critical section: on
        ALLOW (or a granted one-shot) the tool's trifecta legs join the session
        union so the NEXT call sees them, and the session's max-read
        classification is raised for the no-exfil egress gate (SPEC-038 F2).
        ``tool_name``/``arg_summary`` are recorded as leg provenance (SPEC-035
        approval enrichment); the summary is redacted/bounded by the caller so no
        redaction work happens under the admission lock.
        """
        if ledger is None:
            return
        if tool_legs:
            ledger.record(session_id, tool_legs, tool_name=tool_name, arg_summary=arg_summary)
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
        # SPEC-035 approval enrichment — the ledger's provenance explains which
        # PRIOR calls lit the accumulated legs (this blocked call is described by
        # the request's own tool + arguments). Read-only snapshot, JSON-ready.
        ledger = self._capability_ledger
        provenance = (
            [entry.as_dict() for entry in ledger.provenance(call.session_id)]
            if ledger is not None
            else []
        )
        approval = await human_gate.request(call, legs=union, provenance=provenance)
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

    def _normalize_dispatch(self, dispatch: ToolDispatchContext) -> None:
        """Validate arguments and bind trusted caller identity."""
        tool = dispatch.tool
        if tool.input_schema:
            _validate_tool_args(tool.name, dispatch.args, tool.input_schema)
        if not _is_memory_tool(tool.name):
            return
        declared = frozenset(tool.input_schema.get("properties", {}))
        dispatch.args = _bind_caller_did(
            tool.name,
            dispatch.args,
            self._agent_did,
            declared=declared,
            telemetry=self._telemetry,
        )
        if "caller_did" not in declared:
            dispatch.args.pop("caller_did", None)

    async def _authorize_dispatch(self, dispatch: ToolDispatchContext) -> None:
        """Evaluate policy atomically and record immediately admitted calls."""
        pipeline = self._policy_pipeline
        if pipeline is None:
            return
        tool = dispatch.tool
        dispatch.session_id = current_session_id()
        dispatch.call_legs = legs_for_call(tool.name, tool.capability_tags, dispatch.args)
        dispatch.arg_summary = summarize_arguments(dispatch.args) if dispatch.call_legs else ""
        declared_legs = legs_for_tags(tool.capability_tags)
        if EXTERNAL_COMMS in declared_legs and EXTERNAL_COMMS not in dispatch.call_legs:
            self._telemetry.audit_event(
                "policy.owner_channel_exempt",
                {
                    "tool": tool.name,
                    "actor_did": self._agent_did,
                    "tier": self._tier,
                    "reason": "owner-directed egress is a trusted sink",
                    "destination": dispatch.args.get("to"),
                },
            )
        provider_usage = build_provider_usage(dispatch.parent_state, self._provider_label)
        dispatch.clearance = build_clearance_context(
            self._identity,
            self._resource_classifications.get(tool.name),
            self._classification_strict,
        )
        call = ToolCall(
            tool_name=tool.name,
            arguments=dispatch.args,
            agent_did=self._agent_did,
            session_id=dispatch.session_id,
            classification=self._resource_classifications.get(tool.name) or "unclassified",
            capability_tags=dispatch.call_legs,
        )
        dispatch.call = sign_call(call, self._identity) if self._identity is not None else call
        ledger = self._capability_ledger
        dispatch.admission_lock = (
            ledger.admission_lock(dispatch.session_id) if ledger is not None else nullcontext()
        )
        async with dispatch.admission_lock:
            dispatch.accumulated = (
                ledger.snapshot(dispatch.session_id) if ledger is not None else frozenset()
            )
            dispatch.policy_context = PolicyContext(
                tier=self._tier,
                policy_version=self._policy_version,
                bundle_age_seconds=0.0,
                session_capabilities=dispatch.accumulated,
                provider_usage=provider_usage,
                clearance=dispatch.clearance,
            )
            dispatch.decision = await pipeline.evaluate(dispatch.call, dispatch.policy_context)
            if not dispatch.decision.is_deny():
                self._record_admission(
                    ledger,
                    dispatch.session_id,
                    dispatch.call_legs,
                    dispatch.clearance,
                    tool_name=tool.name,
                    arg_summary=dispatch.arg_summary,
                )

    async def _approve_dispatch(self, dispatch: ToolDispatchContext) -> None:
        """Resolve composition approval, then run the final pre-tool veto."""
        if dispatch.decision is not None and dispatch.decision.is_deny():
            if dispatch.call is None or dispatch.policy_context is None:
                raise ToolError(
                    code="TOOL_DISPATCH_STATE_INVALID",
                    message="Policy denied without a complete approval context",
                    details={"tool": dispatch.tool.name},
                )
            dispatch.call = await self._resolve_forbidden_composition(
                dispatch.call,
                dispatch.policy_context,
                dispatch.decision,
                self._human_gate,
                dispatch.call_legs,
                dispatch.accumulated,
            )
            async with dispatch.admission_lock:
                self._record_admission(
                    self._capability_ledger,
                    dispatch.session_id,
                    dispatch.call_legs,
                    dispatch.clearance,
                    tool_name=dispatch.tool.name,
                    arg_summary=dispatch.arg_summary,
                )
        event = await self._bus.emit(
            "agent:pre_tool", {"tool": dispatch.tool.name, "args": dispatch.args}
        )
        if event.is_vetoed:
            raise ToolVetoedError(
                message=f"Tool '{dispatch.tool.name}' vetoed: {event.veto_reason}",
                details={"tool": dispatch.tool.name, "reason": event.veto_reason},
            )

    async def _execute_dispatch(self, dispatch: ToolDispatchContext) -> None:
        """Execute once under the configured timeout and telemetry span."""
        start = time.monotonic()
        try:
            async with self._telemetry.tool_span(dispatch.tool.name, dispatch.args):
                dispatch.result = await asyncio.wait_for(
                    dispatch.tool.execute(**dispatch.args),
                    timeout=dispatch.tool.timeout_seconds,
                )
        except TimeoutError as exc:
            raise ToolError(
                code="TOOL_TIMEOUT",
                message=(
                    f"Tool '{dispatch.tool.name}' timed out after {dispatch.tool.timeout_seconds}s"
                ),
                details={
                    "tool": dispatch.tool.name,
                    "timeout": dispatch.tool.timeout_seconds,
                },
            ) from exc
        dispatch.elapsed = time.monotonic() - start

    async def _record_dispatch(self, dispatch: ToolDispatchContext) -> None:
        """Publish the successful result and write its audit envelope."""
        tool = dispatch.tool
        await self._bus.emit(
            "agent:post_tool",
            {"tool": tool.name, "result": dispatch.result, "duration": dispatch.elapsed},
        )
        if self._agent_did == "did:arc:unknown":
            self._telemetry.audit_event(
                "security.unidentified_tool_call",
                {
                    "tool": tool.name,
                    "actor_did": self._agent_did,
                    "tier": self._tier,
                    "warning": "Tool called without a real agent DID — configure identity.",
                },
            )
        self._telemetry.audit_event(
            "tool.executed",
            {
                "tool": tool.name,
                "transport": tool.transport.value,
                "duration_ms": round(dispatch.elapsed * 1000),
                "actor_did": self._agent_did,
                "tier": self._tier,
            },
        )

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

        async def wrapped_execute(
            args: dict[str, Any] | None = None,
            *,
            parent_state: Any = None,
            **kwargs: Any,
        ) -> Any:
            dispatch = ToolDispatchContext(
                tool=tool,
                args=dict(kwargs if args is None else args),
                parent_state=parent_state,
            )
            self._normalize_dispatch(dispatch)

            await self._authorize_dispatch(dispatch)
            await self._approve_dispatch(dispatch)

            await self._execute_dispatch(dispatch)
            await self._record_dispatch(dispatch)
            return dispatch.result

        return wrapped_execute

    async def shutdown(self) -> None:
        """Clean up all tool connections."""
        self._tools.clear()
        self._prompt_cache = None
        _logger.info("Tool registry shut down")
