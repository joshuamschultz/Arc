"""SPEC-062 T-915 + T-917 — the tests that decide whether this spec succeeded.

COMP-021, serving REQ-278/280 (the mechanism is general) and REQ-284/285/286
(nothing attached is load-bearing).

**T-915 is the governing test**, and it is in two halves because the claim has two
halves. The whole spec exists to make one sentence true: a new connection can be
added with zero core files modified. So a reference extension that implements only
:class:`~arcagent.extension.attachment.ExtensionAttachment` — living outside every
package source tree, naming no vendor, touching no network — must (1) install through
the real install surface, resolving to the implementation it ships **inside its own
bundle**, and (2) show up on a started agent as individually named, governed verbs
that dispatch back to it. Half one is the extension being reachable at all; half two
is the extension being *live*. The structural half of the claim (the fixture is
outside core, and core names none of it) is asserted in
``tests/architecture/test_extension_mechanism.py``.

Three design choices matter more than they look:

* **A real ``ArcAgent``, not a hand-built registry.** The envelope a connector call
  must survive — schema validation, the signed ``ToolCall``, the policy pipeline
  under the admission lock, the trifecta ledger, the timeout, the audit emission —
  only exists when the agent wires it. A registry constructed in the test with
  ``policy_pipeline=None`` would prove the bridge registers a name and nothing about
  whether the call is governed.
* **No attachment is ever injected.** Both halves let Arc resolve the extension from
  its manifest, because an agent that only reaches an extension a test handed it has
  proved nothing. Nothing here is patched or stubbed.
* **A control that executes the fixture through the bridge with no loader at all.**
  If the governing tests fail while the control passes, the defect is provably in
  Arc's resolution and activation path and not in the fixture, the bridge, or the
  envelope. RED is only useful when it points somewhere.

One test is not about the mechanism being general but about it being safe: probing
an attachment *executes* third-party code (a native attachment imports it; a CLI
attachment spawns it), so an unsigned bundle above personal tier must be refused
before that happens (REQ-282).

**T-917** asserts the other direction: with no extensions and with no optional
modules, a real agent starts and dispatches a real tool; ``connectors`` is discovered
but inert until configured; and configuration left behind by a removal does not
break startup. Every one of these drives a real agent through ``startup()`` —
[[feedback_producers_unwired_pattern]] is what a mocked removability test buys you.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from arcrun import ToolContext
from arctrust.audit import AuditEvent

from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    TelemetryConfig,
    load_config,
)
from arcagent.core.errors import ExtensionError
from arcagent.core.module_discovery import active_modules, module_statuses
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import ToolRegistry, ToolTransport
from arcagent.extension.attachment import ExtensionAttachment
from arcagent.extension.bridge import CapabilityBridge
from arcagent.extension.loader import ExtensionLoader
from arcagent.extension.secrets import LocalFileSecretBackend, SecretStore
from arcagent.modules.connectors.install import (
    InstanceConfig,
    install_connector,
    load_instances,
    plan_connector,
    write_instance,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_extension"

#: The bundle name the reference extension installs under. Must satisfy the catalog's
#: name grammar; it is the operator-facing name and nothing in core knows it.
_BUNDLE = "reference_service"

#: The connected-account name one bundle is installed under.
_INSTANCE = "primary"

#: The agent slug that keys the secret store (its grammar forbids a hyphen).
_AGENT_SLUG = "conformance_agent"

#: Recorded as the actor on every credential operation the install performs.
_CALLER = "did:arc:testorg:executor/conformance"

_ECHO = "reference_echo"
_STORE = "reference_store"


class _RecordingSink:
    """A real audit sink that keeps events. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


# --- installing the reference extension -------------------------------------


def _install(root: Path) -> Path:
    """Copy the reference bundle into an extensions root, as an install would."""
    root.mkdir(parents=True, exist_ok=True)
    bundle = root / _BUNDLE
    shutil.copytree(_FIXTURE_DIR, bundle, ignore=shutil.ignore_patterns("__pycache__"))
    return bundle


def _import_fixture_module() -> Any:
    """Import the fixture's implementation straight off disk.

    Used only by the isolation test: it is how a test proves the fixture itself is
    sound when the mechanism's own resolution path is what is broken.
    """
    path = _FIXTURE_DIR / "reference_attachment.py"
    spec = importlib.util.spec_from_file_location("_reference_extension_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- a real agent ------------------------------------------------------------


def _config(tmp_path: Path, *, modules: dict[str, ModuleEntry] | None = None) -> ArcAgentConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    return ArcAgentConfig(
        agent=AgentConfig(
            name="conformance-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=10000),
        modules=modules or {},
    )


async def _started_agent(config: ArcAgentConfig, tmp_path: Path) -> ArcAgent:
    agent = ArcAgent(config=config, config_path=tmp_path / "arcagent.toml")
    await agent.startup()
    return agent


def _tool_context() -> ToolContext:
    return ToolContext(
        run_id=str(uuid.uuid4()),
        tool_call_id=str(uuid.uuid4()),
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )


def _registry(agent: ArcAgent) -> ToolRegistry:
    """The started agent's own tool registry — the real envelope, never a stand-in."""
    registry = agent._tool_registry
    assert registry is not None, "agent has no tool registry; startup did not complete"
    return registry


def _write_agent_toml(agent_dir: Path, *, connectors_enabled: bool, extra: str = "") -> Path:
    """Write a real ``arcagent.toml``, because the install path writes into one too."""
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    lines = [
        "[agent]",
        'name = "conformance-agent"',
        'org = "testorg"',
        'type = "executor"',
        f'workspace = "{workspace}"',
        "",
        "[llm]",
        'model = "test/model"',
        "",
        "[identity]",
        'did = ""',
        f'key_dir = "{agent_dir / "keys"}"',
        'vault_path = ""',
    ]
    if connectors_enabled:
        lines += ["", "[modules.connectors]", "enabled = true"]
    if extra:
        lines += ["", extra]
    path = agent_dir / "arcagent.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _dispatch(agent: ArcAgent, name: str, args: dict[str, Any]) -> str:
    """Call one registered tool through the agent's own dispatch envelope."""
    tools = {tool.name: tool for tool in _registry(agent).to_arcrun_tools()}
    assert name in tools, f"{name!r} is not registered; have {sorted(tools)}"
    return await tools[name].execute(args, _tool_context())


async def _smoke(agent: ArcAgent) -> None:
    """The agent's smoke path: it has tools, and a real one dispatches and answers."""
    assert agent._started
    assert agent._identity is not None and agent._identity.can_sign
    assert "ls" in _registry(agent).tools, "the builtin toolset did not load"
    # The dispatch below is only worth anything if it is governed: a registry with no
    # pipeline would prove the agent starts, not that its envelope survived removal.
    assert _registry(agent)._policy_pipeline is not None

    result = await _dispatch(agent, "ls", {})

    assert "seed.txt" in result


def _loader(root: Path, registry: CapabilityRegistry, sink: _RecordingSink) -> ExtensionLoader:
    return ExtensionLoader(
        roots=[root],
        registry=registry,
        tier=Tier.PERSONAL,
        audit_sink=sink,
        trusted_public_key=None,
    )


# =============================================================================
# T-915 — a new connection is added with zero core files modified
# =============================================================================


def test_the_reference_extension_implements_only_the_hook() -> None:
    """The fixture is sound: its factory returns something that satisfies the contract.

    First, so that any failure below is provably about Arc's mechanism rather than
    about the fixture this whole test file is written around.
    """
    module = _import_fixture_module()

    attachment = module.build_native_attachment({})

    assert isinstance(attachment, ExtensionAttachment)


async def test_the_reference_extension_answers_through_the_hook_alone() -> None:
    """And it really works when driven directly — no Arc machinery involved."""
    module = _import_fixture_module()
    attachment = module.build_native_attachment({})

    probe = await attachment.probe()
    specs = await attachment.describe_tools()
    result = await attachment.invoke(_ECHO, {"message": "hello"})

    assert probe.reachable
    assert sorted(spec.name for spec in specs) == [_ECHO, _STORE]
    assert result.content == "reference echo: hello"


async def test_the_reference_bundle_loads_through_the_real_loader(tmp_path: Path) -> None:
    """An extension that ships only the hook must survive the real trust gate.

    Nothing here is special-cased: the bundle goes into the extensions root and is
    loaded by the component every extension is loaded by.
    """
    root = tmp_path / "extensions"
    _install(root)
    sink = _RecordingSink()

    loaded = await _loader(root, CapabilityRegistry(), sink).load(_BUNDLE)

    assert loaded.name == _BUNDLE
    assert loaded.manifest.extension.attachment == "native"
    assert [tag.name for tag in loaded.requirements] == ["reference_token"]


def test_the_reference_extension_plans_cleanly_on_the_real_install_surface(
    tmp_path: Path,
) -> None:
    """The read-only half of an install: resolve, parse, and inspect the host.

    Ahead of the two tests below so a failure there is never ambiguous with a
    manifest the install surface could not read in the first place.
    """
    root = tmp_path / "extensions"
    _install(root)

    plan = plan_connector(
        extensions_root=[root],
        extension=_BUNDLE,
        instance=_INSTANCE,
        tier=Tier.PERSONAL,
        audit_sink=_RecordingSink(),
    )

    assert plan.extension == _BUNDLE
    assert plan.unsatisfied_host == ()
    assert [secret.name for secret in plan.secrets] == ["reference_token"]


async def test_installing_the_reference_extension_reaches_its_own_implementation(
    tmp_path: Path,
) -> None:
    """GOVERNING TEST, half one (REQ-278/264): the install path builds the real thing.

    No attachment factory is injected. The install surface must resolve the manifest's
    declared entrypoint to the implementation the extension ships **inside its own
    bundle** — that is what REQ-264 means by "referenced from the agent's tools" — and
    probe it. If the only reachable implementation is one already installed into the
    interpreter's own environment, an extension is not a folder a third party can hand
    over and an operator can delete.
    """
    root = tmp_path / "extensions"
    _install(root)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    # A real agent directory, because the install writes its instance block into a real
    # ``arcagent.toml``. Persist must refuse a directory that holds none rather than
    # inventing a config with no ``[agent]`` or ``[llm]`` — an agent could not start
    # from that — so the fixture supplies the file instead of the code relaxing.
    _write_agent_toml(agent_dir, connectors_enabled=False)
    store = SecretStore(LocalFileSecretBackend(agent_dir / "arc.env"))
    plan = plan_connector(
        extensions_root=[root],
        extension=_BUNDLE,
        instance=_INSTANCE,
        tier=Tier.PERSONAL,
        audit_sink=_RecordingSink(),
    )

    report = await install_connector(
        plan,
        agent_dir=agent_dir,
        agent=_AGENT_SLUG,
        secret_values={"reference_token": "unused"},
        store=store,
        caller_did=_CALLER,
    )

    assert sorted(report.tools) == [_ECHO, _STORE]


async def test_a_started_agent_serves_the_tools_of_an_installed_connection(
    tmp_path: Path,
) -> None:
    """GOVERNING TEST, half two (REQ-266/267): an installed connection is a live toolset.

    An install that persists configuration and never reaches the agent is the exact
    producers-unwired shape this project keeps shipping: every install test green, and
    the agent holding no new verb. So this drives the whole path — install writes the
    instance, an agent starts against that same directory with the connector module
    enabled, and the extension's verbs must be registered under their own names and
    dispatch to the extension's own implementation through the agent's real envelope.

    Nothing is injected. Install and startup each build their own attachment from the
    manifest through the real resolution path, which is the whole point: the agent must
    reach the extension on its own, not because a test handed it an object.

    The proof that the calls landed in the extension's own code is a round trip: write
    through one verb, read the same value back through the other. That says a live,
    stateful object inside the extension served both — without assuming *which* object,
    which no correct implementation could guarantee. ``arc connector add`` and the agent
    are different processes in production, so install-time and startup-time attachments
    are necessarily distinct, and an assertion keyed to instance identity could only be
    satisfied by a test-only seam in production code.
    """
    root = tmp_path / "extensions"
    _install(root)
    config_path = _write_agent_toml(tmp_path, connectors_enabled=True)
    store = SecretStore(LocalFileSecretBackend(tmp_path / "arc.env"))
    plan = plan_connector(
        extensions_root=[root],
        extension=_BUNDLE,
        instance=_INSTANCE,
        tier=Tier.PERSONAL,
        audit_sink=_RecordingSink(),
    )
    await install_connector(
        plan,
        agent_dir=tmp_path,
        agent=_AGENT_SLUG,
        secret_values={"reference_token": "unused"},
        store=store,
        caller_did=_CALLER,
    )
    assert _INSTANCE in load_instances(tmp_path), "the install did not persist the instance"
    # The manifest's default gates every outbound call on a signed operator grant, which
    # is correct (REQ-274) and is what the approval tests exercise. Relax it for this one
    # connected account — the operator action REQ-274 explicitly permits — so this test
    # stays pointed at activation rather than blocking on the human gate.
    write_instance(tmp_path, _INSTANCE, InstanceConfig(extension=_BUNDLE, approval="none"))

    agent = ArcAgent(config=load_config(config_path), config_path=config_path)
    await agent.startup()
    try:
        registered = _registry(agent).tools

        # REQ-266 — the real verbs, each under its own name, never one dispatcher.
        assert {_ECHO, _STORE} <= set(registered), (
            "an installed connection contributed no tools to the started agent: "
            f"nothing attaches at startup (REQ-266/267). Registered: {sorted(registered)}"
        )
        # REQ-269 — classification survived translation in both directions.
        assert _registry(agent).get_classification(_ECHO) == "read_only"
        assert _registry(agent).get_classification(_STORE) == "state_modifying"

        # REQ-267 — the calls ride the agent's own envelope and reach the service.
        stored = await _dispatch(agent, _STORE, {"key": "round", "value": "trip"})
        echoed = await _dispatch(agent, _ECHO, {"message": "round"})

        assert "reference stored round" in stored
        assert "reference echo: trip" in echoed, (
            "the read did not return what the write stored, so the dispatches never "
            "reached one live object inside the extension's own implementation"
        )
    finally:
        await agent.shutdown()


async def test_an_unsigned_bundle_is_verified_before_any_of_its_code_runs(
    tmp_path: Path,
) -> None:
    """REQ-282/281 — verification precedes execution, or the signature design is decor.

    Probing is not a read: a native attachment *imports* the extension's module and a
    CLI attachment *spawns* the extension's binary, so the probe is the first moment
    third-party code executes. Above personal tier an unsigned bundle must be refused
    before that happens — and before its credentials are written.

    The factory records whether it was reached. Recording rather than asserting on an
    exception matters: the install can fail for unrelated reasons and still have
    executed the bundle, which would let a broken gate look like a working one.
    """
    root = tmp_path / "extensions"
    _install(root)  # unsigned: no .arcsig sidecars are written
    built: list[str] = []

    def _factory(manifest: Any, bundle: Path) -> Any:
        built.append(str(bundle))
        return _import_fixture_module().build_native_attachment({})

    plan = plan_connector(
        extensions_root=[root],
        extension=_BUNDLE,
        instance=_INSTANCE,
        tier=Tier.ENTERPRISE,
        audit_sink=_RecordingSink(),
    )
    with contextlib.suppress(ExtensionError):
        await install_connector(
            plan,
            agent_dir=tmp_path,
            agent=_AGENT_SLUG,
            secret_values={"reference_token": "unused"},
            store=SecretStore(LocalFileSecretBackend(tmp_path / "arc.env")),
            caller_did=_CALLER,
            attachment_factory=_factory,
        )

    assert built == [], (
        "an UNSIGNED bundle's attachment was built and probed at enterprise tier: "
        "the install path executes extension-declared code before anything verifies "
        "the bundle (REQ-282). ExtensionLoader holds the only signature gate and the "
        "install path never calls it."
    )


async def test_the_same_bridge_and_envelope_execute_the_fixture_attachment_directly(
    tmp_path: Path,
) -> None:
    """Isolation control for the governing test — deliberately skips ONLY the loader.

    Same fixture, same bridge, same real agent registry, same dispatch envelope; the
    attachment is built by importing the fixture's own factory instead of by loading
    the bundle. If this passes while the test above fails, the defect is in the
    bundle→attachment seam and nowhere else.
    """
    module = _import_fixture_module()
    attachment = module.build_native_attachment({})
    agent = await _started_agent(_config(tmp_path), tmp_path)
    try:
        report = CapabilityBridge(
            registry=_registry(agent),
            attachment=attachment,
            transport=ToolTransport.NATIVE,
            source=f"extension:{_BUNDLE}",
            allow=[_ECHO, _STORE],
        ).register(await attachment.describe_tools())

        assert sorted(report.registered) == [_ECHO, _STORE]

        echoed = await _dispatch(agent, _ECHO, {"message": "hello"})

        assert "reference echo: hello" in echoed
        assert attachment.calls == [(_ECHO, {"message": "hello"})]
    finally:
        await agent.shutdown()


async def test_loading_leaves_the_implementation_inside_the_extension_folder(
    tmp_path: Path,
) -> None:
    """REQ-264 — an extension's code is referenced where it lives, never copied out.

    Copying implementation into the agent's capability folder would make removal a
    hunt rather than a directory delete, and would put third-party code under a root
    whose trust posture was decided for the agent's own capabilities.
    """
    root = tmp_path / "extensions"
    bundle = _install(root)
    agent_capabilities = tmp_path / "capabilities"
    agent_capabilities.mkdir()

    loaded = await _loader(root, CapabilityRegistry(), _RecordingSink()).load(_BUNDLE)

    assert (bundle / "reference_attachment.py").is_file()
    assert list(agent_capabilities.rglob("reference_attachment.py")) == []
    assert loaded.path.resolve() == bundle.resolve()


# =============================================================================
# T-917 — removability: nothing attached is load-bearing
# =============================================================================


async def test_an_agent_with_no_extensions_installed_starts_and_passes_smoke(
    tmp_path: Path,
) -> None:
    """REQ-284 — with every extension removed, the agent is still an agent."""
    agent = await _started_agent(_config(tmp_path), tmp_path)
    try:
        await _smoke(agent)
    finally:
        await agent.shutdown()


async def test_an_agent_with_no_optional_modules_enabled_starts_and_passes_smoke(
    tmp_path: Path,
) -> None:
    """REQ-285 — with every optional module removed, the agent is still an agent."""
    config = _config(tmp_path)
    assert active_modules(config) == []

    agent = await _started_agent(config, tmp_path)
    try:
        await _smoke(agent)
    finally:
        await agent.shutdown()


def test_the_connectors_module_is_discovered_but_disabled_by_default(tmp_path: Path) -> None:
    """REQ-286 — present, listable, and inert until an operator says otherwise."""
    statuses = module_statuses(_config(tmp_path))

    assert "connectors" in statuses, "the connector module folder is not discoverable"
    assert statuses["connectors"].discovered
    assert not statuses["connectors"].enabled


async def test_an_agent_without_connectors_configuration_gains_no_new_tools(
    tmp_path: Path,
) -> None:
    """REQ-286 — an existing agent gains no behaviour from a module it never enabled."""
    baseline = await _started_agent(_config(tmp_path), tmp_path / "a")
    try:
        tools = set(_registry(baseline).tools)
    finally:
        await baseline.shutdown()

    assert "connectors" not in tools
    assert not [name for name in tools if name.startswith("connector")]
    assert not [
        name
        for name, tool in _registry(baseline).tools.items()
        if str(tool.source).startswith("extension:")
    ]


async def test_an_enabled_connectors_module_with_nothing_installed_still_starts(
    tmp_path: Path,
) -> None:
    """REQ-284 — enabling the host module with every extension removed is not a failure."""
    config = _config(tmp_path, modules={"connectors": ModuleEntry(enabled=True)})

    agent = await _started_agent(config, tmp_path)
    try:
        assert "connectors" in active_modules(config)
        await _smoke(agent)
    finally:
        await agent.shutdown()


async def test_residual_configuration_after_removal_does_not_break_startup(
    tmp_path: Path,
) -> None:
    """REQ-284 — configuration left behind by a removal must not brick the agent.

    An operator removes an extension by deleting its bundle. The stale
    ``[extensions.*]`` block and the module entry that hosted it stay in the file
    until someone tidies up, and neither may be the difference between an agent that
    starts and one that does not.
    """
    config_path = _write_agent_toml(
        tmp_path,
        connectors_enabled=True,
        extra="\n".join(
            [
                "[extensions.removed_service]",
                'extension = "removed_service"',
                'approval = "outbound"',
                "",
                "[modules.gone_module]",
                "enabled = true",
            ]
        ),
    )

    config = load_config(config_path)
    statuses = module_statuses(config)
    assert not statuses["gone_module"].discovered
    assert not statuses["gone_module"].enabled

    agent = ArcAgent(config=config, config_path=config_path)
    await agent.startup()
    try:
        await _smoke(agent)
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("module_name", ["connectors"])
def test_a_module_folder_that_is_absent_is_never_enabled(tmp_path: Path, module_name: str) -> None:
    """REQ-285 — removing a module folder removes the module, it does not break config.

    Discovery is folder-driven, so an empty modules directory is exactly the
    "all optional modules removed" state, and it must report cleanly rather than
    raise.
    """
    empty_modules = tmp_path / "modules"
    empty_modules.mkdir()
    config = _config(tmp_path, modules={module_name: ModuleEntry(enabled=True)})

    statuses = module_statuses(config, empty_modules)

    assert not statuses[module_name].discovered
    assert not statuses[module_name].enabled
    assert active_modules(config, empty_modules) == []
