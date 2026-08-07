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

The instance reader ships alongside the writer on purpose. A component that
writes a config block nothing reads is this project's known failure mode, so
:func:`load_instances` is here and typed, and the connector runtime binds it.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arctrust.audit import AuditSink, NullSink
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ExtensionAttachment, ProbeResult
from arcagent.extension.catalog import ExtensionCatalog
from arcagent.extension.cli_attachment import CliAttachment, CliCommand, CliResilience
from arcagent.extension.host import HostPrerequisiteDirector, HostVerdict
from arcagent.extension.loader import MANIFEST_NAME, ExtensionLoader
from arcagent.extension.manifest import ExtensionManifest, SecretRequirement, load_manifest
from arcagent.extension.native_attachment import NativeAttachment
from arcagent.extension.secrets import SecretRef, SecretStore
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore
from arcagent.tools._egress_policy import first_forbidden_egress
from arcagent.utils.toml_writer import dumps_toml

if TYPE_CHECKING:
    from arcagent.capabilities.capability_registry import CapabilityRegistry

_logger = logging.getLogger("arcagent.modules.connectors.install")

#: The agent-config table connected instances live in. One bundle can back several
#: distinctly named instances bound to different accounts.
CONFIG_TABLE = "extensions"

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

#: How a manifest's declared attachment kind becomes something that can be probed.
AttachmentFactory = Callable[[ExtensionManifest, Path], ExtensionAttachment]


def _refuse(step: str, message: str, **details: Any) -> ExtensionError:
    """Build the one error shape every failure raises, always naming its step."""
    return ExtensionError(
        code="CONNECTOR_INSTALL_FAILED",
        message=f"{step}: {message}",
        details={"step": step, **details},
    )


class InstanceConfig(BaseModel):
    """One ``[extensions.<instance>]`` block: which bundle, and how gated.

    ``extra="forbid"`` so a hand-edited key is a loud error rather than a setting
    the operator believes is in force and which nothing reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    extension: str
    approval: str = "outbound"


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
    extensions_root: Path
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
    extensions_root: Path,
    extension: str,
    instance: str,
    tier: Tier,
    audit_sink: AuditSink,
    director: HostPrerequisiteDirector | None = None,
    egress_allow: Sequence[str] = (),
) -> ConnectorPlan:
    """Run the three read-only steps: resolve the bundle, parse it, inspect the host.

    Args:
        extensions_root: The directory every loadable bundle lives directly inside.
        extension: The bundle name the operator asked for.
        instance: The name this connected account will be known by.
        tier: The deployment tier — decides the unlisted-bundle verdict, the
            unbounded-allowlist refusal, and the egress verdict.
        audit_sink: Where the catalog records its verdicts.
        director: Host detection. Injectable so a test never depends on what
            happens to be installed on the machine running it.
        egress_allow: ``tools.policy.egress_allow`` — the tools this operator
            permits to send data out (D-580). Read from the agent config by
            :func:`load_egress_allow`.

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
    catalog = ExtensionCatalog(root=Path(extensions_root), tier=tier, audit_sink=audit_sink)
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
        extensions_root=Path(extensions_root),
        egress_allow=tuple(egress_allow),
    )


async def install_connector(
    plan: ConnectorPlan,
    *,
    agent_dir: Path,
    agent: str,
    secret_values: Mapping[str, str],
    store: SecretStore,
    caller_did: str,
    attachment_factory: AttachmentFactory | None = None,
    state: ConnectionStateStore | None = None,
    audit_sink: AuditSink | None = None,
    trusted_public_key: bytes | None = None,
    registry: CapabilityRegistry | None = None,
) -> InstallReport:
    """Finish the install: host, verify, secrets, probe, and only then persist.

    Args:
        plan: The result of :func:`plan_connector`.
        agent_dir: The agent directory holding ``arcagent.toml``.
        agent: The agent's slug, which keys the secret store.
        secret_values: One value per declared secret, collected by the surface.
        store: Where credentials are written — and nowhere else.
        caller_did: Recorded as the actor on every credential operation.
        attachment_factory: How the manifest becomes something probeable.
        state: Operational state, when the data plane is available.
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

    written = await _write_secrets(
        plan, agent=agent, values=secret_values, store=store, did=caller_did
    )
    try:
        probe = await _probe(plan, attachment_factory)
        _refuse_probed_egress(plan, probe)
    except BaseException:
        await _forget_secrets(written, store=store, did=caller_did)
        raise

    write_instance(
        agent_dir,
        plan.instance,
        InstanceConfig(extension=plan.extension, approval=plan.approval_mode),
    )
    if state is not None:
        await state.create(
            ConnectionRecord(agent=agent, instance=plan.instance, health="healthy"),
            actor_did=caller_did,
        )
    return InstallReport(
        instance=plan.instance,
        extension=plan.extension,
        tools=tuple(spec.name for spec in probe.tools),
        detail=probe.detail,
    )


async def remove_connector(
    *,
    agent_dir: Path,
    agent: str,
    instance: str,
    store: SecretStore,
    caller_did: str,
    secret_fields: Sequence[str],
    state: ConnectionStateStore | None = None,
) -> RemovalReport:
    """Drop one connected account: its credentials, its config block, its state.

    Removing something that was never installed is reported, not raised: an
    operator cleaning up after a failed install must not be blocked by a step that
    already has nothing to do.
    """
    removed: list[str] = []
    for field in secret_fields:
        ref = SecretRef(agent=agent, instance=instance, field=field)
        if await store.delete(ref, caller_did=caller_did):
            removed.append(field)

    removed_state = False
    if state is not None:
        removed_state = await state.forget(agent, instance, actor_did=caller_did)

    return RemovalReport(
        instance=instance,
        removed_secrets=tuple(removed),
        removed_config=delete_instance(agent_dir, instance),
        removed_state=removed_state,
    )


# --- the agent-config seam: both halves ------------------------------------


def load_instances(agent_dir: Path) -> dict[str, InstanceConfig]:
    """Read every ``[extensions.<instance>]`` block the agent config declares.

    Args:
        agent_dir: The agent directory holding ``arcagent.toml``.

    Returns:
        Instance name to its configuration. Empty when the agent has no
        connections, which is the ordinary case and never an error.

    Raises:
        ExtensionError: A block is malformed. Refused loudly rather than skipped,
            because a connection an operator configured and the agent silently
            ignores is worse than one that fails to start.
    """
    blocks = _read_config(agent_dir).get(CONFIG_TABLE, {})
    if not isinstance(blocks, dict):
        raise _refuse("persist", f"[{CONFIG_TABLE}] in {agent_dir} is not a table")
    instances: dict[str, InstanceConfig] = {}
    for name, block in blocks.items():
        try:
            instances[name] = InstanceConfig.model_validate(block)
        except ValidationError as exc:
            raise _refuse("persist", f"[{CONFIG_TABLE}.{name}] is invalid — {exc}") from exc
    return instances


def load_egress_allow(agent_dir: Path) -> tuple[str, ...]:
    """Read ``[tools.policy] egress_allow`` — the tools permitted to send out (D-580).

    Args:
        agent_dir: The agent directory holding ``arcagent.toml``.

    Returns:
        The permitted tool names. Empty when unset, which is the fail-closed
        posture: an operator who has permitted no send has permitted no send.
    """
    tools = _read_config(agent_dir).get("tools", {})
    policy = tools.get("policy", {}) if isinstance(tools, dict) else {}
    allow = policy.get("egress_allow", []) if isinstance(policy, dict) else []
    if not isinstance(allow, list):
        raise _refuse("persist", f"[tools.policy] egress_allow in {agent_dir} is not a list")
    return tuple(str(name) for name in allow)


def write_instance(agent_dir: Path, instance: str, config: InstanceConfig) -> None:
    """Add or replace one instance block, leaving the rest of the config alone."""
    document = _read_config(agent_dir)
    table = document.setdefault(CONFIG_TABLE, {})
    if not isinstance(table, dict):
        raise _refuse("persist", f"[{CONFIG_TABLE}] in {agent_dir} is not a table")
    table[instance] = config.model_dump()
    _write_config(agent_dir, document)


def delete_instance(agent_dir: Path, instance: str) -> bool:
    """Remove one instance block. False when there was nothing to remove."""
    document = _read_config(agent_dir)
    table = document.get(CONFIG_TABLE)
    if not isinstance(table, dict) or instance not in table:
        return False
    table.pop(instance)
    if not table:
        document.pop(CONFIG_TABLE)
    _write_config(agent_dir, document)
    return True


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


def build_attachment(manifest: ExtensionManifest, bundle: Path) -> ExtensionAttachment:
    """Build the attachment a manifest declares, or refuse the kind.

    Two kinds ship: ``cli`` runs a locally installed binary the operator was
    directed to install, and ``native`` imports the implementation the extension
    package supplies. A third party adds a kind by supplying a factory, which is
    why this function refuses an unknown kind rather than guessing at one.
    """
    kind = manifest.extension.attachment
    if kind == "native":
        entrypoint = _NativeConfig.model_validate(manifest.config.get("native", {})).entrypoint
        with _importable(bundle):
            return NativeAttachment(entrypoint, {"bundle": str(bundle)})
    if kind == "cli":
        declared = _CliConfig.model_validate(manifest.config.get("cli", {}))
        return CliAttachment(
            binary=declared.binary,
            commands=declared.commands,
            probe_argv=declared.probe_argv,
            install_instruction=declared.install_instruction,
            resilience=declared.resilience,
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
        extensions_root=plan.extensions_root,
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
    plan: ConnectorPlan,
    *,
    agent: str,
    values: Mapping[str, str],
    store: SecretStore,
    did: str,
) -> list[SecretRef]:
    """Store every declared credential, unwinding this call's own writes on failure."""
    written: list[SecretRef] = []
    for declared in plan.secrets:
        value = values.get(declared.name, "")
        ref = SecretRef(agent=agent, instance=plan.instance, field=declared.name)
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


async def _probe(plan: ConnectorPlan, factory: AttachmentFactory | None) -> ProbeResult:
    """Build the attachment and prove the connection answers."""
    build = factory or build_attachment
    try:
        attachment = build(plan.manifest, plan.bundle)
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


def _config_path(agent_dir: Path) -> Path:
    path = Path(agent_dir) / "arcagent.toml"
    if not path.is_file():
        raise _refuse("persist", f"no arcagent.toml at {path} — is that an agent directory?")
    return path


def _read_config(agent_dir: Path) -> dict[str, Any]:
    path = _config_path(agent_dir)
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _refuse("persist", f"{path} — {exc}") from exc


def _write_config(agent_dir: Path, document: dict[str, Any]) -> None:
    _config_path(agent_dir).write_text(dumps_toml(document), encoding="utf-8")


__all__ = [
    "CONFIG_TABLE",
    "INSTALL_STEPS",
    "AttachmentFactory",
    "ConnectorPlan",
    "InstallReport",
    "InstanceConfig",
    "RemovalReport",
    "build_attachment",
    "delete_instance",
    "install_connector",
    "load_egress_allow",
    "load_instances",
    "plan_connector",
    "remove_connector",
    "write_instance",
]
