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

import asyncio
import functools
import logging
import os
import shutil
import time
import tomllib
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from arctrust import causal
from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from arctrust.paths import arc_team, config_file
from arctrust.signer import Signer

from arcagent.connection_catalog import AuditChain, CatalogEntry, ClosableSink, catalog
from arcagent.connector_control import ConnectorControl, ConnectorReconcileResult
from arcagent.connector_reconcile import ConnectorReconcileQueue
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import (
    ExtensionAttachment,
    ProbeResult,
    ToolOutcome,
    ToolSpec,
)
from arcagent.extension.catalog import BUNDLES_DIRNAME, MANIFEST_NAME, resolve_extension_roots
from arcagent.extension.connection_health import (
    AUTH_REASONS,
    ConnectionHealthAuthority,
    HealthSignal,
    SignalSource,
    StoreHealthReporter,
    classify,
    custody_of,
    effective_probe,
)
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal
from arcagent.extension.credential_broker import AccessTokenHandle, credential_plan
from arcagent.extension.custody import CredentialCipher
from arcagent.extension.custody_migrate import (
    MigrationReport,
    ResealReport,
    legacy_env_path,
    migrate_connector_secrets,
    reseal_connector_secrets,
)
from arcagent.extension.custody_select import (
    VAULT_REQUIRED,
    Custody,
    deployment_cipher,
    open_custody,
    reseal_source_cipher,
)
from arcagent.extension.grants import (
    BAD_NAME,
    NO_SUCH_CONNECTION,
    Connection,
    ConnectionRegistry,
)
from arcagent.extension.host import HostPrerequisiteDirector, HostVerdict
from arcagent.extension.host_install import host_install_dir, install_pinned_binary
from arcagent.extension.host_login import (
    run_authorization_check,
    run_remote_login_begin,
    run_remote_login_complete,
    run_token_login,
)
from arcagent.extension.manifest import (
    ArtifactPin,
    DeclaredTool,
    HostRequirement,
    OAuthFlow,
    SecretRequirement,
    load_manifest,
)
from arcagent.extension.oauth import (
    OAuthTokens,
    build_authorize_url,
    exchange_authorization_code,
    post_form,
)
from arcagent.extension.remote_login import (
    REMOTE_LOGIN_FAILED,
    REMOTE_LOGIN_NEEDS_CONFIRMATION,
    PendingLogin,
    RemoteLoginLedger,
    checked_account,
)
from arcagent.extension.secrets import Secret, SecretRef, SecretStore
from arcagent.extension.state import (
    ConnectionRecord,
    ConnectionStateStore,
    open_connection_state,
)
from arcagent.modules.connectors.install import (
    AttachmentFactory,
    ConnectorPlan,
    InstallReport,
    RemovalReport,
    build_attachment,
    install_connector,
    placement_environment,
    plan_connector,
    remove_connector,
    resolve_secrets,
    shape_supplied,
)
from arcagent.modules.connectors.mcp_bundle import (
    DiscoveredTool,
    McpServerSpec,
    discover_tools,
    is_generated_bundle,
    require_exactly,
    sign_bundle,
    spec_digest,
    validate_spec,
    write_bundle,
)
from arcagent.utils.causality import correlate

_logger = logging.getLogger("arcagent.connections")

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


_Method = TypeVar("_Method", bound=Callable[..., Awaitable[Any]])


def _on_connection(method: _Method) -> _Method:
    """Run a per-connection operation with ``connection_id`` on its causal context.

    Item 20: whoever caused the act (a UI session, the CLI operator, the
    scheduler) stays the initiator; the connection it touched is refined in, so
    every secret read and host check inside names the account. Unbound, the act
    is recorded as unattributed rather than borrowing anyone's identity.
    """

    @functools.wraps(method)
    async def scoped(self: Any, instance: str, *args: Any, **kwargs: Any) -> Any:
        with correlate(fallback=("system", causal.UNATTRIBUTED), connection_id=instance):
            return await method(self, instance, *args, **kwargs)

    return cast(_Method, scoped)


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

    ``did`` is the deployment's operator authority. It is never recorded as the
    actor of an audited act: who caused a read, probe or change is the bound
    :mod:`arctrust.causal` initiator (a UI session, the scheduler, an agent, the
    CLI operator). Authority and actor are different facts (item 20).
    """

    arc_dir: Path
    data_dir: Path
    did: str
    tier: Tier
    extension_roots: tuple[Path, ...]
    egress_allow: tuple[str, ...] = ()
    #: ``[tools.policy] mcp_stdio_allow`` — the programs an operator-added MCP server
    #: may launch above personal tier.
    mcp_stdio_allow: tuple[str, ...] = ()
    #: True when the caller named exactly one bundle root, so a generated bundle goes
    #: there rather than into ``<arc_dir>/extensions``.
    extensions_override: bool = False

    @property
    def connections_file(self) -> Path:
        """Where this deployment's connections and grants live."""
        return ConnectionRegistry(self.arc_dir).path

    @property
    def credential_location(self) -> str:
        """Where connector credentials live, in words a surface can print."""
        return CREDENTIAL_LOCATION


@dataclass(frozen=True)
class ConnectorMutation:
    """Durable mutation plus the live-agent activation verdicts it produced."""

    activations: tuple[ConnectorReconcileResult, ...]
    connection: Connection | None = None
    removal: RemovalReport | None = None


@dataclass(frozen=True)
class McpServerAdded:
    """What adding an MCP server produced. Names and a digest, never a credential."""

    report: InstallReport
    spec_sha256: str
    bundle: Path
    signed_by: str
    #: The namespaced tools the operator chose AND the server offered. ``report.tools``
    #: is everything the probe saw, which is the server's list and not the operator's.
    exposed: tuple[str, ...] = ()


#: Where every connector credential lives since P18-2: sealed rows, never a file.
CREDENTIAL_LOCATION = "sealed Arc custody (arcstore, encrypted with the operator key)"


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
) -> ConnectionWorld:
    """Resolve one deployment's paths, tier, and operator identity.

    Args:
        arc_dir: Deployment root (default ``operator_root()``) — where the
            connections, their credentials, and the operator key that pins
            bundle signatures and signs the audit chain all live.
        data_dir: Operational data dir (default arcstore's) — where the connection
            state and the audit chain live. Created when the caller names one.
        extensions_root: Use exactly this bundle root. An operator pointing a
            command at one directory gets that directory and no fallback behind it.

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
        egress_allow=deployment_egress_allow(root),
        mcp_stdio_allow=deployment_mcp_stdio_allow(root),
        extensions_override=extensions_root is not None,
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


def deployment_mcp_stdio_allow(arc_dir: Path | str | None = None) -> tuple[str, ...]:
    """``[tools.policy] mcp_stdio_allow`` at deployment scope.

    The programs an operator-added stdio MCP server may launch above personal tier.
    Empty by default: above personal, no arbitrary binary becomes a server.
    """
    raw = _read_toml(config_file(_FLEET_CONFIG, _root(arc_dir)))
    tools = raw.get("tools", {})
    policy = tools.get("policy", {}) if isinstance(tools, dict) else {}
    allow = policy.get("mcp_stdio_allow", []) if isinstance(policy, dict) else []
    return tuple(str(name) for name in allow) if isinstance(allow, list) else ()


def _strictest(tiers: Sequence[Tier]) -> Tier:
    """The most stringent of several tiers — fail-closed when they disagree."""
    return max(tiers, key=_STRINGENCY.index) if tiers else Tier.PERSONAL


def _tier_of(raw: Mapping[str, Any]) -> Tier:
    security = raw.get("security", {})
    declared = security.get("tier") if isinstance(security, dict) else None
    return Tier(str(declared)) if declared else Tier.PERSONAL


def _root(arc_dir: Path | str | None) -> Path:
    """The deployment root every connection path is resolved against.

    The OPERATOR root, not the install home. Everything reached from here is
    config or state — ``connections.toml``, the operator
    key, installed extensions — and all of it moved beside the fleet so an
    update can replace the install without touching it. Answering ``arc_home()``
    left the registry reading ``~/.arc/config/connections.toml`` after the
    migration had moved the real one, so a deployment with five live connections
    reported none.
    """
    from arctrust.paths import operator_root

    return Path(arc_dir).expanduser() if arc_dir else operator_root()


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


def _operator_signer(arc_dir: Path) -> Any:
    """The deployment's operator signer, or ``None`` when it has none.

    Read-only: never mints a key, so an install above personal tier on a machine
    with no operator key is refused by the loader rather than quietly satisfied by
    a keypair this call generated moments earlier (REQ-283). Resolved through the
    one arctrust resolver so a vault-held key answers the same as an on-disk one.
    """
    from arctrust import SignerError, operator_signer_for

    try:
        return operator_signer_for(base=arc_dir)
    except (OSError, SignerError):  # no key file, or a transit that cannot serve one
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

    signer = _operator_signer(arc_dir)
    if signer is None:
        return UNKEYED_OPERATOR_DID
    return str(OperatorApprovalAuthority(signer).did)


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
    #: True when the manifest declares a two-step remote sign-in Arc can drive
    #: from a browser, so a surface offers that instead of a terminal command.
    remote_login: bool = False


@dataclass(frozen=True)
class RemoteLoginStart:
    """A begun remote sign-in: the link to open, and how long it stays completable.

    ``consent_url`` is the provider's own consent page, checked to be on the host
    the manifest declares. It carries only public values (the client id, a CSRF
    ``state``, a PKCE challenge), never a credential.
    """

    instance: str
    account: str
    consent_url: str
    expires_in: int
    #: Blank-field warnings the operator accepted to start this sign-in.
    warnings: tuple[str, ...] = ()


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


#: Whether this connection's account is actually connected. More than two states,
#: because each collapse is a lie with a cost: "Arc cannot tell" (``unknown``)
#: drawn as signed in is the defect that shipped, and drawn as signed out sends an
#: operator to redo a login already done. ``expired`` is a credential the provider
#: stopped honouring — a *Reconnect*, not a first sign-in — and ``not_installed``
#: is a binary that is not on this host, which no sign-in can fix.
SignInState = Literal["signed_in", "signed_out", "expired", "not_installed", "unknown"]


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
    #: The manifest's own word on the field's shape, so a form can draw a choice
    #: as a choice and say what blank means. Never a value that was stored.
    required: bool = True
    choices: tuple[str, ...] = ()
    default: str = ""
    #: The bundle's warning for this field, set only while it is blank.
    warning: str = ""


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
        token-accepting or browser-finishable connector must NOT produce one:
        sending an operator to a terminal for a sign-in the button would have
        completed is the same lie told in the other direction.
        """
        for host in self.hosts:
            if not host.token_command and not host.remote_login:
                return host.command
        return ""

    @property
    def remote_login(self) -> bool:
        """True when the next step is the browser sign-in Arc drives in two steps."""
        return any(host.remote_login for host in self.hosts)


def _optional_fields(plan: ConnectorPlan) -> frozenset[str]:
    """The fields the bundle declares optional — the only ones a step may leave blank."""
    return frozenset(declared.name for declared in plan.secrets if not declared.required)


def _visible_placements(plan: ConnectorPlan) -> frozenset[str]:
    """The placed variables that carry no credential, by the manifest's own word.

    An account address placed into ``GOG_ACCOUNT`` is configuration; redacting it
    from what the binary prints turns "authorized as X, expected Y" into a line
    that no longer says which account was expected.
    """
    return frozenset(
        declared.placement.variable
        for declared in plan.secrets
        if not declared.sensitive and declared.placement is not None
    )


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
                remote_login=required.remote_login is not None,
            )
            for required in plan.manifest.host_requires
            if required.authorize_command or required.remote_login is not None
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
        remote_logins: RemoteLoginLedger | None = None,
        host_step_timeout: float | None = None,
        clock: Callable[[], datetime] | None = None,
        credential_cipher: CredentialCipher | None = None,
    ) -> None:
        self._world = world
        # The cipher sealing connector credentials. Resolved from the operator key
        # on first use when not injected (a test, or a caller that already holds it).
        self._credential_cipher = credential_cipher
        self._backend: Any = None
        self._audit = audit if audit is not None else AuditChain()
        self._factory: AttachmentFactory = attachment_factory or build_attachment
        self._install_dir = install_dir
        self._state_opener = state_opener
        self._connector_control = connector_control
        # A begun sign-in lives as long as the process that can complete it, so the
        # ledger belongs to the long-lived caller; a fresh one here serves a
        # one-shot surface (a CLI verb) whose begin and complete are one process.
        self._remote_logins = remote_logins if remote_logins is not None else RemoteLoginLedger()
        self._host_step_timeout = host_step_timeout
        # The time a health check is stamped with. Injectable so a test can walk the
        # ten-minute and 24-hour escalation bounds without waiting for them.
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    @classmethod
    def for_deployment(
        cls,
        *,
        arc_dir: Path | str | None = None,
        data_dir: Path | str | None = None,
        extensions_root: Path | str | None = None,
        audit: AuditChain | None = None,
        attachment_factory: AttachmentFactory | None = None,
        install_dir: Path | None = None,
        state_opener: Callable[[], Awaitable[Any]] | None = None,
        connector_control: ConnectorControl | None = None,
        remote_logins: RemoteLoginLedger | None = None,
        host_step_timeout: float | None = None,
        clock: Callable[[], datetime] | None = None,
        credential_cipher: CredentialCipher | None = None,
    ) -> Connections:
        """Resolve a deployment and bind it to a chain in one step."""
        world = resolve_deployment(
            arc_dir=arc_dir,
            data_dir=data_dir,
            extensions_root=extensions_root,
        )
        return cls(
            world,
            audit=audit,
            attachment_factory=attachment_factory,
            install_dir=install_dir,
            state_opener=state_opener,
            connector_control=connector_control,
            remote_logins=remote_logins,
            host_step_timeout=host_step_timeout,
            clock=clock,
            credential_cipher=credential_cipher,
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

    @_on_connection
    async def tools(self, instance: str) -> tuple[ToolSpec, ...]:
        """The verbs one connection offers the agent right now."""
        with self._audit.open() as sink:
            attachment = await self._attachment(self._plan_for(instance, sink), sink)
            return tuple(await attachment.describe_tools())

    @_on_connection
    async def probe(self, instance: str) -> ProbeResult:
        """Open the connection right now — the only honest answer to "does this work"."""
        with self._audit.open() as sink:
            attachment = await self._attachment(self._plan_for(instance, sink), sink)
            return await attachment.probe()

    @_on_connection
    async def check_health(
        self,
        instance: str,
        *,
        checked_by: str,
        source: Literal["probe", "operator"] = "probe",
        timeout: float = 20.0,
    ) -> ConnectionRecord:
        """Run the connection's declared ``[health]`` probe and record what it found.

        The one writer of "does this connection work": the scheduled probe loop and
        an operator's "Check now" both come here, so a card, a notice and a CLI row
        cannot disagree. The record is the answer; nothing is returned that a page
        view could not read from it afterwards.

        A bundle declares its probe in ``[health]``; one that does not gets the
        honest default for its shape (:func:`effective_probe`), and a bare CLI with
        no sign-in check is left unchecked rather than guessed healthy — the lie
        this packet exists to remove.

        Raises:
            ExtensionError: Nothing is connected under that name.
        """
        started = time.monotonic()
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            state = await self._connection_state()
            authority = ConnectionHealthAuthority(state, sink=sink)
            await self._ensure_record(state, plan, checked_by)
            health = effective_probe(plan.manifest)
            if health is None:
                return await self._record_of(authority, instance)
            generation = await self._credential_generation(instance)
            skipped = await self._skip_dead_credential(authority, instance, generation)
            if skipped:
                authority.audit_checked(
                    instance,
                    checked_by=checked_by,
                    ok=False,
                    reason_code=None,
                    duration_ms=0,
                    probe=health.probe,
                    skipped="credential_unchanged",
                )
                return await self._record_of(authority, instance)
            signal = await self._probe_signal(
                plan, sink, checked_by=checked_by, source=source, timeout=timeout
            )
            signal = replace(signal, credential_generation=generation)
            await authority.record(instance, signal, now=self._clock())
            authority.audit_checked(
                instance,
                checked_by=checked_by,
                ok=signal.ok,
                reason_code=signal.reason_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                probe=health.probe,
            )
            return await self._record_of(authority, instance)

    async def health_records(self) -> dict[str, ConnectionRecord]:
        """Every connection's stored health record, read once and read-only.

        What a listing surface shows beside each connection. Reads the shared record
        and nothing else: no credential, no provider, no process.
        """
        state = await self._connection_state()
        return {record.connection: record for record in await state.list()}

    async def ensure_health_records(self) -> None:
        """Give every defined connection a health record, so the probe loop sees it.

        A connection defined in config with no record (a wiped store, a row from
        before this existed) would otherwise sit at "Not checked yet" forever,
        because the loop schedules from the records it can list.
        """
        state = await self._connection_state()
        have = {record.connection for record in await state.list()}
        missing = [instance for instance in self.registry.all() if instance not in have]
        if not missing:
            # The common tick plans nothing: planning a bundle writes an audit row,
            # and one per connection per minute would be its own flood.
            return
        with self._audit.open() as sink:
            for instance in missing:
                try:
                    plan = self._plan_for(instance, sink)
                except ExtensionError:
                    continue
                await self._ensure_record(state, plan, causal.actor_did())

    async def _operator_check(self, instance: str) -> None:
        """Look at a connection once after an operator verb that should have fixed it.

        Never fails the verb: the operator's act succeeded, and the check is what
        turns the card truthful afterwards. A check that cannot run is logged and
        the next scheduled probe picks the connection up.
        """
        try:
            await self.check_health(instance, checked_by=causal.actor_did(), source="operator")
        except Exception:  # reason: bookkeeping after a verb that already succeeded
            _logger.warning("health check after operator verb failed: %s", instance, exc_info=True)

    @staticmethod
    async def _ensure_record(
        state: ConnectionStateStore, plan: ConnectorPlan, actor_did: str
    ) -> None:
        """Insert-if-absent the record, and keep ``custody`` in step with the manifest."""
        custody = custody_of(plan.manifest)
        record = await state.create(
            ConnectionRecord(connection=plan.instance, custody=custody), actor_did=actor_did
        )
        if record.custody != custody:
            await state.set_custody(plan.instance, custody, actor_did=actor_did)

    @staticmethod
    async def _record_of(authority: ConnectionHealthAuthority, instance: str) -> ConnectionRecord:
        record = await authority.get(instance)
        if record is None:
            raise _refuse(
                NOT_INSTALLED, f"{instance!r} has no connection record", instance=instance
            )
        return record

    async def _credential_generation(self, instance: str) -> int | None:
        """The custody generation the credential is at, or None when Arc holds none.

        A dead Arc-held credential is not probed again until this changes (P18-1 D8):
        the operator reconnecting bumps it, and nothing else does.
        """
        try:
            custody = await self._custody(NullSink())
        except ExtensionError:
            return None
        return await custody.rows.generation(instance)

    @staticmethod
    async def _skip_dead_credential(
        authority: ConnectionHealthAuthority, instance: str, generation: int | None
    ) -> bool:
        """True when a dead Arc-held credential is unchanged, so a probe would only hammer it.

        A refresh token the provider already rejected is not made live by asking
        again; only a new credential generation (the operator reconnecting) is worth
        a provider call. A host-held credential is always probed, because signing in
        out of band is its only way back.
        """
        record = await authority.get(instance)
        return (
            record is not None
            and record.custody == "arc"
            and record.status == "needs_you"
            and record.reason_code in AUTH_REASONS
            and record.credential_generation is not None
            and record.credential_generation == generation
        )

    async def _probe_signal(
        self,
        plan: ConnectorPlan,
        sink: AuditSink,
        *,
        checked_by: str,
        source: SignalSource,
        timeout: float,
    ) -> HealthSignal:
        """Run the declared probe and turn what happened into one signal."""
        provider = plan.manifest.extension.label

        def failure(code: str | None, detail: str) -> HealthSignal:
            return HealthSignal(
                ok=False,
                source=source,
                checked_by=checked_by,
                reason_code=classify(code, detail),
                detail=detail,
                provider=provider,
            )

        if plan.unsatisfied_host:
            missing = HealthSignal(
                ok=False,
                source=source,
                checked_by=checked_by,
                reason_code="host_missing",
                detail=plan.unsatisfied_host[0].name,
                provider=provider,
            )
            return missing
        health = effective_probe(plan.manifest)
        if health is None or health.mode == "none":
            return HealthSignal(ok=True, source=source, checked_by=checked_by, provider=provider)
        try:
            async with asyncio.timeout(timeout):
                verdict = await self._run_probe(plan, sink)
        except TimeoutError:
            return failure("provider_unavailable", f"did not answer within {timeout:g} s")
        except ExtensionError as exc:
            missing_credential = exc.details.get("step") == "secrets"
            return failure("credential_missing" if missing_credential else exc.code, exc.message)
        if verdict is None:
            return HealthSignal(ok=True, source=source, checked_by=checked_by, provider=provider)
        code, detail = verdict
        return failure(code, detail)

    async def _run_probe(
        self, plan: ConnectorPlan, sink: AuditSink
    ) -> tuple[str | None, str] | None:
        """``None`` when the probe passed, else ``(reason code or None, provider text)``."""
        health = effective_probe(plan.manifest)
        if health is None:
            return None
        if health.probe == "host_verify":
            return await self._host_verify_verdict(plan, sink)
        attachment = await self._attachment(plan, sink)
        tool = health.tool
        if tool is not None:
            result = await attachment.invoke(tool, dict(health.args))
            return None if result.outcome is ToolOutcome.OK else (None, result.content)
        probe = await attachment.probe()
        return None if probe.reachable else (None, probe.detail)

    async def _host_verify_verdict(
        self, plan: ConnectorPlan, sink: AuditSink
    ) -> tuple[str | None, str] | None:
        state, detail = await self._sign_in_state(plan, sink)
        if state == "signed_in":
            return None
        if state in ("expired", "signed_out"):
            return ("auth_required", detail)
        if state == "not_installed":
            return ("host_missing", detail)
        return ("provider_unavailable", detail)

    @_on_connection
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

    @_on_connection
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
            store = await self._store(sink)
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
                post=post_form,
            )
            await self._store_grant(instance, flow.refresh_token_secret, tokens, store, sink)
            probe = await self._reachability(plan, sink)
            sign_in = await self._sign_in_state(plan, sink)
            supplied = await self._supplied(plan, sink)
            authorize_url = await self._oauth_authorize_url(plan, sink)
        await self._push_credential_change(instance)
        return _authorization(
            instance, plan, probe, sign_in, supplied, authorize_url=authorize_url
        )

    async def _store_grant(
        self,
        instance: str,
        refresh_field: str,
        tokens: OAuthTokens,
        store: SecretStore,
        sink: AuditSink,
    ) -> None:
        """Persist what the code exchange issued.

        With an access token and a lifetime, the refresh token and the access token
        are committed in ONE write, so the first call uses the exchanged access token
        instead of spending a refresh. Otherwise only the refresh token is stored.
        """
        ref = SecretRef(connection=instance, field=refresh_field)
        if not tokens.access_token or tokens.expires_in <= 0:
            await store.put(ref, tokens.refresh_token)
            return
        custody = await self._custody(sink)
        issued = self._clock()
        generation = await custody.rows.put_grant(
            instance,
            refresh_field=refresh_field,
            refresh_token=tokens.refresh_token,
            access_token=tokens.access_token,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=tokens.expires_in),
            scope=None,
            actor_did=causal.actor_did(),
        )
        emit(
            AuditEvent(
                actor_did=causal.actor_did(),
                action="secret.write",
                target=f"secret:{ref}",
                outcome="allow",
                extra={"store": "sealed", "kind": "oauth_grant", "generation": generation},
            ),
            sink,
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
        store = await self._store(sink)
        client_id = await self._read_secret(store, plan.instance, flow.client_id_secret)
        return build_authorize_url(flow, client_id=client_id) if client_id else ""

    async def _read_secret(self, store: SecretStore, instance: str, field: str) -> str:
        """One connector secret's value, or empty when nothing is stored yet."""
        found = await store.get(SecretRef(connection=instance, field=field))
        return found.reveal() if found is not None else ""

    @_on_connection
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
        if token:
            await self._operator_check(instance)
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
                caller_did=causal.actor_did(),
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
        missing = {verdict.name for verdict in plan.unsatisfied_host}
        for required in checks:
            if required.name in missing:
                return ("not_installed", f"{required.name} is not installed on this host")

        placed = await self._placement(plan, sink)
        detail = ""
        for required in checks:
            result = await run_authorization_check(
                required,
                audit_sink=sink,
                tier=self._world.tier,
                env=placed,
                visible=_visible_placements(plan),
                **self._timeout_kwargs(),
            )
            # The command, then what it said. A surface renders this beside the
            # badge, so it has to be the evidence FOR that badge: the probe's own
            # line ("signs in on this host; Arc ran nothing") sat inside a green
            # "Signed in" box describing something nobody had run.
            detail = f"{required.verify_command}: {result.detail}".strip().rstrip(":")
            if not result.known:
                return ("unknown", detail)
            if result.expired:
                return ("expired", detail)
            if not result.authorized:
                return ("signed_out", detail)
        return ("signed_in", detail)

    def _timeout_kwargs(self) -> dict[str, float]:
        """The step timeout a caller pinned, or nothing so each verb keeps its default."""
        return {} if self._host_step_timeout is None else {"timeout": self._host_step_timeout}

    # --- remote sign-in ----------------------------------------------------

    async def begin_remote_login(
        self, instance: str, *, accept_warnings: bool = False
    ) -> RemoteLoginStart:
        """Start the browser sign-in for one connection and return the link to open.

        One sign-in per host binary may be waiting at a time (see
        :class:`~arcagent.extension.remote_login.RemoteLoginLedger`); starting the
        same connection again replaces its own, and a different connection is
        refused by name rather than silently taking its place.

        A blank field its bundle warns about (``blank_warning`` — for Google, no
        OAuth client of the operator's own, so sign-ins expire in a week) refuses
        the begin with that warning until the operator accepts it: the fallback
        exists, and it is labelled rather than silent.

        Raises:
            ExtensionError: No such connection (``code`` :data:`NOT_INSTALLED`), the
                bundle declares no remote sign-in, another account's sign-in is
                waiting, the connection's account is not a plain address, or the
                binary refused or printed no safe link (``code``
                :data:`~arcagent.extension.remote_login.REMOTE_LOGIN_FAILED`).
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            required = self._remote_login_host(plan)
            async with self._remote_logins.lock(required.name):
                with self._refusal_recorded(sink, "begin", instance, required.name):
                    values = await self._login_values(plan, sink)
                    account = values.get("account", "")
                    warnings = await self._blank_warnings(plan, sink)
                    if warnings and not accept_warnings:
                        raise _refuse(
                            REMOTE_LOGIN_NEEDS_CONFIRMATION,
                            " ".join(warnings),
                            connection=instance,
                        )
                    self._remote_logins.admit_begin(
                        required.name, instance=instance, account=account
                    )
                step = await run_remote_login_begin(
                    required,
                    values=values,
                    caller_did=causal.actor_did(),
                    audit_sink=sink,
                    tier=self._world.tier,
                    instance=instance,
                    env=await self._placement(plan, sink),
                    visible=_visible_placements(plan),
                    optional=_optional_fields(plan),
                    **self._timeout_kwargs(),
                )
                if not step.completed or step.link is None:
                    raise _refuse(REMOTE_LOGIN_FAILED, step.detail, connection=instance)
                self._remote_logins.record(
                    required.name,
                    PendingLogin(
                        instance=instance,
                        account=account,
                        redirect_base=step.link.redirect_base,
                        state=step.link.state,
                        started=self._remote_logins.now(),
                    ),
                )
        return RemoteLoginStart(
            instance=instance,
            account=account,
            consent_url=step.link.url,
            expires_in=int(self._remote_logins.ttl),
            warnings=warnings,
        )

    @_on_connection
    async def complete_remote_login(self, instance: str, *, redirect_url: str) -> Authorization:
        """Finish the browser sign-in with the address the operator pasted, then check it.

        The address is checked against the sign-in :meth:`begin_remote_login`
        started for this same connection before the binary runs. Once the binary
        has run, the begun sign-in is spent whatever it answered — its code is
        single-use — so a failure asks the operator to start again rather than
        retry a dead code. The answer is the manifest's own check, run afterwards
        with this connection's account: the only evidence the account WORKS.

        Raises:
            ExtensionError: As :meth:`begin_remote_login`, plus no waiting sign-in
                for this connection (``code`` :data:`~arcagent.extension.
                remote_login.REMOTE_LOGIN_NOT_STARTED`) or a pasted address that
                fails its checks or that the binary refused.
        """
        with self._audit.open() as sink:
            plan = self._plan_for(instance, sink)
            required = self._remote_login_host(plan)
            placed = await self._placement(plan, sink)
            async with self._remote_logins.lock(required.name):
                with self._refusal_recorded(sink, "complete", instance, required.name):
                    values = await self._login_values(plan, sink)
                    pending = self._remote_logins.admit_complete(
                        required.name, instance=instance, account=values.get("account", "")
                    )
                step = await run_remote_login_complete(
                    required,
                    values=values,
                    redirect_url=redirect_url,
                    expected=pending,
                    caller_did=causal.actor_did(),
                    audit_sink=sink,
                    tier=self._world.tier,
                    instance=instance,
                    env=placed,
                    visible=_visible_placements(plan),
                    optional=_optional_fields(plan),
                    **self._timeout_kwargs(),
                )
                if step.reason != "invalid_input":
                    self._remote_logins.clear(required.name)
            if not step.completed:
                raise _refuse(REMOTE_LOGIN_FAILED, step.detail, connection=instance)
            probe = await self._reachability(plan, sink)
            sign_in = await self._sign_in_state(plan, sink)
            supplied = await self._supplied(plan, sink)
        await self._operator_check(instance)
        return _authorization(instance, plan, probe, sign_in, supplied, note=step.detail)

    @contextmanager
    def _refusal_recorded(
        self, sink: AuditSink, step: str, instance: str, binary: str
    ) -> Iterator[None]:
        """Audit a sign-in step refused before its binary ran, then let the refusal go.

        The runner records every step it starts; this records the ones stopped
        earlier — a busy ledger, a bad account, a complete with nothing begun — so
        a repeated forged or out-of-order attempt is visible in the chain too.
        """
        try:
            yield
        except ExtensionError as refusal:
            emit(
                AuditEvent(
                    actor_did=causal.actor_did(),
                    action=f"extension.host.remote_login.{step}",
                    target=f"host:{binary}",
                    outcome="deny",
                    tier=self._world.tier.value,
                    extra={"binary": binary, "connection": instance, "reason": refusal.code},
                ),
                sink,
            )
            raise

    def _remote_login_host(self, plan: ConnectorPlan) -> HostRequirement:
        """The prerequisite whose sign-in a browser can drive, or a refusal naming why not."""
        for required in plan.manifest.host_requires:
            if required.remote_login is not None:
                return required
        raise _refuse(
            REMOTE_LOGIN_FAILED,
            f"{plan.extension} has no sign-in Arc can run from a browser",
            connection=plan.instance,
        )

    async def _login_values(self, plan: ConnectorPlan, sink: AuditSink) -> dict[str, str]:
        """The non-sensitive fields a sign-in step may name, shaped for argv.

        A field the manifest declares as an email address is held to the strict
        address rule before it can become an argument: the stored value passed
        the looser entry check, and argv is where a leading ``-`` becomes a flag.
        """
        formats = {declared.name: declared.format for declared in plan.secrets}
        values: dict[str, str] = {}
        for field in await self._supplied(plan, sink):
            value = field.value or field.default
            if field.sensitive or not value:
                continue
            values[field.name] = (
                checked_account(value) if formats.get(field.name) == "email" else value
            )
        return values

    async def _blank_warnings(self, plan: ConnectorPlan, sink: AuditSink) -> tuple[str, ...]:
        """The bundle's warnings for fields this connection left blank, in order."""
        return tuple(field.warning for field in await self._supplied(plan, sink) if field.warning)

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
            caller_did=causal.actor_did(),
            audit_sink=sink,
            tier=self._world.tier,
            values={field.name: field.value for field in await self._supplied(plan, sink)},
        )
        return result.detail

    @_on_connection
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
            custody = await self._custody(sink)
            report = await install_connector(
                plan,
                connections=self.registry,
                agents=agents,
                secret_values=secrets,
                store=custody.store,
                credential=self._operator_handle(custody, plan),
                caller_did=causal.actor_did(),
                state=await self._connection_state(),
                attachment_factory=self._factory,
                audit_sink=sink,
                trusted_public_key=self._pinned_key(),
            )
        await self._operator_check(plan.instance)
        return report

    async def preview_mcp_server(
        self, spec: McpServerSpec, *, secret_values: Mapping[str, str], timeout: float = 30.0
    ) -> tuple[DiscoveredTool, ...]:
        """List what an MCP server offers. Writes nothing and keeps no credential.

        The operator reads this list to choose which tools to expose; the spec the
        choice goes into is then handed to :meth:`add_mcp_server`.
        """
        tier = self._tier_for(())
        with self._audit.open() as sink:
            try:
                found = await discover_tools(
                    spec,
                    tier=tier,
                    secret_values=secret_values,
                    stdio_allow=self._world.mcp_stdio_allow,
                    builder=self._factory,
                    timeout=timeout,
                )
            except ExtensionError as exc:
                self._mcp_audit(sink, "mcp_server.preview", spec, "deny", reason=exc.code)
                raise
            self._mcp_audit(sink, "mcp_server.preview", spec, "allow", tools=str(len(found)))
        return found

    async def add_mcp_server(
        self,
        spec: McpServerSpec,
        *,
        instance: str | None = None,
        agents: Sequence[str] = (),
        secret_values: Mapping[str, str],
        replace: bool = False,
    ) -> McpServerAdded:
        """Generate, sign and install a connector bundle for an operator's MCP server.

        The steps, in the order that makes each one safe: the spec is refused if it is
        unsafe, the credential fields are checked, the bundle is written and signed by
        the operator key, and only then is it installed through :meth:`install` — so
        the signature gate, the credential custody, the probe and the contract
        approval are the ones every other connection uses. ``secret_values`` go to
        :meth:`install` and nowhere else: they are never written beside the bundle,
        never put in an audit event, and never returned. A refused install removes
        the bundle this call wrote.

        Raises:
            ExtensionError: A step refused; nothing is left behind.
        """
        for agent in agents:
            _check_agent(agent)
        name = instance or spec.name
        tier = self._tier_for(agents)
        with self._audit.open() as sink:
            checked, signer, signer_did = self._checked_mcp_spec(
                sink, spec, name, tier, secret_values
            )
            folder = self._write_mcp_bundle(sink, checked, replace=replace)
            try:
                sign_bundle(folder, signer=signer, signer_did=signer_did)
                self._mcp_audit(
                    sink, "mcp_server.bundle_signed", checked, "allow", signer=signer_did
                )
                # The bundle's root may not have existed when this deployment was resolved.
                # Everything after this call (the health check inside install) reads the
                # world's roots, so the world has to learn about the one just created.
                roots = self._bundle_roots()
                self._learn_roots(roots)
                plan = plan_connector(
                    extensions_root=roots,
                    extension=checked.name,
                    instance=name,
                    tier=tier,
                    audit_sink=sink,
                    egress_allow=self._world.egress_allow,
                )
            except BaseException:
                self._discard_bundle(folder)
                raise
        try:
            report = await self.install(plan, secret_values, agents=agents)
        except ExtensionError as exc:
            self._discard_bundle(folder)
            with self._audit.open() as sink:
                self._mcp_audit(sink, "mcp_server.add", checked, "deny", reason=exc.code)
            raise
        with self._audit.open() as sink:
            self._mcp_audit(
                sink,
                "mcp_server.add",
                checked,
                "allow",
                instance=name,
                agents=",".join(agents),
                tools=",".join(report.tools),
            )
        offered = set(report.tools)
        return McpServerAdded(
            report=report,
            spec_sha256=spec_digest(checked),
            bundle=folder,
            signed_by=signer_did,
            exposed=tuple(
                checked.namespaced(verb)
                for verb in checked.tools
                if checked.namespaced(verb) in offered
            ),
        )

    def _checked_mcp_spec(
        self,
        sink: AuditSink,
        spec: McpServerSpec,
        name: str,
        tier: Tier,
        secret_values: Mapping[str, str],
    ) -> tuple[McpServerSpec, Signer, str]:
        """Everything that can be refused before a byte is written."""
        try:
            checked = validate_spec(spec, tier=tier, stdio_allow=self._world.mcp_stdio_allow)
            require_exactly(checked, secret_values)
            if name in self.registry.all():
                raise _refuse("MCP_NAME_TAKEN", f"a connection named {name!r} already exists")
            signer, signer_did = self._bundle_signer(tier)
        except ExtensionError as exc:
            self._mcp_audit(sink, "mcp_server.add", spec, "deny", reason=exc.code)
            raise
        self._mcp_audit(sink, "mcp_server.spec_validated", checked, "allow")
        return checked, signer, signer_did

    def sign_bundle(self, folder: Path) -> tuple[Path, ...]:
        """Sign a hand-written bundle with the operator key, so it verifies at load.

        The manifest has to parse at this deployment's tier first: the operator key
        must never vouch for a file Arc would refuse to read.

        Raises:
            ExtensionError: Not a bundle, or the manifest is refused at this tier.
        """
        manifest = Path(folder) / MANIFEST_NAME
        if not manifest.is_file() or manifest.is_symlink():
            raise _refuse("MCP_NOT_A_BUNDLE", f"{folder} holds no {MANIFEST_NAME}")
        try:
            load_manifest(manifest.read_text(encoding="utf-8"), tier=self._world.tier)
        except Exception as exc:  # reason: any parse or tier refusal means do not sign
            raise _refuse("MCP_NOT_A_BUNDLE", f"{manifest} would not load: {exc}") from exc
        signer, signer_did = self._bundle_signer(self._world.tier)
        signed = tuple(sign_bundle(Path(folder), signer=signer, signer_did=signer_did))
        with self._audit.open() as sink:
            emit(
                AuditEvent(
                    actor_did=causal.actor_did(),
                    action="connector.bundle_signed",
                    target=f"bundle:{Path(folder).name}",
                    outcome="allow",
                    tier=self._world.tier.value,
                    extra={"files": str(len(signed)), "signer": signer_did},
                ),
                sink,
            )
        return signed

    def install_bundle(self, folder: Path, *, replace: bool = False) -> Path:
        """Install a signed, code-bearing bundle into ``~/.arc/extensions`` (P18-2).

        Nothing executes from the operator tree, so a third-party or hand-written
        connector with code is installed here instead. Every file must verify
        against the deployment's operator key NOW (sign it first with
        ``arc connector sign``); the loader verifies it again on every load.

        Raises:
            ExtensionError: Not a bundle, a symlink inside it, a file that does
                not verify, no pinned operator key, or the name already installed.
        """
        from arctrust.paths import installed_extensions_dir

        from arcagent.capabilities import artifact_signing

        source = Path(folder)
        manifest_path = source / MANIFEST_NAME
        if source.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise _refuse("BUNDLE_NOT_INSTALLABLE", f"{folder} holds no {MANIFEST_NAME}")
        manifest = load_manifest(manifest_path.read_text(encoding="utf-8"), tier=self._world.tier)
        name = manifest.extension.name
        key = self._pinned_key()
        if key is None:
            raise _refuse("BUNDLE_NOT_INSTALLABLE", "no operator key is pinned to verify against")
        files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
        if any(path.is_symlink() for path in files):
            raise _refuse("BUNDLE_NOT_INSTALLABLE", f"{name!r} contains a symlink")
        shipped = [p for p in files if p.suffix != artifact_signing.SIDECAR_SUFFIX]
        unverified = [
            p
            for p in shipped
            if not artifact_signing.verify_file(p, p.read_bytes(), trusted_public_key=key)
        ]
        if unverified:
            raise _refuse(
                "BUNDLE_UNSIGNED",
                f"{len(unverified)} file(s) in {name!r} are unsigned or fail verification; "
                "sign it with `arc connector sign` first",
            )
        root = installed_extensions_dir()
        target = root / name
        if (target.exists() or target.is_symlink()) and not replace:
            raise _refuse("BUNDLE_EXISTS", f"{name!r} is already installed at {target}")
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".{name}.staging-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(source, staging, symlinks=True)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
        with self._audit.open() as sink:
            emit(
                AuditEvent(
                    actor_did=causal.actor_did(),
                    action="connector.bundle_installed",
                    target=f"bundle:{name}",
                    outcome="allow",
                    tier=self._world.tier.value,
                    extra={"files": str(len(shipped))},
                ),
                sink,
            )
        self._learn_roots(self._bundle_roots())
        return target

    def _learn_roots(self, roots: tuple[Path, ...]) -> None:
        self._world = replace(self._world, extension_roots=roots)

    def _bundle_roots(self) -> tuple[Path, ...]:
        """The search path as it is NOW — a generated bundle's root may not have existed."""
        if self._world.extensions_override:
            return self._world.extension_roots
        return resolve_extension_roots(self._world.arc_dir)

    def _write_mcp_bundle(self, sink: AuditSink, spec: McpServerSpec, *, replace: bool) -> Path:
        override = self._world.extensions_override
        first = self._world.extension_roots[0] if self._world.extension_roots else None
        base = first.parent if override and first is not None else self._world.arc_dir
        dirname = first.name if override and first is not None else BUNDLES_DIRNAME
        # A bundle this module wrote, whose connection has since been removed, is
        # leftover: adding the same server again rewrites it. A hand-written bundle
        # of that name is never touched without an explicit ``replace``.
        leftover = is_generated_bundle(base / dirname / spec.name) and not any(
            held.extension == spec.name for held in self.registry.all().values()
        )
        try:
            folder = write_bundle(
                spec,
                base,
                replace=replace or leftover,
                other_roots=self._world.extension_roots,
                bundles_dirname=dirname,
            )
        except ExtensionError as exc:
            self._mcp_audit(sink, "mcp_server.add", spec, "deny", reason=exc.code)
            raise
        self._mcp_audit(sink, "mcp_server.bundle_written", spec, "allow")
        return folder

    @staticmethod
    def _discard_bundle(folder: Path) -> None:
        """Remove the bundle this call wrote, never following a symlink."""
        if not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)

    def _bundle_signer(self, tier: Tier) -> tuple[Signer, str]:
        """The operator key as a signer. Minted at personal tier only, never above it."""
        from arctrust import bootstrap_operator_signer
        from arctrust.policy import OperatorApprovalAuthority

        signer = _operator_signer(self._world.arc_dir)
        if signer is None:
            if tier is not Tier.PERSONAL:
                raise _refuse(
                    "MCP_NO_OPERATOR_KEY",
                    "this deployment has no operator key to sign the bundle with; "
                    "create one with `arc init` first",
                )
            signer = bootstrap_operator_signer(base=self._world.arc_dir)
        return signer, str(OperatorApprovalAuthority(signer).did)

    def _mcp_audit(
        self, sink: AuditSink, action: str, spec: McpServerSpec, outcome: str, **extra: str
    ) -> None:
        """One audit event per step. The spec holds no secret, so its digest is safe."""
        emit(
            AuditEvent(
                actor_did=causal.actor_did(),
                action=action,
                target=f"connector:{spec.name}",
                outcome=outcome,
                tier=self._world.tier.value,
                extra={
                    "name": spec.name,
                    "transport": spec.transport,
                    "spec_sha256": spec_digest(spec),
                    **extra,
                },
            ),
            sink,
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
            store = await self._store(sink)
            for required in plan.secrets:
                value = shaped.get(required.name)
                if not value:
                    continue
                ref = SecretRef(connection=plan.instance, field=required.name)
                await store.put(ref, value)
                written.append(required.name)
        if written:
            await self._push_credential_change(plan.instance)
        return tuple(written)

    async def renew_credentials(self, *, concurrency: int = 4) -> dict[str, str]:
        """Renew every OAuth connection whose access token is due (the proactive renewer).

        One pass over the custody rows. Each due ``[oauth]`` connection is renewed under
        the same fenced lease an on-demand renewal takes, so this loop and a running
        agent can never both spend one refresh token. A failure on one connection is
        isolated and reported per connection; it never stops the pass.

        Returns:
            ``{connection: "renewed" | "fresh" | <error code>}`` for every OAuth row.
        """
        outcome: dict[str, str] = {}
        slots = asyncio.Semaphore(concurrency)
        with self._audit.open() as sink:
            custody = await self._custody(sink)
            flows: dict[str, OAuthFlow] = {}
            for instance in await custody.rows.connections():
                try:
                    flow = self._plan_for(instance, sink).manifest.oauth
                except ExtensionError:
                    continue
                if flow is not None:
                    flows[instance] = flow

            async def renew_one(instance: str) -> None:
                async with slots:
                    try:
                        renewed = await custody.planner.ensure_fresh(
                            instance, flow=flows[instance]
                        )
                        outcome[instance] = "renewed" if renewed else "fresh"
                    except ExtensionError as exc:
                        outcome[instance] = str(exc.details.get("error_code") or exc.code)
                    except Exception as exc:  # reason: one bad row never stops the pass
                        _logger.warning("credential renewal failed: %s", instance, exc_info=True)
                        outcome[instance] = type(exc).__name__

            await asyncio.gather(*(renew_one(instance) for instance in flows))
        return outcome

    async def migrate_secrets(self, *, dry_run: bool = False) -> MigrationReport:
        """Move the legacy plaintext credential file into sealed custody, once (P18-2).

        Every declared credential is stored, read back through a fresh store and
        compared, and only then is the file deleted; undeclared keys are dropped
        by name. Running agents are pushed the change. A dry run writes nothing.

        Raises:
            ExtensionError: The migration could not complete (no custody cipher,
                a read-back mismatch, a symlinked or loose file). The file is kept.
        """
        env_path = legacy_env_path(self._world.arc_dir)
        if not os.path.lexists(env_path):
            return MigrationReport(skipped=True, path=str(env_path))
        with self._audit.open() as sink:
            custody: Custody | None
            try:
                custody = await self._custody(sink)
            except ExtensionError:
                if not dry_run:
                    raise
                custody = None

            def declared(instance: str, _extension: str) -> tuple[str, ...] | None:
                try:
                    plan = self._plan_for(instance, sink)
                except ExtensionError:
                    return None
                return tuple(required.name for required in plan.secrets)

            async def fresh_store() -> SecretStore:
                return (await self._custody(sink)).store

            verify = (await fresh_store()) if custody is not None else None
            report = await migrate_connector_secrets(
                env_path=env_path,
                registry=self.registry,
                declared_fields=declared,
                secret_store=custody.store if custody is not None else None,
                verify_store=(lambda: verify) if verify is not None else None,
                actor_did=causal.actor_did(),
                sink=sink,
                dry_run=dry_run,
            )
        for instance in report.connections if report.deleted else ():
            await self._push_credential_change(instance)
        return report

    async def reseal_secrets(self, *, dry_run: bool = False) -> ResealReport:
        """Move in-process-sealed custody rows under the vault's Transit cipher (P18-2F).

        For a deployment that switched from ``in_process`` to ``vault_transit``
        custody. Explicit and operator-run by design: it reads the OLD on-disk
        operator key once, which a long-running server under ``vault_transit``
        must never hold. Each row moves in one verified CAS (crash-safe); a
        re-run skips rows already moved (idempotent). Running agents are pushed
        the change.

        Raises:
            ExtensionError: not on ``vault_transit``, the old key is gone, the
                transit cannot serve, or a row is sealed under neither key.
        """
        source = reseal_source_cipher(self._world.arc_dir)
        with self._audit.open() as sink:
            custody = await self._custody(sink)
            if custody.rows.cipher_kind != "transit1":
                raise _refuse(
                    VAULT_REQUIRED,
                    "this deployment does not seal connector credentials in a vault",
                )
            report = await reseal_connector_secrets(
                custody.rows,
                source=source,
                actor_did=causal.actor_did(),
                sink=sink,
                dry_run=dry_run,
            )
        for instance in report.resealed:
            await self._push_credential_change(instance)
        return report

    @_on_connection
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
            await ledger.approve(specs, actor_did=causal.actor_did())
            authority = ConnectionHealthAuthority(await self._connection_state(), sink=sink)
            await authority.record(
                instance,
                HealthSignal(ok=True, source="contract", checked_by=causal.actor_did()),
            )
        return tuple(spec.name for spec in specs)

    @_on_connection
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
                credentials=(await self._custody(sink)).rows,
                caller_did=causal.actor_did(),
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

    def _federal_grade_cipher(self) -> bool:
        """True when this deployment seals credentials by reference in its transit."""
        if self._credential_cipher is None:
            try:
                self._credential_cipher = deployment_cipher(
                    self._world.arc_dir, tier=self._world.tier
                )
            except ExtensionError:
                return False
        return self._credential_cipher.kind == "transit1"

    def _refuse_silent_rehome(self, instance: str, agents: Sequence[str], sink: AuditSink) -> None:
        """Refuse a grant that would raise the tier past the store holding the credential.

        Granting a federal agent an account whose token is sealed under an
        in-process key does not move the token into Vault. Honouring it would leave
        a connection reporting federal stringency with its credential under a
        personal-tier cipher — a control reporting a posture it does not have.
        Re-homing it silently is the other half of that hazard, so neither happens:
        the operator is told what to configure and re-runs the connect.

        A connection Arc stores no credential for is untouched by this: a bundle
        whose binary owns its own token has nothing in any store to be in the
        wrong one.
        """
        required = self._tier_for(agents)
        if required is self._world.tier:
            return
        if not self._declared_secret_fields(instance, sink):
            return
        if required is not Tier.FEDERAL:
            return
        # Federal custody is Vault Transit only (FIPS forbids the in-process
        # XChaCha20 cipher). A deployment whose credentials are sealed by the
        # Transit row cipher (P18-2F) already holds them at federal stringency.
        if self._federal_grade_cipher():
            return
        raise _refuse(
            TIER_WOULD_RISE,
            f"granting {instance!r} to these agents raises it to {required.value}, "
            f"and this deployment cannot hold its credential at that tier "
            f"({VAULT_REQUIRED}: connector credentials need the Vault Transit row cipher). "
            f'Set [security] custody = "vault_transit" with a serving transit, run '
            f"`arc connector migrate-secrets --reseal`, then connect {instance!r} again.",
            connection=instance,
            required_tier=required.value,
        )

    def _record(self, sink: AuditSink, action: str, instance: str, agents: Sequence[str]) -> None:
        """Record a change to who may reach an account (AU-2).

        An access-control decision that is not in the chain is not auditable, and
        this one names the connection and the agents rather than only the fact
        that something changed — an auditor reconstructing who could read the mail
        on a given day needs both.
        """
        emit(
            AuditEvent(
                actor_did=causal.actor_did(),
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
        custody = await self._custody(sink)
        secrets = await resolve_secrets(
            plan.manifest,
            connection=plan.instance,
            store=custody.store,
            include_sensitive=plan.manifest.extension.attachment == "mcp",
        )
        return self._factory(
            plan.manifest,
            plan.bundle,
            secrets,
            credential=self._operator_handle(custody, plan),
        )

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
        store = await self._store(sink)
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
                found = await store.get(ref)
                value = found.reveal() if found is not None else ""
            rows.append(
                SuppliedCredential(
                    name=declared.name,
                    prompt=declared.prompt,
                    sensitive=declared.sensitive,
                    value=value,
                    required=declared.required,
                    choices=tuple(declared.choices),
                    default=declared.default,
                    warning=declared.blank_warning if not value else "",
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
                store=await self._store(sink),
            )
        except ExtensionError:
            return {}
        return placement_environment(plan.manifest, secrets)

    async def _custody(self, sink: AuditSink) -> Custody:
        """Sealed custody over this deployment's arcstore, sealed with its operator key.

        Raises:
            ExtensionError: ``SECRET_STORE_VAULT_REQUIRED`` when this deployment's
                custody cannot hold a connector credential yet (vault_transit).
        """
        if self._credential_cipher is None:
            self._credential_cipher = deployment_cipher(self._world.arc_dir, tier=self._world.tier)
        backend = await self._custody_backend()

        async def opened() -> Any:
            return backend

        return open_custody(
            backend,
            self._credential_cipher,
            health=StoreHealthReporter(opened, sink=sink),
            sink=sink,
            actor_did=causal.actor_did(),
        )

    async def _custody_backend(self) -> Any:
        if self._backend is None:
            self._backend = await self._open_reconcile_backend()
        return self._backend

    async def _store(self, sink: AuditSink) -> SecretStore:
        """The one place a connector credential is written or read."""
        return (await self._custody(sink)).store

    def _operator_handle(self, custody: Custody, plan: ConnectorPlan) -> AccessTokenHandle:
        """The credential handle the deployment's own verbs (probe, install) use."""
        broker = custody.broker(registry=lambda: self.registry)
        return broker.operator_handle(
            plan.instance, actor_did=causal.actor_did(), plan=credential_plan(plan.manifest)
        )

    async def _push_credential_change(self, instance: str) -> None:
        """A credential changed: reach running agents, then look at the connection.

        Native and CLI attachments already read the new value through their handle;
        the push is for MCP attachments (resolved at session start, C7) and for
        non-sensitive fields baked in at build.
        """
        try:
            agents = self.registry.get(instance).agents
        except ExtensionError:
            agents = ()
        if agents:
            try:
                await self._reconcile_agents(agents)
            except Exception:  # reason: the credential is stored; activation retries
                _logger.warning("reconcile after credential change failed: %s", instance)
        await self._operator_check(instance)

    def _pinned_key(self) -> bytes | None:
        """The operator key an extension bundle's signatures are pinned to (REQ-283)."""
        signer = _operator_signer(self._world.arc_dir)
        return None if signer is None else bytes(signer.public_key)

    async def _credential_checks(
        self, plan: ConnectorPlan, instance: str, sink: AuditSink
    ) -> list[DoctorCheck]:
        """One row per declared credential: present or missing, never the value."""
        try:
            custody = await self._custody(sink)
            row = await custody.rows.read(instance)
        except ExtensionError as exc:
            return [DoctorCheck(required.name, "error", exc.message) for required in plan.secrets]
        held = set(row.fields) if row is not None else set()
        return [
            DoctorCheck(
                required.name,
                "present" if required.name in held else "missing",
                "sealed custody",
            )
            for required in plan.secrets
        ]

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
    "CREDENTIAL_LOCATION",
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
    "McpServerAdded",
    "MigrationReport",
    "ProbeResult",
    "RemoteLoginLedger",
    "RemoteLoginStart",
    "RemovalReport",
    "SecretRequirement",
    "SignInState",
    "SuppliedCredential",
    "Tier",
    "ToolSpec",
    "agent_tier",
    "catalog",
    "deployment_egress_allow",
    "deployment_mcp_stdio_allow",
    "deployment_tier",
    "resolve_deployment",
    "resolve_roots",
]
