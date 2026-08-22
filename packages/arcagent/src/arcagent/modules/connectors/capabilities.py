"""Decorator-form connector module — SPEC-062 COMP-015 (T-901/T-902, T-917).

A single ``@capability`` class :class:`Connectors` owns the module's lifecycle.
``setup()`` attaches every connection this agent has been **granted** and
registers each one's verbs as individually named tools; ``teardown()`` deregisters
exactly the names it registered and nothing else.

**This is where a grant is enforced**, and it is deliberately the only place. The
module asks :meth:`~arcagent.extension.grants.ConnectionRegistry.granted_to` for
the connections naming this agent, and an agent named in none gets an empty
mapping: no bundle is loaded, no credential is read, and no verb reaches the
registry the model reads its catalog from. Deny-by-default is therefore a
property of the question asked rather than of a check that could be forgotten —
there is no code path here that could omit a check and still attach something.

**The order is the security property**, and it is the loader's order rather than
a second one:

* Every bundle goes through :class:`~arcagent.extension.loader.ExtensionLoader`
  first, because that is the one component holding the signature gate and
  building an attachment EXECUTES extension-declared code (a native attachment
  imports the extension's own module).
* Only then are the instance's credentials read out of the secret store and
  handed to the attachment. After verification, so a bundle the gate refuses is
  never given one; and fail-closed, so a connection whose declared credential is
  missing is refused by name instead of serving verbs that answer 401.
* What the upstream then serves is annotated from the **manifest**, never
  trusted from the server: an upstream choosing its own ``capability_tags``
  would be choosing whether the lethal-trifecta gate applies to it, and a served
  verb the manifest never declared is treated as restrictively as
  ``extension/approval.py`` treats one at call time.
* :class:`~arcagent.extension.contract_ledger.ToolContractLedger` re-verifies the
  served contract against what an operator approved, and a suspended tool is
  simply absent from what registers (REQ-291).
* :class:`~arcagent.extension.approval.ApprovalBinding` binds the instance's
  approval mode onto the deployment's human gate before anything can be called.
* :class:`~arcagent.extension.bridge.CapabilityBridge` puts the survivors in the
  agent's own :class:`~arcagent.core.tool_registry.ToolRegistry` — the sole owner
  of the dispatch envelope. There is no second dispatch path to bypass.

One failing connection is contained: it is audited, logged, and skipped, and
every other connection still attaches — a fleet must not lose nine working
connections because the tenth bundle was deleted. The module stays disabled by
default, so an agent with no ``[modules.connectors]`` entry is untouched.

Runtime state lives in :mod:`arcagent.modules.connectors._runtime`. The agent
calls ``_runtime.configure`` once at startup; this capability reads state lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit

from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.connector_control import ConnectorReconcileResult
from arcagent.connector_reconcile import ConnectorReconcileQueue
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import ToolRegistry, ToolTransport
from arcagent.extension.approval import ApprovalBinding
from arcagent.extension.attachment import ExtensionAttachment, ToolSpec
from arcagent.extension.bridge import CapabilityBridge
from arcagent.extension.catalog import resolve_extension_roots
from arcagent.extension.contract_ledger import ToolContractLedger
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.loader import ExtensionLoader, LoadedExtension
from arcagent.extension.manifest import ToolPolicy
from arcagent.extension.secrets import SecretStore, select_secret_backend
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.install import (
    build_attachment,
    connector_env_file,
    resolve_secrets,
)
from arcagent.tools._decorator import capability

_logger = logging.getLogger("arcagent.modules.connectors.capabilities")

#: The manifest's attachment kind that is reached by spawning something. Every
#: other kind runs in this process, and an unknown one never gets this far —
#: ``build_attachment`` refuses it before anything is registered.
_SPAWNED_KIND = "cli"


class _PreparedRegistry:
    """Capture bridge output before it is atomically committed to the live registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.tools: dict[str, Any] = {}
        self.policy = registry.policy

    def register(self, tool: Any) -> None:
        self.tools[tool.name] = tool


@capability(name="connectors")
class Connectors:
    """Lifecycle-bound holder for the connector module's attached connections."""

    def __init__(self) -> None:
        self._registry: ToolRegistry | None = None
        self._registered: tuple[str, ...] = ()
        self._state_store: ConnectionStateStore | None = None
        self._revision = 0
        self._reconcile_queue: ConnectorReconcileQueue | None = None
        self._reconcile_task: asyncio.Task[None] | None = None

    async def setup(self, ctx: Any) -> None:
        del ctx  # Loader passes None; state lives in _runtime.
        state = _runtime.state()  # fails closed if configure() was never called
        self._registry = state.tool_registry
        result = await self.reconcile()
        self._registered = result.tools
        self._reconcile_queue = ConnectorReconcileQueue(
            _reconcile_backend_opener(state), actor_did=str(state.identity.did)
        )
        await self._drain_reconcile_commands()
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name=f"connector-reconcile:{state.agent_dir.name}"
        )
        _logger.info(
            "Connectors capability started (%d tool(s) from configured connections)",
            len(self._registered),
        )

    async def teardown(self) -> None:
        """Drop every verb this module put in the registry, and only those."""
        task = self._reconcile_task
        self._reconcile_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reconcile_queue = None
        registry = self._registry
        if registry is not None:
            for name in self._registered:
                registry.unregister(name)
        self._registered = ()
        self._registry = None
        if self._state_store is not None:
            await self._state_store.close()
        self._state_store = None
        _logger.info("Connectors capability stopped")

    async def reconcile(self) -> ConnectorReconcileResult:
        """Atomically replace connector tools from the deployment's current grants.

        This is agent-local control only. A management surface without this live
        capability must report ``activation_pending`` rather than claim a tool is
        callable before the agent next starts.
        """
        registry = self._registry
        if registry is None:
            return ConnectorReconcileResult(
                status="activation_pending",
                revision=self._revision,
                detail="connector module is not running in this process",
            )
        state = _runtime.state()
        prepared = _PreparedRegistry(registry)
        await self._attach(state, prepared)
        accepted = registry.replace_owned(set(self._registered), list(prepared.tools.values()))
        self._registered = tuple(sorted(accepted))
        self._revision += 1
        return ConnectorReconcileResult(
            status="applied", revision=self._revision, tools=self._registered
        )

    async def _drain_reconcile_commands(self) -> None:
        queue = self._reconcile_queue
        if queue is None:
            return
        state = _runtime.state()
        await queue.drain(state.agent_dir.name, self.reconcile)

    async def _reconcile_loop(self) -> None:
        """Retry durable work after a mutation from another process or a restart."""
        while True:
            try:
                await self._drain_reconcile_commands()
            except Exception:
                _logger.warning("connector reconcile command will retry", exc_info=True)
            await asyncio.sleep(1)

    # --- attaching ----------------------------------------------------------

    async def _attach(
        self, state: _runtime._State, registry: ToolRegistry | _PreparedRegistry
    ) -> tuple[str, ...]:
        """Attach every configured connection, containing the failure of any one."""
        sink = _audit_sink(state.telemetry)
        if state.tool_registry is None or state.human_gate is None:
            # Both are the envelope: without the registry a verb has no governed
            # dispatch path, and without the gate an outbound call has nothing to
            # ask. Attaching anyway would be an ungoverned connection.
            _refused(sink, state, "ungoverned", "no tool registry or no human gate")
            return ()
        try:
            granted = ConnectionRegistry(state.arc_dir).granted_to(state.agent_dir.name)
        except ExtensionError as exc:
            # An unreadable deployment file refuses loudly and nothing attaches:
            # an operator's grants silently half-applied is worse than an agent
            # that starts with none of them.
            _refused(sink, state, "unreadable_connections", exc.message)
            return ()
        if not granted:
            return ()

        self._state_store = await _open_state_store(state, sink)
        if self._state_store is None:
            return ()

        context = _AttachContext(
            state=state,
            sink=sink,
            registry=registry,
            store=self._state_store,
            secrets=_secret_store(state, sink),
        )
        registered: list[str] = []
        for instance, configured in sorted(granted.items()):
            registered.extend(await _attach_one(context, instance, configured, registered))
        return tuple(registered)


# --- steps ------------------------------------------------------------------


def _reconcile_backend_opener(state: _runtime._State) -> Any:
    """Use the agent's shared ArcStore opener, lazily importing the optional peer."""
    if state.arcstore_opener is not None:
        return state.arcstore_opener

    async def open_backend() -> Any:
        from arcstore.backends import open_backend

        backend = open_backend()
        await backend.start()
        return backend

    return open_backend


@dataclass(frozen=True)
class _AttachContext:
    """What attaching any one connection needs, resolved once for all of them."""

    state: _runtime._State
    sink: AuditSink
    registry: ToolRegistry | _PreparedRegistry
    store: ConnectionStateStore
    secrets: SecretStore | None


async def _attach_one(
    ctx: _AttachContext, instance: str, configured: Connection, taken: list[str]
) -> tuple[str, ...]:
    """Attach one connection. Any failure denies this one and no other.

    ``taken`` is the names earlier instances already registered. A second account
    of the same extension serves the same verb names, and letting the later one
    replace the earlier would silently route an operator's calls to a different
    account than the tool catalog says.
    """
    state = ctx.state
    try:
        loaded = await _load_bundle(ctx, configured.extension)
        # The credentials the operator connected this account with, read from the one
        # store every surface writes to. Absent, the connection is refused by name
        # rather than attached to serve verbs that answer 401.
        secrets = await resolve_secrets(
            loaded.manifest,
            connection=instance,
            store=ctx.secrets,
            caller_did=state.identity.did,
        )
        # Built once and reused: the connection whose tools were described has to
        # be the connection the registered verbs then call, or a stateful
        # attachment answers from a session nobody looked at.
        connection = build_attachment(loaded.manifest, loaded.path, secrets)
        served = await _servable_tools(ctx, instance, loaded, connection)
        specs = [spec for spec in served if spec.name not in taken]
        for spec in served:
            if spec.name in taken:
                _logger.warning(
                    "connectors: instance %r may not serve %r — an earlier instance "
                    "registered that name and keeps it",
                    instance,
                    spec.name,
                )
        attachment = ApprovalBinding(
            instance=instance,
            agent_did=state.identity.did,
            gate=state.human_gate,
            mode=configured.approval,
            audit_sink=ctx.sink,
        ).bind(connection, specs)
    except ExtensionError as exc:
        _refused(ctx.sink, state, "attach_refused", exc.message, instance=instance)
        return ()
    except Exception as exc:  # reason: fail-closed — one bad bundle, one dead connection
        _refused(
            ctx.sink, state, "attach_error", f"{type(exc).__name__}: {exc}", instance=instance
        )
        return ()

    report = CapabilityBridge(
        registry=cast(ToolRegistry, ctx.registry),
        attachment=attachment,
        transport=_transport(loaded.manifest.extension.attachment),
        source=f"extension:{loaded.name}",
        allow=loaded.manifest.tools.allow,
    ).register(specs)
    _logger.info(
        "connectors: instance %r contributed %d tool(s) (%d excluded)",
        instance,
        len(report.registered),
        len(report.denied),
    )
    return report.registered


async def _load_bundle(ctx: _AttachContext, extension: str) -> LoadedExtension:
    """Run the bundle through the one component that owns the signature gate.

    Ahead of everything else because building an attachment executes
    extension-declared code, and the supply-chain gate that runs only on bundles
    which have already run is decoration (REQ-282). A second verification here
    would be a second gate to keep in step, and the one that drifts is the one an
    attacker uses.
    """
    state = ctx.state
    loader = ExtensionLoader(
        roots=_extension_roots(state),
        # The bundle's own skills and tools register through an untrusted
        # ``extension:<name>`` root, which is what runs the AST validator and the
        # Sign gate over every file it ships. They do not reach the agent's
        # capability registry this startup: core bridges that registry into the
        # tool registry BEFORE capability lifecycles run, so handing them over
        # here would advertise verbs to the model that nothing could dispatch.
        registry=CapabilityRegistry(),
        tier=Tier(state.tier),
        audit_sink=ctx.sink,
        # The OPERATOR key, which is what ``arc connector`` pins a bundle's
        # signature to. Pinning to the agent's own key instead would make the
        # audited subject its own supply-chain authority, and would refuse every
        # bundle an operator actually signed. Absent, the loader's own rule
        # applies: above personal, no pin is itself the refusal (REQ-283).
        trusted_public_key=_pinned_key(state),
        # The same list ToolRegistry applies, read from the registry that applies
        # it. A second copy read from elsewhere is the one that drifts (D-580).
        egress_allow=ctx.registry.policy.egress_allow,
    )
    return await loader.load(extension)


async def _servable_tools(
    ctx: _AttachContext,
    instance: str,
    loaded: LoadedExtension,
    connection: ExtensionAttachment,
) -> list[ToolSpec]:
    """What this connection may offer: manifest-annotated, and not suspended.

    The annotation direction matters. ``classification`` and ``capability_tags``
    are what ``legs_for_call`` reads to resolve a call's trifecta legs, so an
    upstream allowed to serve its own would be allowed to declare itself
    ungated. They come from the manifest an operator installed, and a served verb
    the manifest never declared is judged as restrictively as
    ``extension/approval.py`` judges one at call time.
    """
    specs = _annotated(await connection.describe_tools(), loaded.manifest.tools)
    # Keyed by the connection, which is what the install approved under: the
    # contract belongs to the account, so an operator's re-approval clears the
    # suspension for every agent granted it rather than for one of them.
    ledger = ToolContractLedger(ctx.store, connection=instance, sink=ctx.sink)
    # The ledger's own filter, not a second one written here. Connecting approves
    # the contract the connection served at install, so an unapproved verb at
    # startup is one the upstream added afterwards — the rug-pull's other half,
    # and no more callable than a description that changed underneath.
    return await ledger.callable_tools(specs)


def _annotated(specs: list[ToolSpec], policy: ToolPolicy) -> list[ToolSpec]:
    """Replace every upstream annotation with the manifest's declaration."""
    declared = {tool.name: tool for tool in policy.declared}
    annotated: list[ToolSpec] = []
    for spec in specs:
        tool = declared.get(spec.name)
        annotated.append(
            spec.model_copy(
                update={
                    "classification": tool.classification if tool else "state_modifying",
                    "capability_tags": list(tool.capability_tags) if tool else [],
                }
            )
        )
    return annotated


async def _open_state_store(
    state: _runtime._State, sink: AuditSink
) -> ConnectionStateStore | None:
    """Open the durable plane holding approved contracts, or refuse to attach.

    Fail-closed: the approved hashes are the rug-pull defence's whole baseline,
    and a suspension that cannot be read is a suspension that is not enforced.
    """
    try:
        return await open_connection_state(opener=state.arcstore_opener)
    except Exception as exc:  # reason: no baseline, no defence — attach nothing
        _refused(sink, state, "no_connection_state", f"{type(exc).__name__}: {exc}")
        return None


# --- helpers ----------------------------------------------------------------


def _extension_roots(state: _runtime._State) -> tuple[Path, ...]:
    """Where this deployment's bundles live: its config's one root, or the search path.

    A configured root means exactly that root — the same thing
    ``arc connector --extensions-root`` means — so an operator who pinned a
    directory gets the directory they pinned. Unset, the deployment's ordered
    search path applies, which is what lets a fleet ship its bundles once instead
    of copying them into every agent (D-584).
    """
    configured = state.config.extensions_root
    if configured:
        return (Path(configured).expanduser().resolve(),)
    return resolve_extension_roots(state.arc_dir)


def _secret_store(state: _runtime._State, sink: AuditSink) -> SecretStore | None:
    """Where this deployment's connector credentials live, or ``None`` if there is no store.

    The same selection ``arc connector`` makes, from the same arc dir, so the
    agent reads the file the CLI wrote. ``None`` is not itself a failure: a ``cli``
    bundle whose binary owns its own authentication declares no ``[[secrets]]`` and
    needs no store, and taking the safest connectors away because a deployment has
    configured no vault would be the wrong refusal. A bundle that DOES declare a
    credential is refused by name in :func:`~arcagent.modules.connectors.install.
    resolve_secrets`.
    """
    try:
        backend = select_secret_backend(
            Tier(state.tier), env_file=connector_env_file(state.arc_dir)
        )
    except ExtensionError as exc:
        _logger.warning("connectors: no secret store on this deployment — %s", exc.message)
        return None
    return SecretStore(backend, sink=sink)


def _transport(kind: str) -> ToolTransport:
    """How the attachment is reached, recorded on every tool it supplies."""
    return ToolTransport.PROCESS if kind == _SPAWNED_KIND else ToolTransport.NATIVE


def _pinned_key(state: _runtime._State) -> bytes | None:
    """The operator public key bundle signatures verify against, if there is one."""
    signer = state.operator_signer
    return None if signer is None else bytes(signer.public_key)


class _TelemetryAuditSink:
    """Adapt the agent's ``telemetry.audit_event`` to arctrust's ``AuditSink.write``.

    The same adaptation ``core/prompt_context.py`` makes for prompt provenance:
    a module is handed telemetry, and every component under ``extension/`` speaks
    the arctrust sink protocol.
    """

    def __init__(self, telemetry: Any) -> None:
        self._telemetry = telemetry

    def write(self, event: AuditEvent) -> None:
        self._telemetry.audit_event(
            event.action,
            {
                "target": event.target,
                "outcome": event.outcome,
                "tier": event.tier,
                **event.extra,
            },
        )


def _audit_sink(telemetry: Any) -> AuditSink:
    """Where this module's verdicts land. Never nowhere, when telemetry exists."""
    return _TelemetryAuditSink(telemetry) if telemetry is not None else NullSink()


def _refused(
    sink: AuditSink, state: _runtime._State, reason: str, message: str, *, instance: str = ""
) -> None:
    """Record one denied connection through the single emission chokepoint (AU-2)."""
    _logger.warning("connectors: %s%s — %s", reason, f" [{instance}]" if instance else "", message)
    emit(
        AuditEvent(
            actor_did=state.identity.did,
            action="connector.attach_denied",
            target=f"connector:{instance}" if instance else "connector",
            outcome="deny",
            tier=state.tier,
            extra={"instance": instance, "reason": reason, "detail": message},
        ),
        sink,
    )


__all__ = ["Connectors"]
