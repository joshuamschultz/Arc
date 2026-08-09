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
from typing import Any, Literal, Protocol

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
from arcagent.extension.host import HostPrerequisiteDirector, HostVerdict
from arcagent.extension.host_install import host_install_dir, install_pinned_binary
from arcagent.extension.host_login import run_authorization_check, run_token_login
from arcagent.extension.manifest import (
    ArtifactPin,
    DeclaredTool,
    HostRequirement,
    SecretRequirement,
    load_manifest,
)
from arcagent.extension.secrets import Secret, SecretRef, SecretStore, select_secret_backend
from arcagent.extension.state import ConnectionStateStore, open_connection_state
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
    placement_environment,
    plan_connector,
    remove_connector,
    resolve_secrets,
    shape_supplied,
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


@dataclass(frozen=True)
class HostAuthorization:
    """One host binary that holds its own credential, and the command that grants it.

    ``token_command`` non-empty means Arc can complete this sign-in itself, given
    a token. Empty means only a person at the host can — a browser hand-off or a
    device code — and ``command`` is then the entire honest answer.
    """

    binary: str
    command: str
    instruction: str
    token_command: str = ""


@dataclass(frozen=True)
class HostSetupReport:
    """What putting a bundle's host prerequisites on this machine actually did.

    ``manual_steps`` is the bundle's own instruction, carried on every answer:
    a refusal that leaves an operator with no next step is the wall this button
    exists to remove, and a success is where a person may still prefer to read
    what was done.
    """

    installed: bool
    detail: str
    manual_steps: str = ""


#: Whether this connection's account is actually connected. Three states, not two,
#: because "Arc cannot tell" is a real answer and both collapses of it are lies: a
#: bundle reported signed in when it is not is the defect that shipped, and one
#: reported signed out sends an operator to redo a login already done.
SignInState = Literal["signed_in", "signed_out", "unknown"]


@dataclass(frozen=True)
class SuppliedCredential:
    """One value the operator supplies, and — when it is not secret — what it is now.

    ``sensitive`` is the manifest's own declaration, and it decides two separate
    things a surface must not decide for itself: whether the input is masked, and
    whether ``value`` was read back at all.

    ``value`` is a **deliberate, per-field narrowing of D-583** ("a key value is
    write-only"). That rule exists to stop a credential reaching a surface; a base
    URL is not a credential, and making an operator retype one they cannot see on
    every rotation is how a typo becomes an install that fails at probe with
    nothing to look at. So a non-sensitive field answers with what is stored, and a
    sensitive one is never read out of the store at all — not read and blanked,
    never read. It is empty for a field nothing has stored yet, which is the
    ordinary first-time case and not an error.
    """

    name: str
    prompt: str
    sensitive: bool = True
    value: str = ""


@dataclass(frozen=True)
class Authorization:
    """How one connection is authorised, and whether it currently answers.

    Two shapes, and a surface must be able to tell them apart without guessing:
    ``credentials`` non-empty means Arc holds the credential and the operator
    supplies it here; ``hosts`` non-empty means the binary holds its own and the
    operator runs :attr:`HostAuthorization.command` on the machine. A connection
    with neither is one Arc cannot help with, and says so rather than rendering an
    empty form over a button that does nothing.

    ``credentials`` carries each declared field, its prompt, and whether it is
    really secret. A sensitive one is never read out of the store, so a surface
    rendering this list cannot leak a credential (LLM02, LLM07); a non-sensitive
    one carries what is configured, which is what lets a rotation form show an
    operator the URL they already set instead of asking them to retype it blind.

    **``reachable`` and ``sign_in`` are different questions.** ``reachable`` is
    the probe: does this connection answer at all. ``sign_in`` is the account:
    has anyone signed this binary in. A bundle that probes with a ``version``
    subcommand answers reachable with no saved credential whatever — and a
    surface that rendered that probe as *Signed in* told an operator a
    security-relevant thing was done when it was not, and sent them away from the
    one action that would have done it. No surface may derive one from the other.
    """

    instance: str
    extension: str
    credentials: tuple[SuppliedCredential, ...]
    hosts: tuple[HostAuthorization, ...]
    reachable: bool
    detail: str
    sign_in: SignInState = "unknown"
    sign_in_detail: str = ""

    @property
    def working(self) -> bool:
        """The best evidence this connection is usable, in one word.

        The declared sign-in check when there is one, the probe when there is
        not — never the probe in place of a check that exists, which is the
        substitution that reported an empty ``dbxcli`` as a connected account.
        """
        if self.sign_in == "unknown":
            return self.reachable
        return self.sign_in == "signed_in"

    @property
    def supplied_to_arc(self) -> bool:
        """True when the operator's next step is typing a credential into Arc."""
        return bool(self.credentials)

    @property
    def token_binary(self) -> str:
        """The binary whose sign-in Arc can finish with a token, or empty.

        This is the one question a surface asks before offering a button, and it
        is answered from the manifest rather than guessed from the binary's name.
        """
        for host in self.hosts:
            if host.token_command:
                return host.binary
        return ""

    @property
    def manual_command(self) -> str:
        """The command only a person at this host can run, or empty when Arc can do it.

        A surface renders this under "Arc cannot finish this for you", so a
        token-accepting connector must NOT produce one: sending an operator to a
        terminal for a sign-in the button would have completed is the same lie
        told in the other direction.
        """
        for host in self.hosts:
            if not host.token_command:
                return host.command
        return ""


def _authorization(
    instance: str,
    plan: ConnectorPlan,
    probe: DoctorCheck,
    sign_in: tuple[SignInState, str],
    credentials: tuple[SuppliedCredential, ...],
    *,
    note: str = "",
) -> Authorization:
    """The one shape both the read and the sign-in answer with.

    One builder rather than two: reading how a connection is authorised and
    signing it in differ only in whether something was attempted first, and two
    copies of this mapping would let a surface see a host command on one verb and
    not the other.
    """
    state, sign_in_detail = sign_in
    return Authorization(
        instance=instance,
        extension=plan.extension,
        credentials=credentials,
        hosts=tuple(
            HostAuthorization(
                binary=required.name,
                command=required.authorize_command,
                instruction=required.instruction,
                token_command=required.token_command,
            )
            for required in plan.manifest.host_requires
            if required.authorize_command
        ),
        reachable=probe.status == "reachable",
        detail=f"{note} {probe.detail}".strip() if note else probe.detail,
        sign_in=state,
        sign_in_detail=sign_in_detail,
    )


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
        install_dir: Path | None = None,
    ) -> None:
        self._world = world
        self._audit = audit if audit is not None else AuditChain()
        self._factory: AttachmentFactory = attachment_factory or build_attachment
        self._install_dir = install_dir

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
        install_dir: Path | None = None,
    ) -> Connections:
        """Resolve an agent's world and bind it to a chain in one step."""
        world = resolve_world(
            agent_dir,
            arc_dir=arc_dir,
            data_dir=data_dir,
            extensions_root=extensions_root,
            env_file=env_file,
        )
        return cls(
            world,
            audit=audit,
            attachment_factory=attachment_factory,
            install_dir=install_dir,
        )

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
            ExtensionError: The name cannot be used, or the bundle was refused;
                ``.message`` names the step. The name check belongs to
                ``plan_connector`` — one rule on every path — so a surface refuses
                an unusable name here, before asking an operator for a credential.
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

    async def authorization(self, instance: str) -> Authorization:
        """How this connection is authorised — the answer both surfaces render.

        ``arc connector auth`` used to tell the operator of a ``cli`` connector
        that it "declares no credentials; nothing to supply", and arcui drew a
        Replace-credentials button over an empty form. Both were true and neither
        was usable: ``gh`` DOES need authorising, just not by Arc. This names the
        exact host command instead.

        Raises:
            ExtensionError: Nothing is connected under that name
                (``code`` :data:`NOT_INSTALLED`), or its bundle was refused.
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            probe = await self._reachability(plan, sink)
            sign_in = await self._sign_in_state(plan, sink)
            supplied = await self._supplied(plan, sink)
        return _authorization(instance, plan, probe, sign_in, supplied)

    async def authorize(self, instance: str, *, token: str = "") -> Authorization:
        """Sign this connection's host binary in — when that can be done without a human.

        Honest in both directions, which is the whole of it. A binary whose login
        opens a browser or prints a device code is NOT run: the answer names the
        command an operator types on this host, and claims nothing. One that reads
        a token on stdin IS run, and the answer is the probe taken afterwards —
        the only evidence a sign-in worked.

        **The token enters and does not come back.** It reaches the binary's stdin
        and nothing else: it is in no field of the returned
        :class:`Authorization`, no log record, and no audit event (LLM02, LLM07).
        Arc stores no copy — the binary owns its own credential, and a second copy
        would be a second place to leak it from.

        Args:
            instance: The connected account to sign in.
            token: The operator's credential, when they supplied one. Empty asks
                for the current state and the honest next step, and runs nothing.

        Returns:
            How this connection is authorised, and whether it now answers.

        Raises:
            ExtensionError: Nothing is connected under that name
                (``code`` :data:`NOT_INSTALLED`), or its bundle was refused.
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            note = await self._run_login(plan, token, sink)
            probe = await self._reachability(plan, sink)
            sign_in = await self._sign_in_state(plan, sink)
            supplied = await self._supplied(plan, sink)
        return _authorization(instance, plan, probe, sign_in, supplied, note=note)

    async def setup_host(self, extension: str) -> HostSetupReport:
        """Put the host binaries a bundle pins on this machine, verified before they land.

        Keyed by bundle rather than by instance: the prerequisite belongs to the
        bundle and is missing before any instance exists.

        This does not weaken REQ-262 — Arc still never RUNS the manifest's
        instruction. A manifest names bytes and a digest; the bytes are verified
        before anything is unpacked and land in a user-writable directory. What
        changes is only that an operator no longer has to reproduce a checksum
        check by hand to get past the wall.

        Returns:
            What happened, and the bundle's own steps for the person who would
            rather do it themselves — carried whether it worked or not.

        Raises:
            ExtensionError: The bundle name resolved to nothing, or its manifest
                was refused. A refusal from the install itself is reported in the
                returned :class:`HostSetupReport`, not raised: the operator needs
                the reason and the manual steps together.
        """
        with self._audit.open() as sink:
            plan = self._plan(extension, extension, sink)
            steps = "\n\n".join(
                required.instruction for required in plan.manifest.host_requires
            )
            if not plan.unsatisfied_host:
                return HostSetupReport(True, f"{extension} has what it needs here.", steps)
            if plan.manifest.artifact is None:
                return HostSetupReport(
                    False,
                    f"{extension} pins no downloadable build, so Arc cannot install it.",
                    steps,
                )
            return await self._install_host(plan.manifest.artifact, plan, steps, sink)

    async def _install_host(
        self, pin: ArtifactPin, plan: ConnectorPlan, steps: str, sink: AuditSink
    ) -> HostSetupReport:
        """Download, verify, and place one pinned binary, reporting either verdict.

        The verdict is re-taken from the host afterwards rather than inferred from
        "the file was written": a binary in a directory that is not on ``PATH`` is
        one the connector still cannot find, and reporting it as ready would be
        the same false success the button exists to avoid.
        """
        target = self._install_dir if self._install_dir is not None else host_install_dir()
        try:
            path = await install_pinned_binary(
                pin,
                install_dir=target,
                caller_did=self._world.did,
                audit_sink=sink,
                tier=self._world.tier,
            )
        except ExtensionError as exc:
            return HostSetupReport(False, exc.message, steps)

        remaining = HostPrerequisiteDirector().unsatisfied(plan.manifest.host_requires)
        if remaining:
            return HostSetupReport(
                False,
                f"Installed {path}, but {remaining[0].name} is still not on this host's "
                f"PATH — add {target} to PATH and try again.",
                steps,
            )
        return HostSetupReport(
            True, f"Installed {path}, verified against its published digest.", steps
        )

    async def _sign_in_state(
        self, plan: ConnectorPlan, sink: AuditSink
    ) -> tuple[SignInState, str]:
        """Ask every binary that declares a check whether this account is connected.

        Separate from :meth:`_reachability` because they are separate questions.
        The probe runs the connector; this runs the command the manifest says
        proves an account is signed in (``dbxcli account``, ``gh auth status``).
        A bundle declaring none reports ``unknown`` — the one honest answer when
        there is no evidence, and never a green tick over an empty account.
        """
        checks = [
            required for required in plan.manifest.host_requires if required.verify_command
        ]
        if not checks:
            return ("unknown", "")

        placed = await self._placement(plan, sink)
        detail = ""
        for required in checks:
            result = await run_authorization_check(
                required,
                caller_did=self._world.did,
                audit_sink=sink,
                tier=self._world.tier,
                env=placed,
            )
            # The command, then what it said. A surface renders this beside the
            # badge, so it has to be the evidence FOR that badge: the probe's own
            # line ("signs in on this host; Arc ran nothing") sat inside a green
            # "Signed in" box describing something nobody had run.
            detail = f"{required.verify_command}: {result.detail}".strip().rstrip(":")
            if not result.known:
                return ("unknown", detail)
            if not result.authorized:
                return ("signed_out", detail)
        return ("signed_in", detail)

    async def _run_login(self, plan: ConnectorPlan, token: str, sink: AuditSink) -> str:
        """Run the one login Arc can finish, or say plainly why it did not run one."""
        accepting = next(
            (required for required in plan.manifest.host_requires if required.token_command),
            None,
        )
        if accepting is None:
            return f"{plan.extension} signs in on this host; Arc ran nothing."
        if not token:
            return (
                f"{accepting.name} signs in with a token — supply one and Arc will run it."
            )
        result = await run_token_login(
            accepting,
            token=token,
            caller_did=self._world.did,
            audit_sink=sink,
            tier=self._world.tier,
        )
        return result.detail

    async def doctor(self, instance: str) -> tuple[DoctorCheck, ...]:
        """Everything that could be wrong with one connection, without fixing any of it.

        Unmet prerequisites, whether each declared credential is present (never its
        value), whether the connection answers, and — as its own row — whether an
        account is actually signed in. Two rows because they are two facts: a
        connection whose binary starts and whose account was never connected is
        reachable and useless, and one row reading "reachable" is read as "fine".
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            checks = [
                DoctorCheck(verdict.name, "missing", verdict.instruction)
                for verdict in plan.unsatisfied_host
            ]
            checks += await self._credential_checks(plan, instance, sink)
            checks.append(await self._reachability(plan, sink))
            state, detail = await self._sign_in_state(plan, sink)
            checks.append(DoctorCheck("sign-in", state, detail))
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
                state=await self._connection_state(),
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

        # Shaped on this path too, and by the same function the install uses: a rule
        # applied only at creation is a connection that works when it is made and
        # breaks the first time its credential is rotated.
        shaped = shape_supplied(plan, secrets)
        written: list[str] = []
        with self._audit.open() as sink:
            store = self._store(sink)
            for required in plan.secrets:
                value = shaped.get(required.name)
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

        An install already approves what it probed, so this is the RE-approval an
        operator runs after a contract legitimately changed — not a step a fresh
        connection needs before its tools work.

        Returns:
            The approved tool names.

        Raises:
            ExtensionError: Nothing is connected under that name, or the
                connection has no record for an approval to be written against
                (``code`` :data:`~arcagent.extension.contract_ledger.
                APPROVAL_NOT_STORED`).
        """
        from arcagent.extension.contract_ledger import ToolContractLedger

        with self._audit.open() as sink:
            attachment = await self._attachment(self._plan_for(instance, sink), sink)
            specs = await attachment.describe_tools()
            ledger = ToolContractLedger(
                await self._connection_state(),
                agent=self._world.agent,
                instance=instance,
                sink=sink,
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
                state=await self._connection_state(),
            )

    # --- internals -------------------------------------------------------

    async def _connection_state(self) -> ConnectionStateStore:
        """The connection directory this agent's records and approvals live in.

        Opened per verb rather than held: the same reason the audit chain is, and
        the same directory every surface resolves — a second spelling of the data
        dir would mean the agent reads a store no surface ever wrote to.
        """
        return await open_connection_state(str(self._world.data_dir))

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

    async def _supplied(
        self, plan: ConnectorPlan, sink: AuditSink
    ) -> tuple[SuppliedCredential, ...]:
        """Every field the operator supplies, and the value of the ones that are not secret.

        The manifest's ``sensitive`` flag is the ONLY input to that decision, and it
        is read here — there is no parameter on any public method that could widen
        it, so no call site can ask for "all the values" and no future one can be
        added without deleting this comment first. A sensitive field is not read out
        of the store and blanked; :meth:`SecretStore.get` is never called for it, so
        there is no path for the value to be on even for a moment.

        A field with nothing stored answers empty rather than being dropped: a form
        built from this list must still draw the input on a connection that has
        never been configured.
        """
        store = self._store(sink)
        rows: list[SuppliedCredential] = []
        for declared in plan.secrets:
            value = ""
            if not declared.sensitive:
                ref = SecretRef(
                    agent=self._world.agent, instance=plan.instance, field=declared.name
                )
                found = await store.get(ref, caller_did=self._world.did)
                value = found.reveal() if found is not None else ""
            rows.append(
                SuppliedCredential(
                    name=declared.name,
                    prompt=declared.prompt,
                    sensitive=declared.sensitive,
                    value=value,
                )
            )
        return tuple(rows)

    async def _placement(self, plan: ConnectorPlan, sink: AuditSink) -> dict[str, Secret]:
        """The credentials this connection's own binary reads from its environment.

        The sign-in check runs the SAME program the connection's verbs run, so it has
        to run it the same way. ``dbxcli account`` answers from ``DBXCLI_ACCESS_TOKEN``;
        a check taken outside that environment reports "signed out" for an account that
        was connected a moment ago, which sends an operator to redo a login that is
        already done — the mirror of the green tick over an empty account.

        A connection whose credentials are not in the store yet is not an error here:
        it is a connection that is genuinely signed out, and reporting that is the
        whole job. The refusal belongs to the verbs, which is where it already is.
        """
        try:
            secrets = await resolve_secrets(
                plan.manifest,
                agent=self._world.agent,
                instance=plan.instance,
                store=self._store(sink),
                caller_did=self._world.did,
            )
        except ExtensionError:
            return {}
        return placement_environment(plan.manifest, secrets)

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
    "ArtifactPin",
    "AttachmentFactory",
    "AuditChain",
    "Authorization",
    "CatalogEntry",
    "ClosableSink",
    "ConnectionWorld",
    "Connections",
    "ConnectorPlan",
    "DeclaredTool",
    "DoctorCheck",
    "ExtensionError",
    "HostAuthorization",
    "HostPrerequisiteDirector",
    "HostRequirement",
    "HostSetupReport",
    "HostVerdict",
    "InstallReport",
    "InstanceConfig",
    "ProbeResult",
    "RemovalReport",
    "SecretRequirement",
    "SignInState",
    "SuppliedCredential",
    "Tier",
    "ToolSpec",
    "agent_tier",
    "catalog",
    "resolve_roots",
    "resolve_world",
]
