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

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

_logger = logging.getLogger("arcagent.agent_lifecycle")

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

    allowed_paths = [Path(p).resolve() for p in agent._config.tools.policy.allowed_paths] or None
    builtin_runtime.configure(
        workspace=workspace,
        allowed_paths=allowed_paths,
        loader=None,
        vault_resolver=agent._vault_resolver,
    )

    # Configure each enabled module's runtime via signature dispatch.
    configure_module_runtimes(agent, workspace)

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

    global_root = Path("~/.arc/capabilities").expanduser()
    if global_root.is_dir():
        scan_roots.append(("global", global_root))

    agent_root = agent._config_path.parent.resolve()
    agent_caps = agent_root / "capabilities"
    if agent_caps.is_dir():
        scan_roots.append(("agent", agent_caps))

    workspace_caps = workspace / "capabilities"
    if workspace_caps.is_dir():
        scan_roots.append(("workspace", workspace_caps))

    modules_dir = Path(__file__).parent.parent / "modules"
    for mod_name, mod_entry in agent._config.modules.items():
        if not mod_entry.enabled:
            continue
        mod_dir = modules_dir / mod_name
        if (mod_dir / "capabilities.py").is_file():
            scan_roots.append((f"module:{mod_name}", mod_dir))

    # Tier-resolved import policy for untrusted workspace-authored tools:
    # personal allows all; federal honors only the explicit allowlist;
    # enterprise honors allow_all or the allowlist. The AST gate still blocks
    # the path entirely; this only relaxes which module imports are permitted.
    from arcagent.tools._dynamic_loader import resolve_workspace_import_policy

    caps_cfg = agent._config.capabilities
    allow_all_imports, allowed_imports = resolve_workspace_import_policy(
        agent._config.security.tier,
        allow_all_imports=caps_cfg.allow_all_imports,
        allow_imports=caps_cfg.allow_imports,
    )
    agent._capability_loader = CapabilityLoader(
        scan_roots=scan_roots,
        registry=agent._capability_registry,
        bus=bus,
        allow_all_imports=allow_all_imports,
        allowed_imports=allowed_imports,
    )
    builtin_runtime.configure(
        workspace=workspace,
        allowed_paths=allowed_paths,
        loader=agent._capability_loader,
        vault_resolver=agent._vault_resolver,
    )

    diff = await agent._capability_loader.scan_and_register()
    if diff.errors:
        for path, detail in diff.errors:
            _logger.warning("Capability load error %s: %s", path, detail)
    _logger.info("Capability scan: %s", diff.render())

    await bridge_capability_tools_to_registry(agent)
    await bridge_capability_hooks_to_bus(agent)
    setup_capability_prompt_injection(agent)
    await agent._capability_loader.start_lifecycles()


def configure_module_runtimes(agent: ArcAgent, workspace: Path) -> None:
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
        }
        sig = inspect.signature(configure_fn)
        kwargs = {name: value for name, value in available.items() if name in sig.parameters}
        try:
            configure_fn(**kwargs)
        except Exception:  # reason: fail-open — log + continue
            _logger.exception("Module %s _runtime.configure failed", mod_name)


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
