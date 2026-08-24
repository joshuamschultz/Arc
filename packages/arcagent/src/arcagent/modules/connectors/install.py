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

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arctrust.audit import AuditSink, NullSink
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment, ProbeResult
from arcagent.extension.catalog import MANIFEST_NAME, ExtensionCatalog
from arcagent.extension.contract_ledger import ToolContractLedger
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal
from arcagent.extension.field_formats import normalize
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.host import HostPrerequisiteDirector
from arcagent.extension.loader import ExtensionLoader
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.secrets import Secret, SecretRef, SecretStore
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore
from arcagent.modules.connectors.attachments import build_attachment
from arcagent.modules.connectors.credential_placement import placement_environment
from arcagent.modules.connectors.credential_placement import visible_values as _visible_values
from arcagent.modules.connectors.models import ConnectorPlan, InstallReport, RemovalReport
from arcagent.tools._egress_policy import first_forbidden_egress

if TYPE_CHECKING:
    from arcagent.capabilities.capability_registry import CapabilityRegistry

_logger = logging.getLogger("arcagent.modules.connectors.install")

# Kept as an established import surface while credential placement has one owner.
visible_values = _visible_values

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

    "Beside ``connections.toml``" is resolved through the same accessor that
    answers for it, never composed here: joining the root by hand kept the
    credentials flat at ``<root>/connections.env`` after the registry had moved
    into ``config/``, so every connection read its grants from one directory and
    its token from another.
    """
    from arctrust.paths import config_file

    return config_file(CONNECTOR_ENV_FILENAME, arc_dir)


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
        ExtensionError: A declared REQUIRED credential is not in the store. The
            fields are named and never valued: a connection serving verbs it has no
            credential for is a 401 the agent cannot read. A field the bundle
            declared optional is simply absent from the result.
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
    # An OAuth connector's refresh token is absent until `arc connector authorize`
    # obtains it. That is not a missing credential to refuse the build over — the
    # attachment builds without it and probes as unauthenticated until it is stored,
    # which is the honest state of a connection whose sign-in is not finished. Once
    # stored, it resolves and reaches the attachment like any other secret.
    optional = manifest.oauth.refresh_token_secret if manifest.oauth else None
    resolved: dict[str, Secret] = {}
    missing: list[str] = []
    for declared in manifest.secrets:
        ref = SecretRef(connection=connection, field=declared.name)
        secret = await store.get(ref, caller_did=caller_did)
        if secret is None:
            # A field the bundle declared optional was never stored, and its
            # absence is the answer: sqlite's empty `host` means the database is
            # on this machine. Refusing here would make the ordinary case
            # unconnectable.
            if declared.name != optional and declared.required:
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
    # An OAuth connector's refresh token is obtained by `arc connector authorize`
    # AFTER install (the code exchange), never supplied here — so it is not a
    # required value at install time. The operator supplies only the app
    # key/secret; requiring the token now would refuse the install for a value
    # nobody can produce yet, which is the exact wall the OAuth flow removes.
    managed = plan.manifest.oauth.refresh_token_secret if plan.manifest.oauth else None
    written: list[SecretRef] = []
    for declared in plan.secrets:
        if declared.name == managed:
            continue
        value = shaped.get(declared.name, "")
        # An optional field's EMPTINESS is meaningful — sqlite's empty `host`
        # means "the database is on this machine", which is the ordinary case.
        # Nothing is stored for it: an absent secret and an empty one must not be
        # two states the adapter has to tell apart.
        if not value and not declared.required:
            continue
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
