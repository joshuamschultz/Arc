"""Agent capability subsystem setup.

Sibling of ``arcagent.core.agent``. Owns the SPEC-021 capability
subsystem wiring that runs during ``ArcAgent.startup()``: capability
registry construction, builtin runtime configuration, per-module
runtime configuration via explicit typed contracts, scan-root assembly,
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
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from arcprompt import load_stock

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.config import ModuleEntry
from arcagent.core.module_bus import EventContext
from arcagent.core.module_discovery import active_modules, module_root, module_statuses
from arcagent.core.runtime_dependencies import (
    RuntimeBindable,
    RuntimeBinding,
    RuntimeDependencies,
    RuntimeModule,
    RuntimeTeardownable,
)
from arcagent.core.telemetry import TelemetryAuditSink
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._egress_build import build_egress_proxy
from arcagent.utils.source_module import exec_source_module

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent

_logger = logging.getLogger("arcagent.agent_lifecycle")


def _resolve_working_dir(
    config: Any, workspace: Path, allowed_paths: list[Path] | None
) -> Path | None:
    """The launch dir the builtin file/exec tools operate in, or None (→ workspace).

    Opt-in via ``[tools].operate_in_launch_dir`` and supplied by the launcher via
    ``ARC_WORKING_DIR`` (arctui sets it to the trusted project cwd). Honored ONLY when
    that dir is already inside ``workspace + allowed_paths`` — the trust prompt is what
    puts it there — so this can never let a tool reach a path the sandbox would otherwise
    deny. Only bash + the file tools use it; agent state (memory/sessions/identity)
    persists directly to the workspace and is unaffected.
    """
    if not getattr(config.tools, "operate_in_launch_dir", False):
        return None
    launch = os.environ.get("ARC_WORKING_DIR")
    if not launch:
        return None
    launch_path = Path(launch).expanduser().resolve()
    roots = [workspace, *(allowed_paths or [])]
    if any(launch_path == r or r in launch_path.parents for r in roots):
        return launch_path
    _logger.warning(
        "ARC_WORKING_DIR %s is not within workspace/allowed_paths; ignoring (sandbox floor)",
        launch_path,
    )
    return None


def load_module_runtime(name: str) -> RuntimeModule:
    """Load ``<module_root>/<name>/_runtime.py`` from the filesystem.

    ONE loader for every module regardless of origin: a module materialized
    from a signed bundle and a module that happens to still ship in the wheel
    are loaded the same way, so there is no second, weaker path to audit.
    Nothing is added to ``sys.path`` — the folder is addressed directly.

    The result is registered under its canonical dotted name because a module's
    own ``capabilities.py`` reaches its state through
    ``from arcagent.modules.<name> import _runtime``. A second, path-loaded copy
    would own its own ``ContextVar`` objects, and every tool in that module
    would then read state :func:`configure` never touched. Registering also
    makes the load happen once per process — the semantics ``import_module``
    gave — so two agents in one process share one runtime object, as the
    ContextVar isolation already assumes.

    Execution goes through :func:`arcagent.utils.source_module.exec_source_module`,
    which documents why ``compile()`` + ``exec`` replaces ``exec_module`` here.
    """
    dotted = f"arcagent.modules.{name}._runtime"
    cached = sys.modules.get(dotted)
    if cached is not None:
        return cast(RuntimeModule, cached)

    path = module_root() / name / "_runtime.py"
    module = exec_source_module(path, dotted)
    # Bind the submodule onto its package exactly as the import machinery does.
    # The bare ``sys.modules`` entry alone satisfies ``from <pkg> import _runtime``
    # — that form falls back to ``sys.modules`` — but NOT attribute traversal,
    # which is how a ``<pkg>._runtime.<attr>`` reference and pytest's string-form
    # ``monkeypatch.setattr`` resolve. Without this the two disagree about which
    # object is the runtime, which is the whole failure mode this function exists
    # to prevent.
    package = importlib.import_module(f"arcagent.modules.{name}")
    # ignore reason: this assignment is what CREATES the attribute — nothing declares it.
    package._runtime = module  # type: ignore[attr-defined]
    return cast(RuntimeModule, module)


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
    agent._runtime_bindings.clear()
    audit_sink = TelemetryAuditSink(telemetry)

    agent._capability_registry = CapabilityRegistry(
        bus=bus,
        audit_sink=audit_sink,
        agent_did=identity.did,
        tier=agent._config.security.tier,
        task_supervisor=agent._background_tasks,
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
    # Coding agents (opt-in) operate their file/exec tools in the trusted launch dir;
    # everyone else stays workspace-rooted. Agent state still persists to the workspace.
    working_dir = _resolve_working_dir(agent._config, workspace, allowed_paths)
    builtin_runtime.configure(
        workspace=workspace,
        allowed_paths=allowed_paths,
        working_dir=working_dir,
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
    # 2. ${ARC_CONFIG_DIR:-~/.arc}/capabilities/ — global, opt-in by user
    # 3. <agent_root>/capabilities/              — per-agent
    # 4. <workspace>/capabilities/               — agent-authored
    # Plus each enabled module's per-agent capability copy, under root 3.
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
    from arcagent.capabilities.inventory import (
        append_capability_scan_roots,
        append_module_scan_roots,
        global_capabilities_root,
    )

    append_capability_scan_roots(scan_roots, "global", global_capabilities_root())
    agent_root = agent._config_path.parent.resolve()
    append_capability_scan_roots(scan_roots, "agent", agent_root / "capabilities")
    append_capability_scan_roots(scan_roots, "workspace", workspace / "capabilities")

    # A module's RUNTIME stays once at the deployment root (configured above);
    # its CAPABILITY SURFACE is the per-agent copy `arc module install` writes to
    # `<agent_root>/capabilities/modules/<name>/`, and that copy is what loads
    # (REQ-337). One load path, not two: a skill or tool that drifts for this
    # agent must not change what every other agent on the box is told to do.
    #
    # `module:*` is its own trust class (RootTrust.VERIFIED): a valid signature
    # is mandatory at EVERY tier, because the copy sits in a directory the agent
    # can write. It is NOT isolated — the AST import allowlist and ArcRun
    # isolation exist to contain code the model wrote, and applying them to
    # module code made 17 of 18 modules register zero tools (`@hook` /
    # `@background_task` / `@capability` never register and the stdlib a module
    # legitimately imports is blocked).
    append_module_scan_roots(scan_roots, agent_root, active_modules(agent._config))

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
        audit_sink=audit_sink,
        import_policy=posture.import_policy,
        tofu=posture.tofu,
        require_signature=posture.require_signature,
        trusted_public_keys=posture.trusted_public_keys,
        isolation_tier=agent._config.security.tier,
    )
    builtin_runtime.configure(
        workspace=workspace,
        allowed_paths=allowed_paths,
        working_dir=working_dir,
        loader=agent._capability_loader,
        vault_resolver=agent._vault_resolver,
        identity=agent._identity,
        protected_paths=protected_paths,
        audit_sink=protected_audit,
        egress_proxy=egress_proxy,
        tier=agent._config.security.tier,
        import_policy=posture.import_policy,
    )
    # Task 27 follow-up (hotfix) — this is the FINAL builtin_runtime.configure()
    # call, so its snapshot is the one every turn must rebind.
    agent._runtime_bindings.append(
        RuntimeBinding("builtins", builtin_runtime.bind, builtin_runtime.snapshot())
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

    # Build the overlay-aware prompt resolver once (COMP-006), pinned to the
    # operator key that arcui signs overlays with. Overlay root is the agent
    # config root's context/ dir — outside the workspace tool sandbox (COMP-007).
    from arcagent.core.prompt_context import build_prompt_resolver

    agent._prompt_resolver = build_prompt_resolver(
        agent._config_path, str(agent._config.security.tier)
    )


def configure_module_runtimes(
    agent: ArcAgent, workspace: Path, *, egress_proxy: Any = None
) -> None:
    """Configure enabled modules through explicit, typed dependency contracts."""
    identity = agent._identity
    telemetry = agent._telemetry
    tool_registry = agent._tool_registry
    agent_name = agent._config.agent.name
    team_root = agent._config.team.root
    llm_config = agent._config.llm
    eval_config = agent._config.eval

    dependencies = RuntimeDependencies(
        workspace=workspace,
        config_path=agent._config_path,
        eval_config=eval_config,
        llm_config=llm_config,
        telemetry=telemetry,
        bus=agent._bus,
        tool_registry=tool_registry,
        agent_name=agent_name,
        agent_did=identity.did if identity is not None else "",
        team_root=team_root,
        tier=str(agent._config.security.tier),
        identity=identity,
        operator_signer=agent._operator_signer,
        policy_pipeline=agent._policy_pipeline,
        egress_proxy=egress_proxy,
        human_gate=agent._human_gate,
        agent_run_fn=agent.run_collected,
        arcstore_opener=agent._arcstore_opener,
        source_sync_store_opener=agent._make_source_sync_store_opener(),
    )
    # Kept so a module enabled later in the session is configured from the same
    # menu as one enabled at startup (set_module_enabled).
    agent._runtime_deps = dependencies

    _warn_config_without_folder(agent)

    for mod_name in active_modules(agent._config):
        mod_entry = agent._config.modules[mod_name]
        try:
            runtime_mod = load_module_runtime(mod_name)
            # The module's ``configure`` signature is its whole dependency
            # contract — core names no module and holds no per-module registry.
            kwargs = dependencies.select_for(runtime_mod.configure, mod_entry.config)
            runtime_mod.configure(**kwargs)
        except Exception as exc:
            # Every enabled module is required. Half-configuring the agent and
            # continuing hides the failure until some later tool silently
            # no-ops, so abort with the cause chained (undiagnosable otherwise).
            raise RuntimeError(f"Required module {mod_name!r} configuration failed") from exc

        # Task 27 follow-up (hotfix) — record the built state so every turn
        # can rebind it in whatever asyncio.Task actually dispatches it.
        if isinstance(runtime_mod, RuntimeBindable):
            agent._runtime_bindings.append(
                RuntimeBinding(mod_name, runtime_mod.bind, runtime_mod.state())
            )


async def set_module_enabled(agent: ArcAgent, name: str, *, enabled: bool) -> str:
    """Enable or disable one module at runtime — bind or unbind all its effects.

    A module contributes tools, hooks, background tasks, and per-agent state. The
    capability half (tools/hooks/tasks) rides the transactional reload: this adds
    or drops the module's ``module:<name>`` scan root and lets the reload
    (de)register from it. This function owns only the runtime half — configure on
    enable, ``teardown`` on disable — plus the task-local binding. No restart.
    """
    loader = agent._capability_loader
    deps = agent._runtime_deps
    if loader is None or deps is None:
        raise RuntimeError("Module hot-swap requires a started agent")

    if enabled == (name in active_modules(agent._config)):
        return f"module {name!r} already {'enabled' if enabled else 'disabled'}"

    entry = agent._config.modules.get(name) or ModuleEntry()
    if enabled:
        agent._config.modules[name] = entry.model_copy(update={"enabled": True})
        runtime_mod = load_module_runtime(name)
        try:
            runtime_mod.configure(**deps.select_for(runtime_mod.configure, entry.config))
        except Exception as exc:
            raise RuntimeError(f"Module {name!r} configuration failed") from exc
        if isinstance(runtime_mod, RuntimeBindable):
            agent._runtime_bindings.append(
                RuntimeBinding(name, runtime_mod.bind, runtime_mod.state())
            )
    else:
        runtime_mod = load_module_runtime(name)
        if isinstance(runtime_mod, RuntimeTeardownable):
            await runtime_mod.teardown()
        agent._runtime_bindings[:] = [
            binding for binding in agent._runtime_bindings if binding.module_name != name
        ]
        agent._config.modules[name] = entry.model_copy(update={"enabled": False})

    loader.set_module_roots(agent._config_path.parent.resolve(), active_modules(agent._config))
    return await agent.reload()


def _warn_config_without_folder(agent: ArcAgent) -> None:
    """Warn AND audit any ``[modules.NAME]`` entry naming an absent folder.

    Discovery is folder-driven, so a config entry with no matching module folder
    can never load. Surface it clearly instead of failing silently — a typo'd or
    stale module name is a config error, not a crash.

    A WARNING alone is not enough once modules ship as separately installed
    bundles. The agent still boots and still answers, so the operator sees a
    healthy service while the scheduler never fires and tasks never dispatch —
    the failure is invisible precisely because nothing crashed. An audit event
    puts it on the same durable trail an operator already reviews, so "this
    deployment lost a capability it was configured for" is a queryable fact
    rather than a line someone had to be tailing logs to catch.
    """
    for name, status in module_statuses(agent._config).items():
        entry = agent._config.modules.get(name)
        if entry is not None and entry.enabled and not status.discovered:
            _logger.warning(
                "Config enables module %r but no module folder is present; skipping", name
            )
            if agent._telemetry is not None:
                agent._telemetry.audit_event(
                    "module.enabled_but_absent",
                    {
                        "module": name,
                        "module_root": str(module_root()),
                        "reason": "config enables the module but no module folder is present",
                        "remedy": f"arc module install {name}",
                    },
                )


def activate_runtime_bindings(agent: ArcAgent) -> None:
    """Bind THIS agent's built runtime state into the CURRENT asyncio task.

    ``ContextVar`` values set during ``startup()`` are visible only to that
    task and its descendants — never to a sibling task created later (task
    27 follow-up hotfix). ``SessionRouter.handle()`` spawns exactly such a
    sibling per turn, so this runs at the top of every turn-dispatch entry
    point so each module's ``state()`` resolves to THIS agent's DID.

    For the DID-keyed module runtimes (memory, workpad) each ``bind`` both
    registers the state under its ``agent_did`` and binds that DID as the
    running turn's current agent. Rebinding is a correctness convenience, not
    the sole isolation guard: those modules' ``state()`` fail closed on a
    missing or mismatched DID, so a missed rebind refuses the read rather than
    leaking another agent's state. Cheap and idempotent — each ``bind`` call is
    a dict insert plus a single ``ContextVar.set``.
    """
    for binding in agent._runtime_bindings:
        binding.activate()


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
    replacements: list[RegisteredTool] = []
    for entry in registry.tool_entries():
        registered = RegisteredTool(
            name=entry.meta.name,
            description=entry.meta.description,
            input_schema=entry.meta.input_schema,
            transport=ToolTransport.NATIVE,
            execute=entry.execute,
            source=str(entry.source_path),
            scan_root=entry.scan_root,
            classification=entry.meta.classification,
            capability_tags=list(entry.meta.capability_tags),
            when_to_use=entry.meta.when_to_use,
            signals_completion=entry.meta.signals_completion,
        )
        replacements.append(registered)
    agent._capability_tool_names = tool_registry.replace_owned(
        agent._capability_tool_names, replacements
    )


async def bridge_capability_hooks_to_bus(agent: ArcAgent) -> None:
    """Subscribe each registered hook to the module bus.

    Idempotent: tracks already-bridged (event, name) pairs so
    reload doesn't double-subscribe.
    """
    registry = agent._capability_registry
    bus = agent._bus
    if registry is None or bus is None:
        return
    replacements: list[tuple[str, Callable[[EventContext], Awaitable[None]], int, str]] = []
    for event, hooks in registry.hook_entries().items():
        for hook in hooks:
            module_name = f"capability:{hook.meta.name}"
            replacements.append((event, hook.handler, hook.meta.priority, module_name))
    bus.replace_handlers(module_prefix="capability:", handlers=replacements)


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
                tool_count, skill_count = registry.counts()
                telemetry.audit_event(
                    "prompt.capabilities_manifest_rebuilt",
                    {
                        "tool_count": tool_count,
                        "skill_count": skill_count,
                    },
                )

    async def _inject_skill_usage(ctx: EventContext) -> None:
        sections = ctx.data.get("sections")
        if not isinstance(sections, dict) or not registry.has_skills():
            return
        sections["skill_usage"] = load_stock("arcagent", "skill_usage_instruction")

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
