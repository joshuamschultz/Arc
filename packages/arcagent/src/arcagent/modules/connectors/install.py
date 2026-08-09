"""SPEC-062 COMP-016 — the connector install path, shared by every surface.

The write half of connector management lives here rather than in the CLI so that
the terminal UI and the web panel drive the same order of operations instead of
each reimplementing it (D-561, REQ-293). ``arccli.commands.connector`` owns only
the prompting: it collects the declared secrets with a hidden prompt and hands
them in. That split is the one ``arcgateway.connect`` already uses for arcui's
benefit, and it is what keeps the dependency arrow pointing down — arcui and
arctui call into arcagent, never into arccli.

**The order is the security property.** Seven steps run in exactly one sequence —
``resolve``, ``manifest``, ``host``, ``verify``, ``secrets``, ``probe``,
``persist`` — and configuration is written in the last one only (REQ-260). A
failure anywhere names its step and unwinds what it wrote (REQ-261): the
credential is the one thing that lands before the probe, so it is the one thing
rolled back. Nothing half-connected is left for an operator to discover later,
and nothing that failed to probe is left in a config file to fail again at every
agent start.

``verify`` sits where it does for one reason: building an attachment EXECUTES
extension-declared code — a native attachment imports the extension's own module
— so the signature gate has to close before that, and before a credential is
handed to a bundle that may be refused (REQ-282).

**Host prerequisites are directed, never installed** (REQ-262): an absent binary
stops the install with the instruction to run, because a machine-level change is
the host's decision and must be visible.

**A connection is the deployment's, not an agent's.** The definition and its
grants are written to :class:`~arcagent.extension.grants.ConnectionRegistry` under
``arc_home()``, and the credential to one owner-only file beside it. An install
therefore hands nothing to any agent: the account exists, and the agents named in
``agents`` may use it. An agent not named gets no verb and no credential, which is
what makes adding an agent incapable of widening access.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arctrust.audit import AuditSink, NullSink
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment, ProbeResult
from arcagent.extension.catalog import MANIFEST_NAME, ExtensionCatalog
from arcagent.extension.cli_attachment import CliAttachment, CliCommand, CliResilience
from arcagent.extension.contract_ledger import ToolContractLedger
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal
from arcagent.extension.field_formats import normalize
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.host import HostPrerequisiteDirector, HostVerdict
from arcagent.extension.loader import ExtensionLoader
from arcagent.extension.manifest import ExtensionManifest, SecretRequirement, load_manifest
from arcagent.extension.native_attachment import NativeAttachment
from arcagent.extension.secrets import Secret, SecretRef, SecretStore
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore
from arcagent.tools._egress_policy import first_forbidden_egress

if TYPE_CHECKING:
    from arcagent.capabilities.capability_registry import CapabilityRegistry

_logger = logging.getLogger("arcagent.modules.connectors.install")

#: The deployment's owner-only credential file, beside its connections (D-555).
CONNECTOR_ENV_FILENAME = "connections.env"

#: The ordered steps an install runs, and the vocabulary a failure reports in.
INSTALL_STEPS: tuple[str, ...] = (
    "resolve",
    "manifest",
    "host",
    "verify",
    "secrets",
    "probe",
    "persist",
)

#: How a manifest's declared attachment kind becomes something that can be probed,
#: holding the credentials the operator connected it with.
AttachmentFactory = Callable[[ExtensionManifest, Path, Mapping[str, Secret]], ExtensionAttachment]


def _refuse(step: str, message: str, **details: Any) -> ExtensionError:
    """Build the one error shape every failure raises, always naming its step."""
    return ExtensionError(
        code="CONNECTOR_INSTALL_FAILED",
        message=f"{step}: {message}",
        details={"step": step, **details},
    )


@dataclass(frozen=True)
class ConnectorPlan:
    """Everything an install needs, and everything the operator must still supply.

    Produced without writing anything, so a surface can show the operator what is
    about to happen — and so ``doctor`` can report a broken connection without
    touching it.
    """

    instance: str
    extension: str
    bundle: Path
    manifest: ExtensionManifest
    unsatisfied_host: tuple[HostVerdict, ...]
    secrets: tuple[SecretRequirement, ...]
    approval_mode: str
    tier: Tier
    extensions_root: tuple[Path, ...]
    egress_allow: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallReport:
    """What a completed install produced."""

    instance: str
    extension: str
    tools: tuple[str, ...]
    detail: str = ""


@dataclass(frozen=True)
class RemovalReport:
    """What a removal actually dropped, so a surface can say so precisely."""

    instance: str
    removed_secrets: tuple[str, ...] = ()
    removed_config: bool = False
    removed_state: bool = False


def plan_connector(
    *,
    extensions_root: Sequence[Path],
    extension: str,
    instance: str,
    tier: Tier,
    audit_sink: AuditSink,
    director: HostPrerequisiteDirector | None = None,
    egress_allow: Sequence[str] = (),
) -> ConnectorPlan:
    """Run the three read-only steps: resolve the bundle, parse it, inspect the host.

    Args:
        extensions_root: The bundle search path, in order (D-584). A loadable
            bundle lives directly inside one of these; the first root holding the
            name wins, so an agent-local bundle overrides a fleet-wide one without
            hiding the rest of the fleet. :func:`~arcagent.extension.catalog.
            resolve_extension_roots` composes the deployment's order.
        extension: The bundle name the operator asked for.
        instance: The name this connected account will be known by, validated
            against the one coordinate rule (:mod:`arcagent.extension.
            coordinates`). Checked HERE, and only here, because every path — the
            CLI, the web, the TUI, and every read verb behind them — comes
            through this function: one check, and no route by which an unusable
            name survives anywhere. It covers a bundle declaring no
            ``[[secrets]]``, which is the shape whose name nothing else on the
            path ever looks at.

            A name this refuses is still removable: ``remove`` reads its
            credential fields through here, treats a refusal as "no fields to
            delete" — correct, because ``SecretRef`` applies the same rule, so no
            credential can exist under such a name — and drops the config block
            and the connection record regardless. The strict path has an exit.
        tier: The deployment tier — decides the unlisted-bundle verdict, the
            unbounded-allowlist refusal, and the egress verdict.
        audit_sink: Where the catalog records its verdicts.
        director: Host detection. Injectable so a test never depends on what
            happens to be installed on the machine running it.
        egress_allow: ``tools.policy.egress_allow`` — the tools this operator
            permits to send data out (D-580). Read from the agent config by
            :attr:`~arcagent.extension.grants.Deployment.egress_allow`.

    Returns:
        The plan, including any host prerequisite the operator must satisfy first.

    Raises:
        ExtensionError: The name or bundle was refused (``step="resolve"``) or the
            manifest did not parse or was refused at this tier (``step="manifest"``).
            The egress verdict is deliberately NOT taken here: this function is
            also how ``remove`` finds the credentials to delete and how ``doctor``
            reports a connection, and a planner that refuses would strand the
            credentials of a connection the tier has since turned forbidden.
    """
    if not is_coordinate(instance):
        raise _refuse("resolve", coordinate_refusal("connection name", instance))

    roots = tuple(Path(root) for root in extensions_root)
    catalog = ExtensionCatalog(roots=roots, tier=tier, audit_sink=audit_sink)
    try:
        resolution = catalog.resolve(extension)
    except ExtensionError as exc:
        raise _refuse("resolve", exc.message, extension=extension) from exc

    manifest_path = resolution.path / MANIFEST_NAME
    try:
        manifest = load_manifest(manifest_path.read_text(encoding="utf-8"), tier=tier)
    except (OSError, ValueError, ValidationError, ExtensionError) as exc:
        raise _refuse("manifest", f"{manifest_path} — {exc}", extension=extension) from exc

    checker = director or HostPrerequisiteDirector()
    return ConnectorPlan(
        instance=instance,
        extension=resolution.name,
        bundle=resolution.path,
        manifest=manifest,
        unsatisfied_host=tuple(checker.unsatisfied(manifest.host_requires)),
        secrets=tuple(manifest.secrets),
        approval_mode=manifest.approval.default,
        tier=tier,
        extensions_root=roots,
        egress_allow=tuple(egress_allow),
    )


async def install_connector(
    plan: ConnectorPlan,
    *,
    connections: ConnectionRegistry,
    agents: Sequence[str],
    secret_values: Mapping[str, str],
    store: SecretStore,
    caller_did: str,
    state: ConnectionStateStore,
    attachment_factory: AttachmentFactory | None = None,
    audit_sink: AuditSink | None = None,
    trusted_public_key: bytes | None = None,
    registry: CapabilityRegistry | None = None,
) -> InstallReport:
    """Finish the install: host, verify, secrets, probe, and only then persist.

    Args:
        plan: The result of :func:`plan_connector`.
        connections: The deployment's connections and grants — where this account
            is defined, and the only thing that decides who may use it.
        agents: The agents granted this connection. Empty is legal and means an
            account nothing can reach yet, which is the honest state of a
            connection an operator has proved works and not yet handed out.
        secret_values: One value per declared secret, collected by the surface.
        store: Where credentials are written — and nowhere else.
        caller_did: Recorded as the actor on every credential operation, and the
            operator this connection's tool contract is approved by.
        state: The connection directory. Required, not optional: an install that
            registers no record is an install whose tools can never be approved,
            because every later write is a merge patch against a row that is not
            there. Making it a parameter a caller could omit is what shipped a
            connection reporting ``Connected`` and serving nothing.
        attachment_factory: How the manifest becomes something probeable.
        audit_sink: Where the loader records its verification verdict.
        trusted_public_key: The operator key bundle signatures are pinned to.
            ``None`` above personal tier is itself the refusal (REQ-283).
        registry: The capability registry the bundle's skills and tools register
            into. A fresh one is used when the surface has none.

    Returns:
        The instance, the tools the probe found, and the probe's own detail line.

    Raises:
        ExtensionError: Any step failed. ``details["step"]`` names which, and
            every credential this call wrote has already been removed.
    """
    _refuse_declared_egress(plan)
    if plan.unsatisfied_host:
        missing = plan.unsatisfied_host[0]
        raise _refuse(
            "host",
            f"{missing.name} is not installed on this host — {missing.instruction}",
            extension=plan.extension,
            prerequisite=missing.name,
        )

    await _verify_bundle(
        plan, audit_sink=audit_sink, trusted_public_key=trusted_public_key, registry=registry
    )

    written = await _write_secrets(plan, values=secret_values, store=store, did=caller_did)
    try:
        # Read back out of the store rather than reusing ``secret_values``: what the
        # probe proves must be the credential the running agent will later resolve,
        # not the one this call happened to be handed.
        secrets = await resolve_secrets(
            plan.manifest, connection=plan.instance, store=store, caller_did=caller_did
        )
        probe = await _probe(plan, attachment_factory, secrets)
        _refuse_probed_egress(plan, probe)
    except BaseException:
        await _forget_secrets(written, store=store, did=caller_did)
        raise

    connections.define(
        plan.instance,
        Connection(extension=plan.extension, approval=plan.approval_mode, agents=tuple(agents)),
    )
    await state.create(
        ConnectionRecord(connection=plan.instance, health="healthy"), actor_did=caller_did
    )
    # Connecting IS approving (REQ-291). An operator who supplied this account's
    # credentials and completed its probe has consented to the contract it just
    # served; requiring a second, separate ``arc connector approve`` before a
    # single verb worked was the step nobody knew to run. The defence is
    # untouched — it exists to catch a contract that moves AFTER approval, and
    # this is the moment there is finally something for it to move away from.
    await ToolContractLedger(
        state, connection=plan.instance, sink=audit_sink or NullSink()
    ).approve(probe.tools, actor_did=caller_did)
    return InstallReport(
        instance=plan.instance,
        extension=plan.extension,
        tools=tuple(spec.name for spec in probe.tools),
        detail=probe.detail,
    )


async def remove_connector(
    *,
    connections: ConnectionRegistry,
    instance: str,
    store: SecretStore,
    caller_did: str,
    secret_fields: Sequence[str],
    state: ConnectionStateStore,
) -> RemovalReport:
    """Drop one connected account: its credential, its definition, its grants, its state.

    Removing the definition removes every grant on it in the same write, so there
    is no state in which an agent still holds a grant for an account that no
    longer exists.

    ``state`` is required for the same reason it is on the install: a removal that
    leaves the record behind hands the next install of that name the approvals an
    operator minted for the account they just disconnected.

    Removing something that was never installed is reported, not raised: an
    operator cleaning up after a failed install must not be blocked by a step that
    already has nothing to do.
    """
    removed: list[str] = []
    for field in secret_fields:
        ref = SecretRef(connection=instance, field=field)
        if await store.delete(ref, caller_did=caller_did):
            removed.append(field)

    return RemovalReport(
        instance=instance,
        removed_secrets=tuple(removed),
        removed_config=connections.forget(instance),
        removed_state=await state.forget(instance, actor_did=caller_did),
    )


# --- where a credential lives ----------------------------------------------


def connector_env_file(arc_dir: Path) -> Path:
    """The owner-only file this deployment's connector credentials live in (D-555).

    One resolver rather than a filename constant per surface: the CLI, the TUI,
    the web and the running agent all hand this path to
    ``select_secret_backend``, and a surface spelling it differently would write a
    credential the others cannot read.

    One file for the deployment, beside ``connections.toml``, because a connection
    is one account. Per-agent files were the shape that made "grant" mean "type
    the token again", and left a copy behind on every revoke.
    """
    return Path(arc_dir) / CONNECTOR_ENV_FILENAME


@contextlib.contextmanager
def _importable(bundle: Path) -> Iterator[None]:
    """Make a bundle's own directory importable for exactly one entrypoint resolve.

    REQ-264 keeps an extension's implementation inside its own folder and never
    copies it into core or into the agent's capability folder, so the dotted
    entrypoint a manifest declares resolves there and nowhere else.

    The entry comes straight back off ``sys.path``. Leaving it would let one
    extension's top-level module names shadow another's — and every unrelated
    import for the rest of the process — which is a supply-chain hole dressed as
    a convenience.
    """
    entry = str(bundle)
    sys.path.insert(0, entry)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(entry)


async def resolve_secrets(
    manifest: ExtensionManifest,
    *,
    connection: str,
    store: SecretStore | None,
    caller_did: str,
) -> dict[str, Secret]:
    """Every credential this bundle declares, still wrapped, or refuse naming the gaps.

    The store is the only place a connector credential lives, so this is the only
    way one reaches an attachment. Values stay :class:`~arcagent.extension.secrets.
    Secret` all the way here and are unwrapped in :func:`build_attachment` alone.

    Args:
        manifest: What the bundle declares it needs.
        connection: The connected account, which keys the store. Credentials are
            per-connection, which is what lets one bundle back two accounts without
            either seeing the other's — and what lets two agents share one account
            without either holding a copy of its credential.
        store: Where credentials live. ``None`` is not a failure for a bundle that
            declares none — a ``cli`` connector whose binary owns its own auth needs
            no store at all — and is a refusal for one that does.
        caller_did: Recorded as the actor on every read.

    Returns:
        Field name to credential, one entry per declared secret.

    Raises:
        ExtensionError: A declared credential is not in the store. The fields are
            named and never valued: a connection serving verbs it has no credential
            for is a 401 the agent cannot read.
    """
    if not manifest.secrets:
        return {}
    if store is None:
        raise _refuse(
            "secrets",
            f"{manifest.extension.name} needs a stored credential and this deployment "
            f"has no secret store configured",
            extension=manifest.extension.name,
            connection=connection,
        )
    resolved: dict[str, Secret] = {}
    missing: list[str] = []
    for declared in manifest.secrets:
        ref = SecretRef(connection=connection, field=declared.name)
        secret = await store.get(ref, caller_did=caller_did)
        if secret is None:
            missing.append(declared.name)
        else:
            resolved[declared.name] = secret
    if missing:
        raise _refuse(
            "secrets",
            f"{connection} has no stored credential for {', '.join(missing)} — "
            f"run 'arc connector auth {connection}'",
            extension=manifest.extension.name,
            connection=connection,
            missing=missing,
        )
    return resolved


def placement_environment(
    manifest: ExtensionManifest, secrets: Mapping[str, Secret]
) -> dict[str, Secret]:
    """The child-process environment this bundle's declared placements ask for.

    One mapping, built in one place, and used by everything that starts a program on
    this connection's behalf: the attachment's own verbs and probe, and the sign-in
    check behind ``verify_command``. A second copy would let a connection whose tools
    work report itself as signed out, because the check ran without the credential the
    tools had.

    Values stay wrapped: the caller that starts the process unwraps them into that
    process's environment and nothing before it does. So a mapping held on an
    attachment, or dumped by a traceback on the way to one, still renders
    ``Secret(***)``.

    Args:
        manifest: What the bundle declares. A credential with no
            ``[secrets.placement]`` contributes nothing.
        secrets: The credentials :func:`resolve_secrets` read out of the store.

    Returns:
        Environment variable to credential, empty when the bundle places nothing.
    """
    return {
        declared.placement.variable: secrets[declared.name]
        for declared in manifest.secrets
        if declared.placement is not None and declared.name in secrets
    }


def visible_values(manifest: ExtensionManifest, secrets: Mapping[str, Secret]) -> dict[str, str]:
    """The bundle's non-sensitive fields, for the argv tokens its manifest names.

    Only ``sensitive = false`` fields, and that is the whole safety rule rather than a
    convenience: these values are written into a command line, and a command line is
    readable by every other user on the box. A credential reaches a child through
    ``[secrets.placement]`` — its environment — or through a login's stdin, and never
    through here. The manifest parser refuses a placeholder naming a sensitive field,
    so a bundle cannot ask; this makes it so a bundle could not be served if it did.
    """
    return {
        declared.name: secrets[declared.name].reveal()
        for declared in manifest.secrets
        if not declared.sensitive and declared.name in secrets
    }


def shape_supplied(plan: ConnectorPlan, values: Mapping[str, str]) -> dict[str, str]:
    """Put every supplied value into the shape its bundle declared for that field.

    One place, called by both paths a value can enter through — the install and the
    re-auth — because a rule applied on one of them is a connection that works when
    it was created and breaks when its credential is rotated.

    Args:
        plan: What the bundle declares. A field with no ``format`` is untouched.
        values: What the operator typed, keyed by field name.

    Returns:
        The same mapping with each declared field shaped. Undeclared keys are
        carried through unchanged; refusing them belongs to the caller that knows
        whether an undeclared key is an error (``reauth``) or ignorable.

    Raises:
        ExtensionError: A value cannot be put in its declared shape. The message is
            written for the person who typed it and never echoes the value.
    """
    declared = {field.name: field for field in plan.secrets}
    return {
        name: normalize(declared[name].format, name, value) if name in declared else value
        for name, value in values.items()
    }


def _unplaced_secrets(manifest: ExtensionManifest) -> list[str]:
    """Declared credentials this bundle gives no destination for.

    A ``cli`` attachment reaches its service by spawning a binary, so a credential it
    declares is deliverable only through ``[secrets.placement]``. Accepting one without
    would store a credential and deliver it nowhere — a connection that probes green
    and 401s on the first real verb.

    A field some declared command NAMES is the exception, and it is not a loophole:
    that command carries the field on its own argv or stdin, which is why one CLI
    reads its site and address from no environment variable at all and another
    requires its vault on every call. Refusing those would leave a bundle unable to
    declare the very fields its commands cannot run without. The manifest parser
    already refuses a command naming a SENSITIVE field, so nothing exempted here can
    be a credential.
    """
    delivered = manifest.fields_named_by_commands()
    return [
        declared.name
        for declared in manifest.secrets
        if declared.placement is None and declared.name not in delivered
    ]


def build_attachment(
    manifest: ExtensionManifest, bundle: Path, secrets: Mapping[str, Secret]
) -> ExtensionAttachment:
    """Build the attachment a manifest declares, or refuse the kind.

    Two kinds ship: ``cli`` runs a locally installed binary the operator was
    directed to install, and ``native`` imports the implementation the extension
    package supplies. A third party adds a kind by supplying a factory, which is
    why this function refuses an unknown kind rather than guessing at one.

    **This is where a credential is revealed, and it is the only place here.** A
    :class:`~arcagent.extension.secrets.Secret` renders ``Secret(***)`` wherever it
    is formatted, and ``reveal()`` ends that protection — so the value crosses
    exactly one boundary, from the store into the extension's own factory, with no
    log line, audit event, or refusal between the two. A placed credential is not
    unwrapped here at all: it stays a ``Secret`` until the attachment spawns the
    process it belongs to.
    """
    kind = manifest.extension.attachment
    if kind == "native":
        entrypoint = _NativeConfig.model_validate(manifest.config.get("native", {})).entrypoint
        context: dict[str, Any] = {"bundle": str(bundle)}
        context.update({name: secret.reveal() for name, secret in secrets.items()})
        with _importable(bundle):
            return NativeAttachment(entrypoint, context)
    if kind == "cli":
        unplaced = _unplaced_secrets(manifest)
        if unplaced:
            raise _refuse(
                "probe",
                f"{manifest.extension.name} declares credential(s) {', '.join(unplaced)} "
                f"with no [secrets.placement], and attaches as 'cli', which can only "
                f"deliver a credential the bundle names a destination for",
                extension=manifest.extension.name,
                attachment=kind,
                unplaced=unplaced,
            )
        declared = _CliConfig.model_validate(manifest.config.get("cli", {}))
        return CliAttachment(
            binary=declared.binary,
            commands=declared.commands,
            probe_argv=declared.probe_argv,
            install_instruction=declared.install_instruction,
            resilience=declared.resilience,
            env=placement_environment(manifest, secrets),
            values=visible_values(manifest, secrets),
        )
    raise _refuse("probe", f"unknown attachment kind {kind!r}", attachment=kind)


class _NativeConfig(BaseModel):
    """``[config.native]`` — the dotted module exposing the extension's factory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entrypoint: str


class _CliConfig(BaseModel):
    """``[config.cli]`` — the binary, its declared commands, and its resilience bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    binary: str
    commands: list[CliCommand] = Field(default_factory=list)
    probe_argv: list[str] = Field(default_factory=lambda: ["--version"])
    install_instruction: str = ""
    resilience: CliResilience = Field(default_factory=CliResilience)


# --- internals --------------------------------------------------------------


async def _verify_bundle(
    plan: ConnectorPlan,
    *,
    audit_sink: AuditSink | None,
    trusted_public_key: bytes | None,
    registry: CapabilityRegistry | None,
) -> None:
    """Run the bundle through the one component that owns the signature gate.

    Ordered before the credential write and before anything builds an attachment,
    because building one executes extension-declared code (a native attachment
    imports the extension's own module). Verifying afterwards would mean the
    supply-chain gate ran only on bundles that had already run (REQ-282), and a
    refused bundle would still have been handed a credential.

    :class:`~arcagent.extension.loader.ExtensionLoader` holds the gate — a second
    verification implemented here would be a second gate to keep in step, and the
    one that drifts is the one an attacker uses.
    """
    from arcagent.capabilities.capability_registry import CapabilityRegistry as _Registry

    loader = ExtensionLoader(
        roots=plan.extensions_root,
        registry=registry if registry is not None else _Registry(),
        tier=plan.tier,
        audit_sink=audit_sink if audit_sink is not None else NullSink(),
        trusted_public_key=trusted_public_key,
        egress_allow=plan.egress_allow,
    )
    try:
        await loader.load(plan.extension)
    except ExtensionError as exc:
        raise _refuse("verify", exc.message, extension=plan.extension) from exc


async def _write_secrets(
    plan: ConnectorPlan, *, values: Mapping[str, str], store: SecretStore, did: str
) -> list[SecretRef]:
    """Store every declared value, unwinding this call's own writes on failure.

    Shapes are applied to all of them BEFORE the first write, so a value that cannot
    work is refused with nothing yet stored and nothing to unwind — and the refusal
    reaches the operator in its own words rather than as a rolled-back install step.
    """
    shaped = shape_supplied(plan, values)
    written: list[SecretRef] = []
    for declared in plan.secrets:
        value = shaped.get(declared.name, "")
        ref = SecretRef(connection=plan.instance, field=declared.name)
        try:
            if not value:
                raise ValueError(f"no value supplied for required secret {declared.name!r}")
            await store.put(ref, value, caller_did=did)
        except (ValueError, ExtensionError) as exc:
            await _forget_secrets(written, store=store, did=did)
            raise _refuse("secrets", str(exc), secret=declared.name) from exc
        written.append(ref)
    return written


async def _forget_secrets(refs: Sequence[SecretRef], *, store: SecretStore, did: str) -> None:
    """Undo this install's credential writes. Best effort, and loud when it cannot."""
    for ref in refs:
        try:
            await store.delete(ref, caller_did=did)
        except Exception:  # reason: rollback must attempt every ref, then report
            _logger.exception("could not roll back connector secret %s", ref)


async def _probe(
    plan: ConnectorPlan, factory: AttachmentFactory | None, secrets: Mapping[str, Secret]
) -> ProbeResult:
    """Build the attachment with its credentials and prove the connection answers."""
    build = factory or build_attachment
    try:
        attachment = build(plan.manifest, plan.bundle, secrets)
        result = await attachment.probe()
    except ExtensionError as exc:
        raise _refuse("probe", exc.message, extension=plan.extension) from exc
    except Exception as exc:  # reason: any attachment failure is a refused install
        raise _refuse("probe", f"{type(exc).__name__}: {exc}", extension=plan.extension) from exc
    if not result.reachable:
        raise _refuse(
            "probe",
            f"{plan.extension} did not answer — {result.detail}",
            extension=plan.extension,
        )
    return result


def _refuse_declared_egress(plan: ConnectorPlan) -> None:
    """Refuse a bundle DECLARING a send this tier forbids from an extension (D-580).

    First of everything, before the host check and long before a credential is
    written, because the whole point of the rule is that the agent never receives
    a tool it cannot use — and a refusal that arrives after the install has
    written something is a cleanup problem rather than a refusal.
    """
    declared = ((tool.name, tool.capability_tags) for tool in plan.manifest.tools.declared)
    _refuse_egress(plan, declared, "manifest")


def _refuse_probed_egress(plan: ConnectorPlan, probe: ProbeResult) -> None:
    """Refuse a connection whose LIVE tools include a send this tier forbids (D-580).

    The manifest check reads a declaration; this reads what the connection
    actually serves, which is the only place an attachment declaring its verbs
    somewhere other than ``[[tools.declared]]`` — a CLI's commands, an MCP
    server's ``tools/list`` — becomes visible. Inside the rollback scope on
    purpose, so a refusal here takes the credential back out.
    """
    _refuse_egress(plan, ((spec.name, spec.capability_tags) for spec in probe.tools), "probe")


def _refuse_egress(
    plan: ConnectorPlan, declared: Iterator[tuple[str, Sequence[str]]], step: str
) -> None:
    """Take the one egress verdict over ``declared`` and raise if it refuses."""
    refusal = first_forbidden_egress(
        declared, tier=plan.tier, from_extension=True, egress_allow=plan.egress_allow
    )
    if refusal is not None:
        raise _refuse(step, refusal.message, extension=plan.extension, tool=refusal.tool)


__all__ = [
    "CONNECTOR_ENV_FILENAME",
    "INSTALL_STEPS",
    "AttachmentFactory",
    "ConnectorPlan",
    "InstallReport",
    "RemovalReport",
    "build_attachment",
    "connector_env_file",
    "install_connector",
    "placement_environment",
    "plan_connector",
    "remove_connector",
    "resolve_secrets",
    "shape_supplied",
]
