"""SPEC-064 — the one seam a surface uses to manage a deployment's connected accounts.

:mod:`arcagent.modules.connectors.install` owns the *sequence* — resolve, manifest,
host, verify, secrets, probe, persist — and the rollback that unwinds it. That is
one layer too low to be the seam a surface drives: three of them (``arc
connector``, the arcui routes, the arctui modals) each re-derived the same
orchestration around it — resolve the deployment, open and close the operator
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

**A connection belongs to the deployment; a grant hands it to an agent.** Every
verb here is scoped to the deployment rather than to an agent, because that is
what a connected account is: one credential, entered once, and a list of the
agents permitted to use it. :meth:`Connections.grant` and
:meth:`Connections.revoke` are the whole of access control, and the running
agent enforces them (:mod:`arcagent.modules.connectors.capabilities`). Nothing
here writes into an agent's directory, so no surface can hand an agent a
credential by writing a config block.
"""

from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.paths import arc_team, config_file, default_operator_key_path

from arcagent.connection_catalog import AuditChain, CatalogEntry, ClosableSink, catalog
from arcagent.connector_control import ConnectorControl, ConnectorReconcileResult
from arcagent.connector_reconcile import ConnectorReconcileQueue
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment, ProbeResult, ToolSpec
from arcagent.extension.catalog import BUNDLES_DIRNAME, resolve_extension_roots
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal
from arcagent.extension.grants import (
    BAD_NAME,
    NO_SUCH_CONNECTION,
    Connection,
    ConnectionRegistry,
)
from arcagent.extension.host import HostPrerequisiteDirector, HostVerdict
from arcagent.extension.host_install import host_install_dir, install_pinned_binary
from arcagent.extension.host_login import run_authorization_check, run_token_login
from arcagent.extension.manifest import (
    ArtifactPin,
    DeclaredTool,
    HostRequirement,
    SecretRequirement,
)
from arcagent.extension.oauth import build_authorize_url, exchange_authorization_code
from arcagent.extension.secrets import Secret, SecretRef, SecretStore, select_secret_backend
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors.install import (
    AttachmentFactory,
    ConnectorPlan,
    InstallReport,
    RemovalReport,
    build_attachment,
    connector_env_file,
    install_connector,
    placement_environment,
    plan_connector,
    remove_connector,
    resolve_secrets,
    shape_supplied,
)

#: Refusal code for a verb aimed at a connection this deployment has not made.
#: Re-exported from :mod:`arcagent.extension.grants` so a surface branches on one
#: code: "not installed" and "not defined" were one question the moment a
#: connection stopped being a block in someone's config file.
NOT_INSTALLED = NO_SUCH_CONNECTION

#: Refusal code for a config file that exists and will not parse. Loud rather
#: than defaulted: reading ``personal`` off a broken federal config would be the
#: silent downgrade this resolution exists to stop.
UNREADABLE_TIER = "CONNECTION_TIER_UNREADABLE"

#: Refusal code for a grant this deployment could not honour without silently
#: re-homing a credential. See :meth:`Connections.grant`.
TIER_WOULD_RISE = "CONNECTION_TIER_WOULD_RISE"

#: Refusal code for an install whose grants demand more stringency than the plan
#: was resolved at.
PLAN_TIER_TOO_LOW = "CONNECTION_PLAN_TIER_TOO_LOW"

#: The fleet-wide config every agent's own config merges over. What the rest of
#: the stack already reads — ``core/config.py`` composes it under every agent's
#: file, and ``arctui.roster`` enumerates ``<arc_team>/*/arcagent.toml``.
_FLEET_CONFIG = "arcagent.toml"

#: Stringency order. A connection two agents share is served at the strictest of
#: them, because a store that satisfies the laxest satisfies nobody else.
_STRINGENCY = (Tier.PERSONAL, Tier.ENTERPRISE, Tier.FEDERAL)

#: Refusal code for a credential no manifest declares — the allowlist that stops
#: a rotation from writing an arbitrary entry into the agent's credential file.
UNDECLARED_CREDENTIAL = "CONNECTOR_SECRET_UNDECLARED"


def _refuse(code: str, message: str, **details: Any) -> ExtensionError:
    """The one refusal shape every surface renders: a code to branch on, a line to show."""
    return ExtensionError(code=code, message=message, details=details)


def _check_agent(agent: str) -> None:
    """Refuse an agent name that could never match anything, where it was typed.

    A grant is matched against an agent's directory name, so a name the coordinate
    rule refuses is a grant that is written, listed, and effective for no one —
    the silent no-op this whole surface exists to remove.
    """
    if not is_coordinate(agent):
        raise _refuse(BAD_NAME, coordinate_refusal("agent name", agent), name=agent)


# ---------------------------------------------------------------------------
# The deployment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionWorld:
    """Everything every verb needs, resolved once for one deployment.

    There is no agent here, and that is the point. A connected account belongs to
    the deployment: its credential, its bundle, its approved contract and its
    grant list are one set of facts, not one set per agent. An agent enters this
    module only as a NAME in :attr:`~arcagent.extension.grants.Connection.agents`,
    which is matched against the agent's directory name when it starts.
    """

    arc_dir: Path
    data_dir: Path
    did: str
    tier: Tier
    extension_roots: tuple[Path, ...]
    env_file: Path
    egress_allow: tuple[str, ...] = ()

    @property
    def connections_file(self) -> Path:
        """Where this deployment's connections and grants live."""
        return ConnectionRegistry(self.arc_dir).path


@dataclass(frozen=True)
class ConnectorMutation:
    """Durable mutation plus the live-agent activation verdicts it produced."""

    activations: tuple[ConnectorReconcileResult, ...]
    connection: Connection | None = None
    removal: RemovalReport | None = None


#: Recorded as the actor when a deployment has no operator key to derive a DID
#: from. Honest rather than convenient: an unkeyed deployment cannot pin its
#: connector verdicts to a person, and saying so in the chain is better than
#: borrowing an agent's identity for an act the agent did not perform.
UNKEYED_OPERATOR_DID = "did:arc:operator:unkeyed"


def resolve_deployment(
    *,
    arc_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    extensions_root: Path | str | None = None,
    env_file: Path | str | None = None,
) -> ConnectionWorld:
    """Resolve one deployment's paths, tier, and operator identity.

    Args:
        arc_dir: Arc config dir (default ``arc_home()``) — where the connections,
            their credentials, and the operator key that pins bundle signatures
            and signs the audit chain all live.
        data_dir: Operational data dir (default arcstore's) — where the connection
            state and the audit chain live. Created when the caller names one.
        extensions_root: Use exactly this bundle root. An operator pointing a
            command at one directory gets that directory and no fallback behind it.
        env_file: Owner-only credential store (default ``<arc_dir>/connections.env``).

    Returns:
        The resolved deployment, ready to hand to :class:`Connections`.
    """
    root = _root(arc_dir)
    return ConnectionWorld(
        arc_dir=root,
        data_dir=_data_dir(data_dir),
        did=_operator_did(root),
        tier=deployment_tier(root),
        extension_roots=resolve_roots(root, extensions_root=extensions_root),
        env_file=(Path(env_file).expanduser().resolve() if env_file else connector_env_file(root)),
        egress_allow=deployment_egress_allow(root),
    )


def resolve_roots(
    arc_dir: Path | str | None = None, *, extensions_root: Path | str | None = None
) -> tuple[Path, ...]:
    """The ordered bundle search path, or exactly the one root the operator named."""
    if extensions_root:
        return (Path(extensions_root).expanduser().resolve(),)
    return resolve_extension_roots(Path(arc_dir) if arc_dir is not None else None)


def deployment_tier(arc_dir: Path | str | None = None) -> Tier:
    """The tier floor this deployment runs at.

    Read from the deployment's ``arcagent.toml`` (under the config root), the fleet-wide layer
    ``core/config.py`` already merges under every agent's own config — not a new
    setting. A deployment that has never been hardened answers ``personal``, which
    is the shipped default that file itself carries.

    Separate from :func:`resolve_deployment` because listing what could be
    connected needs the tier a manifest is parsed at and nothing else — a
    deployment that has connected nothing yet still has an answer.
    """
    return _tier_of(_read_toml(config_file(_FLEET_CONFIG, _root(arc_dir))))


def agent_tier(agent: str, arc_dir: Path | str | None = None) -> Tier:
    """One agent's effective tier, never below the deployment's floor.

    The agent's own ``<arc_team>/<agent>/arcagent.toml`` merges over the fleet
    file, which is exactly how that agent will run. The floor is then applied as a
    lower bound rather than a default: a per-agent block declaring ``personal``
    under a federal deployment is a downgrade of the deployment's own posture, and
    a shared account must not be the thing that grants it.
    """
    root = _root(arc_dir)
    own = _read_toml(arc_team(base=root) / agent / _FLEET_CONFIG)
    return _strictest([deployment_tier(root), _tier_of(own)])


def deployment_egress_allow(arc_dir: Path | str | None = None) -> tuple[str, ...]:
    """``[tools.policy] egress_allow`` at deployment scope (D-580).

    Read from the same fleet-wide file, consulted at enterprise tier only. Each
    agent still applies its own list when a verb is registered; this one decides
    whether the account may be connected at all.
    """
    raw = _read_toml(config_file(_FLEET_CONFIG, _root(arc_dir)))
    tools = raw.get("tools", {})
    policy = tools.get("policy", {}) if isinstance(tools, dict) else {}
    allow = policy.get("egress_allow", []) if isinstance(policy, dict) else []
    return tuple(str(name) for name in allow) if isinstance(allow, list) else ()


def _strictest(tiers: Sequence[Tier]) -> Tier:
    """The most stringent of several tiers — fail-closed when they disagree."""
    return max(tiers, key=_STRINGENCY.index) if tiers else Tier.PERSONAL


def _tier_of(raw: Mapping[str, Any]) -> Tier:
    security = raw.get("security", {})
    declared = security.get("tier") if isinstance(security, dict) else None
    return Tier(str(declared)) if declared else Tier.PERSONAL


def _root(arc_dir: Path | str | None) -> Path:
    from arctrust.paths import arc_home

    return Path(arc_dir).expanduser() if arc_dir else arc_home()


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse one config file, or answer empty when there is not one.

    Absent is the ordinary case — a deployment that has never been hardened, an
    agent that lives outside the fleet root — and is never an error. A file that
    exists and will not parse is, because silently reading ``personal`` off a
    broken federal config is the downgrade this whole resolution exists to stop.
    """
    if not path.is_file():
        return {}
    try:
        parsed: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _refuse(UNREADABLE_TIER, f"{path} — {exc}", path=str(path)) from exc
    return parsed


def _operator_key(arc_dir: Path) -> Any:
    """The deployment's operator key, or ``None`` when it has none.

    Read-only: never mints a key, so an install above personal tier on a machine
    with no operator key is refused by the loader rather than quietly satisfied by
    a keypair this call generated moments earlier (REQ-283).
    """
    from arctrust import OperatorKey

    try:
        return OperatorKey.load(default_operator_key_path(arc_dir), generate_if_absent=False)
    except (FileNotFoundError, OSError):
        return None


def _operator_did(arc_dir: Path) -> str:
    """The DID every connector verdict is recorded under.

    Derived from the operator key through the one authority
    :class:`~arctrust.policy.OperatorApprovalAuthority`, so a connector approval
    and an ``arc approve`` grant name the same operator. Connecting is an
    operator's act at every tier; recording it under an agent's DID would put the
    subject of the audit in the actor's place.
    """
    from arctrust.policy import OperatorApprovalAuthority

    key = _operator_key(arc_dir)
    if key is None:
        return UNKEYED_OPERATOR_DID
    return str(OperatorApprovalAuthority(key.into_signer()).did)


def _data_dir(given: Path | str | None) -> Path:
    """The operational data directory — arcstore's, unless the operator names one."""
    if given:
        path = Path(given).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    from arcstore import resolve_data_dir

    return Path(resolve_data_dir(None))


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
    #: True when the connector's manifest declares an ``[oauth]`` flow — the shape
    #: is "supply the app key/secret, then finish by pasting a consent code", not a
    #: host binary or a typed token. Set independently of ``authorize_url`` so a
    #: surface can tell an OAuth connector whose app key is not supplied yet (no URL
    #: to open) from one that is not OAuth at all.
    oauth: bool = False
    #: The provider consent URL for a native OAuth connector — empty for every
    #: other shape, and empty for an OAuth connector until its app key is supplied
    #: (there is no URL before the connector knows which app it is). It names no
    #: secret (only the public client id), so it is safe to render.
    authorize_url: str = ""

    @property
    def oauth_connect(self) -> bool:
        """True when the next step is opening the URL and pasting a consent code."""
        return bool(self.authorize_url)

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


async def _oauth_post(
    url: str, data: dict[str, str], auth: tuple[str, str]
) -> tuple[int, dict[str, Any]]:
    """POST form data with HTTP basic auth for the OAuth exchange — the one HTTP call.

    A plain function, not a method, so :meth:`Connections.complete_oauth` injects
    it exactly as the exchange's tests inject a fake: the framework owns the HTTP
    client, the pure exchange in :mod:`arcagent.extension.oauth` owns the protocol.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, data=data, auth=auth)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


def _authorization(
    instance: str,
    plan: ConnectorPlan,
    probe: DoctorCheck,
    sign_in: tuple[SignInState, str],
    credentials: tuple[SuppliedCredential, ...],
    *,
    note: str = "",
    authorize_url: str = "",
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
        oauth=plan.manifest.oauth is not None,
        authorize_url=authorize_url,
    )


class Connections:
    """A deployment's connected accounts: what could be connected, what is, and who holds it.

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
        state_opener: Callable[[], Awaitable[Any]] | None = None,
        connector_control: ConnectorControl | None = None,
    ) -> None:
        self._world = world
        self._audit = audit if audit is not None else AuditChain()
        self._factory: AttachmentFactory = attachment_factory or build_attachment
        self._install_dir = install_dir
        self._state_opener = state_opener
        self._connector_control = connector_control

    @classmethod
    def for_deployment(
        cls,
        *,
        arc_dir: Path | str | None = None,
        data_dir: Path | str | None = None,
        extensions_root: Path | str | None = None,
        env_file: Path | str | None = None,
        audit: AuditChain | None = None,
        attachment_factory: AttachmentFactory | None = None,
        install_dir: Path | None = None,
        state_opener: Callable[[], Awaitable[Any]] | None = None,
        connector_control: ConnectorControl | None = None,
    ) -> Connections:
        """Resolve a deployment and bind it to a chain in one step."""
        world = resolve_deployment(
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
            state_opener=state_opener,
            connector_control=connector_control,
        )

    @property
    def world(self) -> ConnectionWorld:
        """The deployment's resolved paths and identity — what a surface reports back."""
        return self._world

    @property
    def registry(self) -> ConnectionRegistry:
        """The deployment's connections and grants."""
        return ConnectionRegistry(self._world.arc_dir)

    # --- reading ---------------------------------------------------------

    def catalog(self) -> tuple[CatalogEntry, ...]:
        """What this deployment could connect, from its own search path."""
        with self._audit.open() as sink:
            return catalog(
                roots=self._world.extension_roots, tier=self._world.tier, audit_sink=sink
            )

    def connections(self) -> dict[str, Connection]:
        """Every connected account, and the agents each one is granted to.

        The whole of "who can reach what", answered in one read for every agent at
        once — which is the question no per-agent arrangement could answer.
        """
        return self.registry.all()

    def plan(self, extension: str, instance: str, *, agents: Sequence[str] = ()) -> ConnectorPlan:
        """The read-only half of an install: resolve the bundle, parse it, check the host.

        Nothing is written, so a surface can show an operator the credentials it is
        about to ask for and the approval mode it will run under before the first
        field is filled in.

        ``agents`` is who the connection is about to be granted to, and it decides
        the tier the manifest is parsed and the egress verdict taken at: an account
        a federal agent will use is a federal account from the first step, not from
        the moment somebody notices. Omitting it plans at the deployment's floor,
        and :meth:`install` refuses a plan that turns out to be too lax.

        Raises:
            ExtensionError: The name cannot be used, or the bundle was refused;
                ``.message`` names the step. The name check belongs to
                ``plan_connector`` — one rule on every path — so a surface refuses
                an unusable name here, before asking an operator for a credential.
        """
        with self._audit.open() as sink:
            return self._plan(extension, instance, sink, tier=self._tier_for(agents))

    def plan_for(self, instance: str) -> ConnectorPlan:
        """The plan behind an already-connected account.

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
            authorize_url = await self._oauth_authorize_url(plan, sink)
        return _authorization(
            instance, plan, probe, sign_in, supplied, authorize_url=authorize_url
        )

    async def complete_oauth(self, instance: str, *, code: str) -> Authorization:
        """Finish a native OAuth connection by swapping its code for a refresh token.

        The whole in-harness sign-in: read the operator-supplied app key/secret,
        exchange the one-time ``code`` the provider showed for a DURABLE refresh
        token (never a short-lived access token), store that under the manifest's
        ``refresh_token_secret``, and probe. The operator never obtains, types, or
        sees a refresh token, and Arc keeps only the refresh token — the access
        tokens the connection needs are minted from it on demand.

        Raises:
            ExtensionError: The connection is not an OAuth connector, its app
                key/secret have not been supplied yet, or the exchange failed
                (a terminal ``invalid_grant`` means the code is dead — authorize
                again).
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            flow = plan.manifest.oauth
            if flow is None:
                raise ExtensionError(
                    code="NOT_OAUTH",
                    message=(
                        f"{instance!r} is not an OAuth connector; supply its credentials instead"
                    ),
                    details={"instance": instance},
                )
            store = self._store(sink)
            client_id = await self._read_secret(store, instance, flow.client_id_secret)
            client_secret = await self._read_secret(store, instance, flow.client_secret_secret)
            if not client_id or not client_secret:
                raise ExtensionError(
                    code="OAUTH_CLIENT_MISSING",
                    message=(
                        f"supply {flow.client_id_secret} and {flow.client_secret_secret} for "
                        f"{instance!r} before authorizing"
                    ),
                    details={"instance": instance},
                )
            tokens = await exchange_authorization_code(
                flow,
                code=code.strip(),
                client_id=client_id,
                client_secret=client_secret,
                post=_oauth_post,
            )
            await store.put(
                SecretRef(connection=instance, field=flow.refresh_token_secret),
                tokens.refresh_token,
                caller_did=self._world.did,
            )
            probe = await self._reachability(plan, sink)
            sign_in = await self._sign_in_state(plan, sink)
            supplied = await self._supplied(plan, sink)
            authorize_url = await self._oauth_authorize_url(plan, sink)
        return _authorization(
            instance, plan, probe, sign_in, supplied, authorize_url=authorize_url
        )

    async def _oauth_authorize_url(self, plan: ConnectorPlan, sink: AuditSink) -> str:
        """The provider consent URL for a native OAuth connector, or empty.

        Built from the operator-supplied client id (never a secret), so a surface
        can render it. Empty until the app key is supplied — there is no URL to
        open before the connector knows which app it is.
        """
        flow = plan.manifest.oauth
        if flow is None:
            return ""
        store = self._store(sink)
        client_id = await self._read_secret(store, plan.instance, flow.client_id_secret)
        return build_authorize_url(flow, client_id=client_id) if client_id else ""

    async def _read_secret(self, store: SecretStore, instance: str, field: str) -> str:
        """One connector secret's value, or empty when nothing is stored yet."""
        found = await store.get(
            SecretRef(connection=instance, field=field), caller_did=self._world.did
        )
        return found.reveal() if found is not None else ""

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
            steps = "\n\n".join(required.instruction for required in plan.manifest.host_requires)
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
        checks = [required for required in plan.manifest.host_requires if required.verify_command]
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
        """Run the one login Arc can finish, or say plainly why it did not run one.

        A login needing more than a token — ``acli`` needs the site and the address
        on its own argv — is filled in from the fields the operator already supplied.
        Only the non-sensitive ones are readable at all (:meth:`_supplied` never
        reads a credential out of the store), so the rule that a credential crosses
        on stdin and nowhere else holds here by construction rather than by care.
        """
        accepting = next(
            (required for required in plan.manifest.host_requires if required.token_command),
            None,
        )
        if accepting is None:
            return f"{plan.extension} signs in on this host; Arc ran nothing."
        if not token:
            return f"{accepting.name} signs in with a token — supply one and Arc will run it."
        result = await run_token_login(
            accepting,
            token=token,
            caller_did=self._world.did,
            audit_sink=sink,
            tier=self._world.tier,
            values={field.name: field.value for field in await self._supplied(plan, sink)},
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

    async def install(
        self, plan: ConnectorPlan, secrets: Mapping[str, str], *, agents: Sequence[str] = ()
    ) -> InstallReport:
        """Connect one account, and hand it to the agents that should have it.

        The ordering, the signature gate that closes before extension code runs, and
        the rollback that takes a credential back out when the probe fails all
        belong to :func:`~arcagent.modules.connectors.install.install_connector`.

        Args:
            plan: What :meth:`plan` resolved.
            secrets: One value per declared credential. Consumed and dropped — the
                report names fields, never values.
            agents: The agents granted this connection. Granting here rather than
                in a second step is what makes one pass enough for a
                non-technical operator; an empty list is legal and means an
                account that is proven to work and reaches nobody yet.

        Raises:
            ExtensionError: A step refused. ``details["step"]`` names which, and
                nothing was left behind. An agent name that cannot address
                anything is refused BEFORE the install runs (``code``
                :data:`~arcagent.extension.grants.BAD_NAME`), so a typo costs a
                refusal rather than a connected account nobody can use.
        """
        for agent in agents:
            _check_agent(agent)
        self._refuse_lax_plan(plan, agents)
        with self._audit.open() as sink:
            return await install_connector(
                plan,
                connections=self.registry,
                agents=agents,
                secret_values=secrets,
                store=self._store(sink),
                caller_did=self._world.did,
                state=await self._connection_state(),
                attachment_factory=self._factory,
                audit_sink=sink,
                trusted_public_key=self._pinned_key(),
            )

    def grant(self, instance: str, agents: Sequence[str]) -> Connection:
        """Permit ``agents`` to use one connected account.

        Nothing is copied: the credential stays at its one coordinate and the
        grantee reads it through the same store every other grantee does. So a
        rotation is one write, and :meth:`revoke` cannot leave a credential behind.

        Takes effect at the granted agent's next start — the connector module
        reads its grants when it attaches, which is the same startup-only binding
        every other connector change has.

        A grant that would raise this connection's stringency past what its
        credential's current store can satisfy is REFUSED rather than honoured
        (see :meth:`_refuse_silent_rehome`). Nothing is re-homed behind an
        operator's back, and nothing claims a posture it does not have.

        Raises:
            ExtensionError: No such connection (``code`` :data:`NOT_INSTALLED`), an
                agent name that cannot address anything, or a grant that would
                raise the tier past this deployment's store
                (``code`` :data:`TIER_WOULD_RISE`).
        """
        with self._audit.open() as sink:
            current = self.registry.get(instance)
            for agent in agents:
                _check_agent(agent)
            self._refuse_silent_rehome(instance, [*current.agents, *agents], sink)
            granted = self.registry.grant(instance, agents)
            self._record(sink, "connector.grant", instance, agents)
        return granted

    def revoke(self, instance: str, agents: Sequence[str]) -> Connection:
        """Take one connected account back from ``agents``. The account is untouched.

        Revoking from an agent that never held it is not an error: an operator
        making sure nobody has something must not be stopped by a name that
        already does not.

        Raises:
            ExtensionError: No such connection (``code`` :data:`NOT_INSTALLED`).
        """
        with self._audit.open() as sink:
            remaining = self.registry.revoke(instance, agents)
            self._record(sink, "connector.revoke", instance, agents)
        return remaining

    async def grant_and_reconcile(self, instance: str, agents: Sequence[str]) -> ConnectorMutation:
        """Persist a grant then durably refresh every affected running agent."""
        connection = self.grant(instance, agents)
        return ConnectorMutation(
            connection=connection, activations=await self._reconcile_agents(agents)
        )

    async def revoke_and_reconcile(
        self, instance: str, agents: Sequence[str]
    ) -> ConnectorMutation:
        """Persist a revocation then remove its tools from live affected agents."""
        connection = self.revoke(instance, agents)
        return ConnectorMutation(
            connection=connection, activations=await self._reconcile_agents(agents)
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
                ref = SecretRef(connection=plan.instance, field=required.name)
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
                await self._connection_state(), connection=instance, sink=sink
            )
            await ledger.approve(specs, actor_did=self._world.did)
        return tuple(spec.name for spec in specs)

    async def remove(self, instance: str) -> RemovalReport:
        """Disconnect one account: its credential, its definition, every grant on it.

        Removing something that was never connected is reported, not raised: an
        operator cleaning up after a failed install must not be blocked by a step
        that already has nothing to do.
        """
        with self._audit.open() as sink:
            return await remove_connector(
                connections=self.registry,
                instance=instance,
                store=self._store(sink),
                caller_did=self._world.did,
                secret_fields=self._declared_secret_fields(instance, sink),
                state=await self._connection_state(),
            )

    async def remove_and_reconcile(self, instance: str) -> ConnectorMutation:
        """Remove an account and refresh every agent that held its tools."""
        try:
            agents = self.registry.get(instance).agents
        except ExtensionError as exc:
            if exc.code != NOT_INSTALLED:
                raise
            agents = ()
        removal = await self.remove(instance)
        return ConnectorMutation(removal=removal, activations=await self._reconcile_agents(agents))

    async def _reconcile_agents(
        self, agents: Sequence[str]
    ) -> tuple[ConnectorReconcileResult, ...]:
        """Queue every projection then use this process as an optional fast path."""
        commands = await self._reconcile_queue().enqueue(agents)
        outcomes: list[ConnectorReconcileResult] = []
        queue = self._reconcile_queue()
        for command in commands:
            agent = command.agent
            try:
                result = (
                    await self._connector_control.reconcile(agent)
                    if self._connector_control is not None
                    else None
                )
            except Exception as exc:
                outcomes.append(
                    ConnectorReconcileResult(
                        status="activation_pending",
                        agent=agent,
                        revision=command.revision,
                        detail=f"reconcile retry pending: {exc}",
                    )
                )
                continue
            if result is None:
                outcomes.append(
                    ConnectorReconcileResult(
                    status="activation_pending",
                    agent=agent,
                    revision=command.revision,
                    detail="agent is not running in this process",
                )
                )
                continue
            outcome = replace(result, agent=agent, revision=command.revision)
            if outcome.status == "applied":
                await queue.acknowledge(command, outcome)
            outcomes.append(outcome)
        return tuple(outcomes)

    def _reconcile_queue(self) -> ConnectorReconcileQueue:
        return ConnectorReconcileQueue(self._open_reconcile_backend, actor_did=self._world.did)

    async def _open_reconcile_backend(self) -> Any:
        if self._state_opener is not None:
            return await self._state_opener()
        from arcstore.backends import open_backend

        backend = open_backend()
        await backend.start()
        return backend

    def _refuse_lax_plan(self, plan: ConnectorPlan, agents: Sequence[str]) -> None:
        """Refuse an install whose grantees need more stringency than the plan took.

        The plan carries the tier its manifest was parsed at and its egress verdict
        taken at, so installing it for a stricter agent than it was resolved for
        would apply a personal deployment's verdicts to a federal agent's account.
        The surface re-plans; it does not get to keep the lax answer.
        """
        required = self._tier_for(agents)
        if _STRINGENCY.index(required) <= _STRINGENCY.index(plan.tier):
            return
        raise _refuse(
            PLAN_TIER_TOO_LOW,
            f"{plan.instance!r} would be granted to an agent running at "
            f"{required.value}, but it was planned at {plan.tier.value} — "
            f"plan it again for these agents",
            connection=plan.instance,
            planned_tier=plan.tier.value,
            required_tier=required.value,
        )

    def _refuse_silent_rehome(self, instance: str, agents: Sequence[str], sink: AuditSink) -> None:
        """Refuse a grant that would raise the tier past the store holding the credential.

        Granting a federal agent an account whose token sits in this host's
        ``connections.env`` does not move the token. Honouring it would leave a
        connection reporting federal stringency with its credential in a local
        file — a control reporting a posture it does not have, which is the exact
        defect class this feature has already shipped. Re-homing it silently is
        the other half of that hazard, so neither happens: the operator is told
        what to configure and re-runs the connect.

        A connection Arc stores no credential for is untouched by this: a bundle
        whose binary owns its own token has nothing in any store to be in the
        wrong one.
        """
        required = self._tier_for(agents)
        if required is self._world.tier:
            return
        if not self._declared_secret_fields(instance, sink):
            return
        try:
            select_secret_backend(required, env_file=self._world.env_file)
        except ExtensionError as exc:
            raise _refuse(
                TIER_WOULD_RISE,
                f"granting {instance!r} to these agents raises it to {required.value}, "
                f"and this deployment cannot hold its credential at that tier: "
                f"{exc.message}. Configure that store, then connect {instance!r} again.",
                connection=instance,
                required_tier=required.value,
            ) from exc

    def _record(self, sink: AuditSink, action: str, instance: str, agents: Sequence[str]) -> None:
        """Record a change to who may reach an account (AU-2).

        An access-control decision that is not in the chain is not auditable, and
        this one names the connection and the agents rather than only the fact
        that something changed — an auditor reconstructing who could read the mail
        on a given day needs both.
        """
        emit(
            AuditEvent(
                actor_did=self._world.did,
                action=action,
                target=f"connector:{instance}",
                outcome="allow",
                tier=self._world.tier.value,
                extra={"connection": instance, "agents": ",".join(agents)},
            ),
            sink,
        )

    # --- internals -------------------------------------------------------

    async def _connection_state(self) -> ConnectionStateStore:
        """The connection directory this agent's records and approvals live in.

        Opened per verb rather than held: the same reason the audit chain is, and
        the same directory every surface resolves — a second spelling of the data
        dir would mean the agent reads a store no surface ever wrote to.
        """
        return await open_connection_state(opener=self._state_opener)

    def _plan(
        self, extension: str, instance: str, sink: AuditSink, *, tier: Tier | None = None
    ) -> ConnectorPlan:
        """Plan against an already-open chain, so no verb opens a second ``flock``."""
        return plan_connector(
            extensions_root=self._world.extension_roots,
            extension=extension,
            instance=instance,
            tier=tier if tier is not None else self._world.tier,
            audit_sink=sink,
            egress_allow=self._world.egress_allow,
        )

    def _plan_for(self, instance: str, sink: AuditSink) -> ConnectorPlan:
        """Plan an existing connection at the tier its own grantees require."""
        connection = self.registry.get(instance)
        return self._plan(
            connection.extension, instance, sink, tier=self._tier_for(connection.agents)
        )

    def _tier_for(self, agents: Sequence[str]) -> Tier:
        """The stringency one connection must be served at, given who holds it.

        The strictest of the deployment floor and every grantee. A connection is
        one account with one credential in one store, so the store has to satisfy
        the strictest agent that can reach it — satisfying the laxest satisfies
        nobody else.
        """
        return _strictest(
            [self._world.tier, *(agent_tier(agent, self._world.arc_dir) for agent in agents)]
        )

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
            connection=plan.instance,
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
        # An OAuth connector's refresh token is WRITTEN by ``complete_oauth``, never
        # typed — the operator supplies only the app key/secret. Listing it as a
        # field to fill would send them looking for a value they can't get by hand,
        # which is the exact confusion the OAuth flow exists to remove.
        managed = plan.manifest.oauth.refresh_token_secret if plan.manifest.oauth else None
        rows: list[SuppliedCredential] = []
        for declared in plan.secrets:
            if declared.name == managed:
                continue
            value = ""
            if not declared.sensitive:
                ref = SecretRef(connection=plan.instance, field=declared.name)
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
                connection=plan.instance,
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
        """The operator key an extension bundle's signatures are pinned to (REQ-283)."""
        key = _operator_key(self._world.arc_dir)
        return None if key is None else bytes(key.public_key)

    async def _credential_checks(
        self, plan: ConnectorPlan, instance: str, sink: AuditSink
    ) -> list[DoctorCheck]:
        """One row per declared credential: present or missing, never the value."""
        store = self._store(sink)
        rows: list[DoctorCheck] = []
        for required in plan.secrets:
            ref = SecretRef(connection=instance, field=required.name)
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
    "BAD_NAME",
    "BUNDLES_DIRNAME",
    "NOT_INSTALLED",
    "PLAN_TIER_TOO_LOW",
    "TIER_WOULD_RISE",
    "UNDECLARED_CREDENTIAL",
    "UNKEYED_OPERATOR_DID",
    "UNREADABLE_TIER",
    "ArtifactPin",
    "AttachmentFactory",
    "AuditChain",
    "Authorization",
    "CatalogEntry",
    "ClosableSink",
    "Connection",
    "ConnectionRegistry",
    "ConnectionWorld",
    "Connections",
    "ConnectorControl",
    "ConnectorMutation",
    "ConnectorPlan",
    "ConnectorReconcileResult",
    "DeclaredTool",
    "DoctorCheck",
    "ExtensionError",
    "HostAuthorization",
    "HostPrerequisiteDirector",
    "HostRequirement",
    "HostSetupReport",
    "HostVerdict",
    "InstallReport",
    "ProbeResult",
    "RemovalReport",
    "SecretRequirement",
    "SignInState",
    "SuppliedCredential",
    "Tier",
    "ToolSpec",
    "agent_tier",
    "catalog",
    "deployment_egress_allow",
    "deployment_tier",
    "resolve_deployment",
    "resolve_roots",
]
