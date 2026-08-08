"""SPEC-064 — the one seam a surface uses to manage an agent's connected accounts.

:mod:`arcagent.modules.connectors.install` owns the *sequence* — resolve, manifest,
host, verify, secrets, probe, persist — and the rollback that unwinds it. That is
one layer too low to be the seam a surface drives: three of them (``arc
connector``, the arcui routes, the arctui modals) each re-derived the same
orchestration around it — resolve the agent's world, open and close the operator
WORM chain, select the secret backend for the tier, plan, install, re-auth, probe,
assemble doctor rows, record an approved contract, remove. This module is that
orchestration, once.

It **orchestrates and does not re-implement**: every ordering decision, the
signature gate, the probe and the rollback still belong to ``plan_connector`` and
``install_connector``. Moving any of them up here would recreate the second copy
this module exists to delete.

**A credential enters and does not come back.** :meth:`Connections.install` and
:meth:`Connections.reauth` take a ``{field: value}`` mapping and answer with field
NAMES; no type this module returns has a field able to hold a value, so a surface
that renders one of them cannot leak one (LLM02, LLM07).

**The audit chain's lifetime belongs here.** A :class:`~arctrust.WormSink` holds an
exclusive ``flock`` for its lifetime, so a caller that opens one and forgets to
close it locks every later writer out of the deployment's chain. Callers describe
their chain once with :class:`AuditChain` and never hold an open sink.
"""

from __future__ import annotations

import contextlib
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from arctrust.audit import AuditEvent, AuditSink, NullSink
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment, ProbeResult, ToolSpec
from arcagent.extension.catalog import (
    BUNDLES_DIRNAME,
    MANIFEST_NAME,
    ExtensionCatalog,
    ExtensionResolution,
    resolve_extension_roots,
)
from arcagent.extension.host import HostVerdict
from arcagent.extension.manifest import (
    DeclaredTool,
    HostRequirement,
    SecretRequirement,
    load_manifest,
)
from arcagent.extension.secrets import SecretRef, SecretStore, select_secret_backend
from arcagent.modules.connectors.install import (
    AttachmentFactory,
    ConnectorPlan,
    InstallReport,
    InstanceConfig,
    RemovalReport,
    build_attachment,
    connector_env_file,
    install_connector,
    load_egress_allow,
    load_instances,
    plan_connector,
    remove_connector,
    resolve_secrets,
)

#: Refusal code for a verb aimed at an instance this agent has not connected. A
#: surface that distinguishes "not found" from "refused" (a web route answering
#: 404 rather than 400) reads this rather than matching on the message text.
NOT_INSTALLED = "CONNECTOR_NOT_INSTALLED"

#: Refusal code for an agent directory that is not one, or has no identity yet.
NO_AGENT = "CONNECTOR_CONTEXT"

#: Refusal code for a credential no manifest declares — the allowlist that stops
#: a rotation from writing an arbitrary entry into the agent's credential file.
UNDECLARED_CREDENTIAL = "CONNECTOR_SECRET_UNDECLARED"

#: The operator key an extension bundle's signatures are pinned to, under an
#: Arc config dir. Read-only here — see :meth:`Connections.install`.
_OPERATOR_KEY = ("operator", "operator.key")


def _refuse(code: str, message: str, **details: Any) -> ExtensionError:
    """The one refusal shape every surface renders: a code to branch on, a line to show."""
    return ExtensionError(code=code, message=message, details=details)


# ---------------------------------------------------------------------------
# The agent's world
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionWorld:
    """Everything every verb needs, resolved once from one agent directory.

    ``agent`` is the DIRECTORY name, not a roster label, because it keys the secret
    store: a surface keying on anything else would write a credential the other
    surfaces cannot see and the agent never reads.
    """

    agent_dir: Path
    arc_dir: Path
    data_dir: Path
    agent: str
    did: str
    tier: Tier
    extension_roots: tuple[Path, ...]
    env_file: Path


def resolve_world(
    agent_dir: Path | str,
    *,
    arc_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    extensions_root: Path | str | None = None,
    env_file: Path | str | None = None,
) -> ConnectionWorld:
    """Resolve one agent's identity, tier, and paths, or refuse naming what is wrong.

    Args:
        agent_dir: The agent directory holding ``arcagent.toml``.
        arc_dir: Arc config dir (default ``<arc_home>``) — where the operator key
            that pins bundle signatures and signs the audit chain lives.
        data_dir: Operational data dir (default arcstore's) — where the connection
            state and the audit chain live. Created when the caller names one.
        extensions_root: Use exactly this bundle root. An operator pointing a
            command at one directory gets that directory and no fallback behind it.
        env_file: Owner-only credential store (default ``<agent>/connectors.env``).

    Returns:
        The resolved world, ready to hand to :class:`Connections`.

    Raises:
        ExtensionError: No agent config, an unparseable one, or no DID to record as
            the actor on a credential write. ``code`` is :data:`NO_AGENT`.
    """
    directory = Path(agent_dir).expanduser().resolve()
    raw = _read_agent_config(directory)
    did = str(raw.get("identity", {}).get("did", ""))
    if not did:
        config = directory / "arcagent.toml"
        raise _refuse(NO_AGENT, f"{config} has no [identity].did — run 'arc agent build' first.")

    return ConnectionWorld(
        agent_dir=directory,
        arc_dir=Path(arc_dir).expanduser() if arc_dir else _arc_home(),
        data_dir=_data_dir(data_dir),
        agent=directory.name,
        did=did,
        tier=_tier_of(raw),
        extension_roots=resolve_roots(directory, extensions_root=extensions_root),
        env_file=(
            Path(env_file).expanduser().resolve() if env_file else connector_env_file(directory)
        ),
    )


def resolve_roots(
    agent_dir: Path | str | None = None, *, extensions_root: Path | str | None = None
) -> tuple[Path, ...]:
    """The ordered bundle search path, or exactly the one root the operator named.

    ``agent_dir=None`` asks the fleet-wide question — the one a surface has before
    an operator has chosen which agent to connect.
    """
    if extensions_root:
        return (Path(extensions_root).expanduser().resolve(),)
    return resolve_extension_roots(Path(agent_dir) if agent_dir is not None else None)


def agent_tier(agent_dir: Path | str) -> Tier:
    """The tier one agent runs at, read from its ``arcagent.toml``.

    Separate from :func:`resolve_world` because listing what a deployment could
    connect needs the tier a manifest is parsed at, and nothing else — an agent
    that has not been built yet still has an answer.
    """
    return _tier_of(_read_agent_config(Path(agent_dir).expanduser().resolve()))


def _read_agent_config(agent_dir: Path) -> dict[str, Any]:
    """Parse one agent's ``arcagent.toml``, or refuse naming what is wrong."""
    config = agent_dir / "arcagent.toml"
    if not config.is_file():
        raise _refuse(NO_AGENT, f"no arcagent.toml at {config} — is that an agent directory?")
    try:
        parsed: dict[str, Any] = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _refuse(NO_AGENT, f"{config} — {exc}") from exc
    return parsed


def _tier_of(raw: Mapping[str, Any]) -> Tier:
    return Tier(str(raw.get("security", {}).get("tier", "personal")))


def _arc_home() -> Path:
    from arctrust.paths import arc_home

    return arc_home()


def _data_dir(given: Path | str | None) -> Path:
    """The operational data directory — arcstore's, unless the operator names one."""
    if given:
        path = Path(given).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    from arcstore import resolve_data_dir

    return Path(resolve_data_dir(None))


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------


class ClosableSink(Protocol):
    """An audit sink whose lifetime the caller hands to this module."""

    def write(self, event: AuditEvent) -> None: ...

    def close(self) -> None: ...


class AuditChain:
    """Where connector verdicts are recorded — and who is responsible for closing it.

    A :class:`~arctrust.WormSink` holds an exclusive ``flock`` for its lifetime, so
    one left open locks every later writer out of the deployment's chain. Which is
    why a caller describes its chain once here and never holds an open sink: a
    command-line surface hands in an opener and every verb opens and closes its own
    (:meth:`opened_by`); a long-running server hands in the chain it already holds
    and this module never closes it (:meth:`held`).

    The default discards — the right answer for a read-only caller that takes no
    verdict, and never a silent downgrade of one that does.
    """

    def __init__(
        self,
        *,
        sink: AuditSink | None = None,
        opener: Callable[[], ClosableSink] | None = None,
    ) -> None:
        self._sink = sink
        self._opener = opener

    @classmethod
    def held(cls, sink: AuditSink) -> AuditChain:
        """A chain the caller already holds open. Written to, never closed."""
        return cls(sink=sink)

    @classmethod
    def opened_by(cls, opener: Callable[[], ClosableSink]) -> AuditChain:
        """A chain opened for one verb and closed the moment that verb ends."""
        return cls(opener=opener)

    @contextlib.contextmanager
    def open(self) -> Iterator[AuditSink]:
        """Yield the sink one verb records into, closing it if this chain owns it."""
        if self._opener is None:
            yield self._sink if self._sink is not None else NullSink()
            return
        sink = self._opener()
        try:
            yield sink
        finally:
            sink.close()


# ---------------------------------------------------------------------------
# What could be connected
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One bundle on the search path, and what connecting it would give the agent.

    A directory that will not parse keeps its place with ``error`` saying why: one
    broken bundle blanking a catalog is a far worse failure than a listed bundle an
    operator cannot install. Every other field is empty on such an entry.
    """

    name: str
    path: Path
    official: bool
    version: str = ""
    description: str = ""
    error: str = ""
    attachment: str = ""
    tier_floor: str = ""
    approval_default: str = ""
    secrets: tuple[SecretRequirement, ...] = ()
    host_requires: tuple[HostRequirement, ...] = ()
    tools: tuple[DeclaredTool, ...] = ()


def catalog(
    *,
    roots: Sequence[Path],
    tier: Tier = Tier.PERSONAL,
    audit_sink: AuditSink | None = None,
) -> tuple[CatalogEntry, ...]:
    """Every bundle the search path holds, in the order the path defines.

    Args:
        roots: The bundle search path — :func:`resolve_roots` composes it.
        tier: The tier manifests are parsed at. A bundle this tier refuses is listed
            with that refusal as its reason rather than advertised as installable.
        audit_sink: Where the catalog would record a verdict. Listing takes none —
            the refusal an operator acts on is taken (and recorded) by an install.

    Returns:
        One entry per name, never raising on a bundle it cannot read.
    """
    listing = ExtensionCatalog(
        roots=roots, tier=tier, audit_sink=audit_sink if audit_sink is not None else NullSink()
    )
    return tuple(_catalog_entry(resolution, tier) for resolution in listing.available())


def _catalog_entry(resolution: ExtensionResolution, tier: Tier) -> CatalogEntry:
    """Read one resolved bundle into a listing entry, never raising."""
    entry = CatalogEntry(
        name=resolution.name,
        path=resolution.path,
        official=resolution.official,
        version=resolution.version,
        description=resolution.description,
        error=resolution.error,
    )
    if resolution.error:
        return entry
    try:
        text = (resolution.path / MANIFEST_NAME).read_text(encoding="utf-8")
        manifest = load_manifest(text, tier=tier)
    except (OSError, ValueError, ValidationError, ExtensionError) as exc:
        return CatalogEntry(
            name=resolution.name,
            path=resolution.path,
            official=resolution.official,
            error=f"{type(exc).__name__}: {exc}",
        )
    header = manifest.extension
    return CatalogEntry(
        name=resolution.name,
        path=resolution.path,
        official=resolution.official,
        version=header.version,
        description=header.description,
        attachment=header.attachment,
        tier_floor=header.tier_floor.value,
        approval_default=manifest.approval.default,
        secrets=tuple(manifest.secrets),
        host_requires=tuple(manifest.host_requires),
        tools=tuple(manifest.tools.declared),
    )


# ---------------------------------------------------------------------------
# Managing one agent's connections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorCheck:
    """One line of a connection's health report. Coordinates only, never a value."""

    check: str
    status: str
    detail: str


class Connections:
    """One agent's connected accounts: what could be connected, what is, and whether it works.

    Every verb runs inside the caller's :class:`AuditChain`, so a surface never
    holds an open WORM sink, and every refusal is an :class:`ExtensionError` whose
    ``.message`` is written for an operator and whose ``.code`` is what a surface
    branches on.
    """

    def __init__(
        self,
        world: ConnectionWorld,
        *,
        audit: AuditChain | None = None,
        attachment_factory: AttachmentFactory | None = None,
    ) -> None:
        self._world = world
        self._audit = audit if audit is not None else AuditChain()
        self._factory: AttachmentFactory = attachment_factory or build_attachment

    @classmethod
    def for_agent(
        cls,
        agent_dir: Path | str,
        *,
        arc_dir: Path | str | None = None,
        data_dir: Path | str | None = None,
        extensions_root: Path | str | None = None,
        env_file: Path | str | None = None,
        audit: AuditChain | None = None,
        attachment_factory: AttachmentFactory | None = None,
    ) -> Connections:
        """Resolve an agent's world and bind it to a chain in one step."""
        world = resolve_world(
            agent_dir,
            arc_dir=arc_dir,
            data_dir=data_dir,
            extensions_root=extensions_root,
            env_file=env_file,
        )
        return cls(world, audit=audit, attachment_factory=attachment_factory)

    @property
    def world(self) -> ConnectionWorld:
        """The agent's resolved paths and identity — what a surface reports back."""
        return self._world

    # --- reading ---------------------------------------------------------

    def catalog(self) -> tuple[CatalogEntry, ...]:
        """What this agent could connect, from its own search path."""
        with self._audit.open() as sink:
            return catalog(
                roots=self._world.extension_roots, tier=self._world.tier, audit_sink=sink
            )

    def installed(self) -> dict[str, InstanceConfig]:
        """What this agent has connected, from the blocks its runtime binds."""
        return load_instances(self._world.agent_dir)

    def plan(self, extension: str, instance: str) -> ConnectorPlan:
        """The read-only half of an install: resolve the bundle, parse it, check the host.

        Nothing is written, so a surface can show an operator the credentials it is
        about to ask for and the approval mode it will run under before the first
        field is filled in.

        Raises:
            ExtensionError: The bundle was refused; ``.message`` names the step.
        """
        with self._audit.open() as sink:
            return self._plan(extension, instance, sink)

    def plan_for(self, instance: str) -> ConnectorPlan:
        """The plan behind an already-connected instance.

        Raises:
            ExtensionError: Nothing is connected under that name
                (``code`` :data:`NOT_INSTALLED`), or its bundle was refused.
        """
        with self._audit.open() as sink:
            return self._plan_for(instance, sink)

    async def tools(self, instance: str) -> tuple[ToolSpec, ...]:
        """The verbs one connection offers the agent right now."""
        with self._audit.open() as sink:
            attachment = await self._attachment(self._plan_for(instance, sink), sink)
            return tuple(await attachment.describe_tools())

    async def probe(self, instance: str) -> ProbeResult:
        """Open the connection right now — the only honest answer to "does this work"."""
        with self._audit.open() as sink:
            attachment = await self._attachment(self._plan_for(instance, sink), sink)
            return await attachment.probe()

    async def doctor(self, instance: str) -> tuple[DoctorCheck, ...]:
        """Everything that could be wrong with one connection, without fixing any of it.

        Unmet prerequisites, whether each declared credential is present (never its
        value), and whether the connection answers.
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            checks = [
                DoctorCheck(verdict.name, "missing", verdict.instruction)
                for verdict in plan.unsatisfied_host
            ]
            checks += await self._credential_checks(plan, instance, sink)
            checks.append(await self._reachability(plan, sink))
        return tuple(checks)

    # --- writing ---------------------------------------------------------

    async def install(self, plan: ConnectorPlan, secrets: Mapping[str, str]) -> InstallReport:
        """Run the shared install path: verify, store credentials, probe, then persist.

        The ordering, the signature gate that closes before extension code runs, and
        the rollback that takes a credential back out when the probe fails all
        belong to :func:`~arcagent.modules.connectors.install.install_connector`.

        Args:
            plan: What :meth:`plan` resolved.
            secrets: One value per declared credential. Consumed and dropped — the
                report names fields, never values.

        Raises:
            ExtensionError: A step refused. ``details["step"]`` names which, and
                nothing was left behind.
        """
        with self._audit.open() as sink:
            return await install_connector(
                plan,
                agent_dir=self._world.agent_dir,
                agent=self._world.agent,
                secret_values=secrets,
                store=self._store(sink),
                caller_did=self._world.did,
                attachment_factory=self._factory,
                audit_sink=sink,
                trusted_public_key=self._pinned_key(),
            )

    async def reauth(self, plan: ConnectorPlan, secrets: Mapping[str, str]) -> tuple[str, ...]:
        """Re-supply an instance's credentials — a rotation, or a first-time fix.

        Only fields the manifest declares are written; that allowlist is what stops
        a rotation from putting an arbitrary entry into the agent's credential file.

        Returns:
            The field NAMES written, in declaration order.

        Raises:
            ExtensionError: A supplied field is not declared by the bundle
                (``code`` :data:`UNDECLARED_CREDENTIAL`).
        """
        declared = {required.name for required in plan.secrets}
        unknown = sorted(set(secrets) - declared)
        if unknown:
            raise _refuse(
                UNDECLARED_CREDENTIAL,
                f"{plan.extension} declares no credential named {unknown[0]!r}",
                extension=plan.extension,
                field=unknown[0],
            )

        written: list[str] = []
        with self._audit.open() as sink:
            store = self._store(sink)
            for required in plan.secrets:
                value = secrets.get(required.name)
                if not value:
                    continue
                ref = SecretRef(
                    agent=self._world.agent, instance=plan.instance, field=required.name
                )
                await store.put(ref, value, caller_did=self._world.did)
                written.append(required.name)
        return tuple(written)

    async def approve(self, instance: str) -> tuple[str, ...]:
        """Record the tool contract this connection serves RIGHT NOW as approved.

        The rug-pull defence (REQ-291): a tool whose shape changes afterwards is
        suspended until an operator approves it again.

        Returns:
            The approved tool names.
        """
        from arcagent.extension.contract_ledger import ToolContractLedger
        from arcagent.extension.state import open_connection_state

        with self._audit.open() as sink:
            attachment = await self._attachment(self._plan_for(instance, sink), sink)
            specs = await attachment.describe_tools()
            state = await open_connection_state(str(self._world.data_dir))
            ledger = ToolContractLedger(
                state, agent=self._world.agent, instance=instance, sink=sink
            )
            await ledger.approve(specs, actor_did=self._world.did)
        return tuple(spec.name for spec in specs)

    async def remove(self, instance: str) -> RemovalReport:
        """Drop one connected account: its credentials, its config block, its state.

        Removing something that was never connected is reported, not raised: an
        operator cleaning up after a failed install must not be blocked by a step
        that already has nothing to do.
        """
        with self._audit.open() as sink:
            return await remove_connector(
                agent_dir=self._world.agent_dir,
                agent=self._world.agent,
                instance=instance,
                store=self._store(sink),
                caller_did=self._world.did,
                secret_fields=self._declared_secret_fields(instance, sink),
            )

    # --- internals -------------------------------------------------------

    def _plan(self, extension: str, instance: str, sink: AuditSink) -> ConnectorPlan:
        """Plan against an already-open chain, so no verb opens a second ``flock``."""
        return plan_connector(
            extensions_root=self._world.extension_roots,
            extension=extension,
            instance=instance,
            tier=self._world.tier,
            audit_sink=sink,
            egress_allow=load_egress_allow(self._world.agent_dir),
        )

    def _plan_for(self, instance: str, sink: AuditSink) -> ConnectorPlan:
        configured = self.installed().get(instance)
        if configured is None:
            raise _refuse(
                NOT_INSTALLED,
                f"no connector instance named {instance!r} on {self._world.agent}",
                instance=instance,
            )
        return self._plan(configured.extension, instance, sink)

    def _declared_secret_fields(self, instance: str, sink: AuditSink) -> list[str]:
        """Which credentials this instance declared, when the bundle is still readable.

        A bundle deleted before its connection was removed leaves nothing to read,
        so removal proceeds on the config block alone rather than refusing to clean up.
        """
        try:
            plan = self._plan_for(instance, sink)
        except ExtensionError:
            return []
        return [required.name for required in plan.secrets]

    async def _attachment(self, plan: ConnectorPlan, sink: AuditSink) -> ExtensionAttachment:
        """The connection as it really is: built with the credentials it was connected with.

        Every read verb this class offers goes through here, so a surface can never
        report on a connection the operator does not have. A declared credential the
        store does not hold refuses by name rather than answering from a blank one.
        """
        secrets = await resolve_secrets(
            plan.manifest,
            agent=self._world.agent,
            instance=plan.instance,
            store=self._store(sink),
            caller_did=self._world.did,
        )
        return self._factory(plan.manifest, plan.bundle, secrets)

    def _store(self, sink: AuditSink) -> SecretStore:
        """The one place a connector credential is written or read."""
        backend = select_secret_backend(self._world.tier, env_file=self._world.env_file)
        return SecretStore(backend, sink=sink)

    def _pinned_key(self) -> bytes | None:
        """The operator key an extension bundle's signatures are pinned to (REQ-283).

        Read-only: never mints a key, so an install above personal tier on a machine
        with no operator key is refused by the loader rather than quietly satisfied
        by a keypair this call generated moments earlier.
        """
        from arctrust import OperatorKey

        try:
            key = OperatorKey.load(
                self._world.arc_dir.joinpath(*_OPERATOR_KEY), generate_if_absent=False
            )
        except (FileNotFoundError, OSError):
            return None
        return key.public_key

    async def _credential_checks(
        self, plan: ConnectorPlan, instance: str, sink: AuditSink
    ) -> list[DoctorCheck]:
        """One row per declared credential: present or missing, never the value."""
        store = self._store(sink)
        rows: list[DoctorCheck] = []
        for required in plan.secrets:
            ref = SecretRef(agent=self._world.agent, instance=instance, field=required.name)
            found = await store.get(ref, caller_did=self._world.did)
            status = "present" if found else "missing"
            rows.append(DoctorCheck(required.name, status, str(self._world.env_file)))
        return rows

    async def _reachability(self, plan: ConnectorPlan, sink: AuditSink) -> DoctorCheck:
        """Probing is the only honest answer to "does this connection work"."""
        try:
            result = await (await self._attachment(plan, sink)).probe()
        except Exception as exc:  # reason: doctor reports failures, it does not raise them
            return DoctorCheck("connection", "error", f"{type(exc).__name__}: {exc}")
        return DoctorCheck(
            "connection", "reachable" if result.reachable else "unreachable", result.detail
        )


__all__ = [
    "BUNDLES_DIRNAME",
    "NOT_INSTALLED",
    "NO_AGENT",
    "UNDECLARED_CREDENTIAL",
    "AttachmentFactory",
    "AuditChain",
    "CatalogEntry",
    "ClosableSink",
    "ConnectionWorld",
    "Connections",
    "ConnectorPlan",
    "DeclaredTool",
    "DoctorCheck",
    "ExtensionError",
    "HostRequirement",
    "HostVerdict",
    "InstallReport",
    "InstanceConfig",
    "ProbeResult",
    "RemovalReport",
    "SecretRequirement",
    "Tier",
    "ToolSpec",
    "agent_tier",
    "catalog",
    "resolve_roots",
    "resolve_world",
]
