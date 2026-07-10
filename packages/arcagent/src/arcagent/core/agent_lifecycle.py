"""Agent capability subsystem setup.

Sibling of ``arcagent.core.agent``. Owns the SPEC-021 capability
subsystem wiring that runs during ``ArcAgent.startup()``: capability
registry construction, builtin runtime configuration, per-module
runtime configuration via signature dispatch, scan-root assembly,
and the bridges that route discovered tools and hooks back into
the existing ToolRegistry and ModuleBus.

Functions take an ``agent`` parameter (the ArcAgent instance). They
read and write its private attributes — coupling acceptable here
because lifecycle wiring is intrinsically tied to the agent's
component graph and these helpers exist solely to keep the
orchestrator file slim.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.module_bus import EventContext
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._egress_build import build_egress_proxy

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

_logger = logging.getLogger("arcagent.agent_lifecycle")

# SPEC-053/037 — modules that construct an operator-signed WORM audit sink and so
# receive the config-RESOLVED operator Signer (same custody + algorithm as the
# policy chain). No other module is handed it, and none is handed the raw seed.
_WORM_SINK_MODULE_NAMES = frozenset({"skills", "messaging", "planning"})

# Per D-346/D-347 — short skill-usage instruction injected at priority 91.
_SKILL_USAGE_INSTRUCTION = (
    "## Skills\n"
    "When the manifest above lists a relevant skill, read its SKILL.md "
    "for step-by-step guidance before invoking the related tools."
)


async def setup_capabilities(agent: ArcAgent, workspace: Path) -> None:
    """Wire the SPEC-021 capability subsystem.

    Builds the :class:`CapabilityRegistry`, configures per-module
    runtimes, scans builtin + enabled-module roots through the
    :class:`CapabilityLoader`, bridges discovered tools and hooks
    into the existing :class:`ToolRegistry` and :class:`ModuleBus`,
    and starts ``@capability`` class lifecycles.
    """
    bus = agent._bus
    tool_registry = agent._tool_registry
    telemetry = agent._telemetry
    identity = agent._identity
    if bus is None or tool_registry is None or telemetry is None or identity is None:
        msg = "Capability subsystem requires bus, tool_registry, telemetry, and identity"
        raise RuntimeError(msg)

    agent._capability_registry = CapabilityRegistry(
        bus=bus,
        audit_sink=None,
        agent_did=identity.did,
        tier=agent._config.security.tier,
    )

    # Configure builtin runtime — workspace + allowed_paths visible
    # to read/write/edit/bash; loader reference patched in below.
    from arcagent.builtins.capabilities import _runtime as builtin_runtime
    from arcagent.tools._validation import resolve_protected_paths

    allowed_paths = [Path(p).resolve() for p in agent._config.tools.policy.allowed_paths] or None
    # SPEC-035 REQ-002 — resolve the goal-lock set once; immutable for the session.
    protected_paths = resolve_protected_paths(
        workspace, list(agent._config.tools.policy.protected_paths)
    )
    protected_audit = telemetry.audit_event if telemetry is not None else None
    builtin_runtime.configure(
        workspace=workspace,
        allowed_paths=allowed_paths,
        loader=None,
        vault_resolver=agent._vault_resolver,
        protected_paths=protected_paths,
        audit_sink=protected_audit,
        tier=agent._config.security.tier,
    )

    # Build the single per-agent egress proxy up front so module runtimes (e.g.
    # telegram) receive it and route their outbound comms through it (REQ-031).
    egress_proxy = build_egress_proxy(
        config=agent._config, ledger=agent._capability_ledger, telemetry=telemetry
    )

    # Configure each enabled module's runtime via signature dispatch.
    configure_module_runtimes(agent, workspace, egress_proxy=egress_proxy)

    # Scan roots per SPEC-021 R-001 precedence:
    # 1. builtins + builtin skills (always)
    # 2. ~/.arc/capabilities/             — global, opt-in by user
    # 3. <agent_root>/capabilities/       — per-agent
    # 4. <workspace>/capabilities/       — agent-authored
    # Plus enabled modules with capabilities.py.
    import arcagent.builtins.capabilities as builtins_pkg

    builtins_root = Path(builtins_pkg.__file__).parent
    scan_roots: list[tuple[str, Path]] = [
        ("builtins", builtins_root),
        ("builtins-skills", builtins_root / "skills"),
    ]

    # Each agent-writable root contributes both ``<name>`` (tools live directly
    # under it) and ``<name>-skills`` (its ``skills/`` subdir, where create_skill
    # writes), mirroring the builtins / builtins-skills pair above. Shared with
    # the arcui inventory seam so a UI read and a real load scan the same roots.
    from arcagent.capabilities.inventory import append_capability_scan_roots

    append_capability_scan_roots(scan_roots, "global", Path("~/.arc/capabilities").expanduser())
    agent_root = agent._config_path.parent.resolve()
    append_capability_scan_roots(scan_roots, "agent", agent_root / "capabilities")
    append_capability_scan_roots(scan_roots, "workspace", workspace / "capabilities")

    modules_dir = Path(__file__).parent.parent / "modules"
    for mod_name, mod_entry in agent._config.modules.items():
        if not mod_entry.enabled:
            continue
        mod_dir = modules_dir / mod_name
        if (mod_dir / "capabilities.py").is_file():
            scan_roots.append((f"module:{mod_name}", mod_dir))

    # SPEC-033 Sign gate: re-verify signatures at load and adjudicate via TOFU.
    # Signature is the floor above personal; personal may relax (auto_run). The
    # posture (tier -> require_signature, import policy, pinned key) is resolved
    # by the SAME helper the arcui capability inventory uses, so a UI read and a
    # real load agree on every verdict (single source of truth).
    from arcagent.capabilities.inventory import resolve_trust_posture

    trusted_pubkey = agent._identity.public_key if agent._identity is not None else None
    posture = resolve_trust_posture(
        agent._config.security,
        agent._config.capabilities,
        trusted_public_key=trusted_pubkey,
    )
    agent._capability_loader = CapabilityLoader(
        scan_roots=scan_roots,
        registry=agent._capability_registry,
        bus=bus,
        allow_all_imports=posture.allow_all_imports,
        allowed_imports=posture.allowed_imports,
        tofu=posture.tofu,
        require_signature=posture.require_signature,
        trusted_public_key=posture.trusted_public_key,
    )
    builtin_runtime.configure(
        workspace=workspace,
        allowed_paths=allowed_paths,
        loader=agent._capability_loader,
        vault_resolver=agent._vault_resolver,
        identity=agent._identity,
        protected_paths=protected_paths,
        audit_sink=protected_audit,
        egress_proxy=egress_proxy,
        tier=agent._config.security.tier,
    )
    # Task 27 follow-up (hotfix) — this is the FINAL builtin_runtime.configure()
    # call, so its snapshot is the one every turn must rebind.
    agent._runtime_bindings.append((builtin_runtime.bind, builtin_runtime.snapshot()))

    diff = await agent._capability_loader.scan_and_register()
    if diff.errors:
        for path, detail in diff.errors:
            _logger.warning("Capability load error %s: %s", path, detail)
    _logger.info("Capability scan: %s", diff.render())

    await bridge_capability_tools_to_registry(agent)
    await bridge_capability_hooks_to_bus(agent)
    setup_capability_prompt_injection(agent)
    await agent._capability_loader.start_lifecycles()


def configure_module_runtimes(
    agent: ArcAgent, workspace: Path, *, egress_proxy: Any = None
) -> None:
    """Call ``_runtime.configure(...)`` on every enabled module.

    Each module's configure() declares the kwargs it needs; we
    introspect the signature and pass only matching values.
    Modules without a ``_runtime`` submodule are silently ignored
    — they may legitimately have no shared state.
    """
    identity = agent._identity
    telemetry = agent._telemetry
    agent_name = agent._config.agent.name
    team_root = agent._config.team.root
    llm_config = agent._config.llm
    eval_config = agent._config.eval

    for mod_name, mod_entry in agent._config.modules.items():
        if not mod_entry.enabled:
            continue
        try:
            runtime_mod = importlib.import_module(f"arcagent.modules.{mod_name}._runtime")
        except ImportError:
            continue
        configure_fn = getattr(runtime_mod, "configure", None)
        if configure_fn is None:
            continue

        available: dict[str, Any] = {
            "config": mod_entry.config,
            "eval_config": eval_config,
            "telemetry": telemetry,
            "workspace": workspace,
            "llm_config": llm_config,
            "agent_name": agent_name,
            "team_root": team_root,
            "bus": agent._bus,
            "agent_did": identity.did if identity else "",
            "identity": identity,
            "egress_proxy": egress_proxy,
            "human_gate": agent._human_gate,
        }
        # SPEC-053/037 — the operator authority is NOT broadcast to every module.
        # Only modules that actually build a WORM audit sink receive the resolved
        # operator Signer, shrinking the in-process attack surface: a compromised
        # generic module cannot harvest signing authority by declaring the
        # parameter. Under vault_transit this Signer holds NO seed — it signs by
        # reference (SPEC-037 F1), so no module ever dereferences the operator seed.
        if mod_name in _WORM_SINK_MODULE_NAMES:
            available["operator_signer"] = agent._operator_signer
        sig = inspect.signature(configure_fn)
        kwargs = {name: value for name, value in available.items() if name in sig.parameters}
        try:
            configure_fn(**kwargs)
        except Exception:  # reason: fail-open — log + continue
            _logger.exception("Module %s _runtime.configure failed", mod_name)
            continue

        # Task 27 follow-up (hotfix) — record the built state so every turn
        # can rebind it in whatever asyncio.Task actually dispatches it.
        bind_fn = getattr(runtime_mod, "bind", None)
        state_fn = getattr(runtime_mod, "state", None)
        if bind_fn is not None and state_fn is not None:
            agent._runtime_bindings.append((bind_fn, state_fn()))


def activate_runtime_bindings(agent: ArcAgent) -> None:
    """Rebind every built runtime state into the CURRENT asyncio task.

    ``ContextVar`` values set during ``startup()`` are visible only to that
    task and its descendants — never to a sibling task created later (task
    27 follow-up hotfix). ``SessionRouter.handle()`` spawns exactly such a
    sibling per turn, so this must run at the top of every turn-dispatch
    entry point before any builtin or module tool reads its runtime state.
    Cheap and idempotent — each ``bind`` call is a single ``ContextVar.set``.
    """
    for bind_fn, built_state in agent._runtime_bindings:
        bind_fn(built_state)


async def bridge_capability_tools_to_registry(agent: ArcAgent) -> None:
    """Register every CapabilityRegistry tool into ToolRegistry.

    ToolRegistry owns the security wrapping (policy pipeline,
    audit, pre/post bus events, telemetry span). Capability tools
    flow through the same wrapper so behavior is identical.
    """
    registry = agent._capability_registry
    tool_registry = agent._tool_registry
    if registry is None or tool_registry is None:
        return
    async with registry._lock.reader:
        entries = list(registry._tools.values())
    for entry in entries:
        if entry.meta.name in agent._capability_tool_names:
            continue
        registered = RegisteredTool(
            name=entry.meta.name,
            description=entry.meta.description,
            input_schema=entry.meta.input_schema,
            transport=ToolTransport.NATIVE,
            execute=entry.execute,
            source=str(entry.source_path),
            classification=entry.meta.classification,
            capability_tags=list(entry.meta.capability_tags),
            when_to_use=entry.meta.when_to_use,
            signals_completion=entry.meta.signals_completion,
        )
        tool_registry.register(registered)
        agent._capability_tool_names.add(entry.meta.name)


async def bridge_capability_hooks_to_bus(agent: ArcAgent) -> None:
    """Subscribe each registered hook to the module bus.

    Idempotent: tracks already-bridged (event, name) pairs so
    reload doesn't double-subscribe.
    """
    registry = agent._capability_registry
    bus = agent._bus
    if registry is None or bus is None:
        return
    async with registry._lock.reader:
        hook_lists = {evt: list(hooks) for evt, hooks in registry._hooks.items()}
    for event, hooks in hook_lists.items():
        for hook in hooks:
            module_name = f"capability:{hook.meta.name}"
            if bus.handler_count_by_module(event, module_name) > 0:
                continue
            bus.subscribe(
                event=event,
                handler=hook.handler,
                priority=hook.meta.priority,
                module_name=module_name,
            )


def setup_capability_prompt_injection(agent: ArcAgent) -> None:
    """Subscribe to agent:assemble_prompt to inject the capability manifest.

    Single subscriber at priority 85 calls
    :meth:`CapabilityRegistry.format_for_prompt` for the unified
    XML manifest (tools + skills). A second subscriber at priority
    91 injects the per-skill usage instruction.
    """
    bus = agent._bus
    registry = agent._capability_registry
    telemetry = agent._telemetry
    if bus is None or registry is None:
        return

    async def _inject_capabilities(ctx: EventContext) -> None:
        sections = ctx.data.get("sections")
        if not isinstance(sections, dict):
            return
        prompt_text = await registry.format_for_prompt()
        if prompt_text:
            sections["capabilities"] = prompt_text
            if telemetry is not None:
                telemetry.audit_event(
                    "prompt.capabilities_manifest_rebuilt",
                    {
                        "tool_count": len(registry._tools),
                        "skill_count": len(registry._skills),
                    },
                )

    async def _inject_skill_usage(ctx: EventContext) -> None:
        sections = ctx.data.get("sections")
        if not isinstance(sections, dict) or not registry._skills:
            return
        sections["skill_usage"] = _SKILL_USAGE_INSTRUCTION

    bus.subscribe(
        event="agent:assemble_prompt",
        handler=_inject_capabilities,
        priority=85,
        module_name="capability_registry",
    )
    bus.subscribe(
        event="agent:assemble_prompt",
        handler=_inject_skill_usage,
        priority=91,
        module_name="capability_registry.skills",
    )
