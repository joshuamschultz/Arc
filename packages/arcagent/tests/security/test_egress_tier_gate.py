"""D-580 — egress is gated by tier and tool ORIGIN, and refused at INSTALL and LOAD.

One predicate decides whether a tool may send data out, from two signals that
already exist: the ``external_comms`` trifecta leg resolved from the tool's
declared ``capability_tags``, and the tool's origin — is this an extension, or an
operator-signed capability?

The assertions here are deliberately about ABSENCE, not about exceptions. A rule
enforced at call time would produce exactly the silent mid-turn failure this
decision exists to prevent, so every load-path test asserts that a forbidden
egress tool is **never in the registry** — not that calling it raises. A test
that invoked the tool and caught an error would pass against the wrong design.

Install-path tests assert the refusal is raised before anything is written, and
that the message names both the tool and the tier, because an operator who cannot
tell which verb was refused or what tier refused it has no way to act on it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from arctrust.audit import AuditEvent
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.capability_registry import CapabilityRegistry, ToolEntry
from arcagent.core.agent_lifecycle import bridge_capability_tools_to_registry
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    ToolConfig,
    ToolsConfig,
)
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.extension import catalog as catalog_module
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.bridge import CapabilityBridge
from arcagent.extension.loader import ExtensionLoader
from arcagent.extension.secrets import LocalFileSecretBackend, SecretStore
from arcagent.modules.connectors.install import install_connector, plan_connector
from arcagent.tools._decorator import ToolMetadata

_SEND = "acme_send_message"
_READ = "acme_list_issues"
_EXTENSION = "acme_tickets"

#: The declared tag that resolves to the ``external_comms`` leg. Written as the
#: tag, never as the leg, so this test exercises the same resolution the gate does.
_EGRESS_TAG = "network_egress"


class RecordingSink:
    """Audit sink that keeps every event. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _vetted_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the test bundle on the vetted-upstream allowlist at every tier.

    That allowlist is a different control: federal refuses an unlisted bundle
    before the manifest is even read. Leaving it in force would let every federal
    test here pass with no egress gate implemented at all.
    """
    monkeypatch.setitem(
        catalog_module.OFFICIAL_EXTENSIONS, _EXTENSION, "https://example.invalid/acme"
    )


# --- the load path: a forbidden tool never enters the registry ---------------


def _registry(
    *, tier: Tier, egress_allow: list[str] | None = None, deny: list[str] | None = None
) -> ToolRegistry:
    config = ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
        tools=ToolsConfig(
            policy=ToolConfig(
                deny=list(deny or []),
                egress_allow=list(egress_allow or []),
            )
        ),
    )
    return ToolRegistry(
        config=config.tools, bus=MagicMock(), telemetry=MagicMock(), tier=tier.value
    )


def _tool(name: str, *, tags: list[str], source: str = "", scan_root: str = "") -> RegisteredTool:
    async def execute(**kwargs: Any) -> str:
        return "sent"

    return RegisteredTool(
        name=name,
        description=f"Test tool {name}",
        input_schema={"type": "object", "properties": {}},
        transport=ToolTransport.NATIVE,
        execute=execute,
        source=source,
        scan_root=scan_root,
        capability_tags=tags,
    )


def _extension_tool(name: str, *, tags: list[str]) -> RegisteredTool:
    return _tool(
        name, tags=tags, source=f"extension:{_EXTENSION}", scan_root=f"extension:{_EXTENSION}"
    )


def _signed_capability(name: str, *, tags: list[str]) -> RegisteredTool:
    """A tool the operator placed and signed into the agent's capabilities folder."""
    return _tool(name, tags=tags, source="/agents/sales/capabilities/send.py", scan_root="agent")


class TestPersonalTier:
    """Personal: yes, any origin. Ease comes from unbreakable defaults, not setup."""

    def test_an_extension_egress_tool_registers(self) -> None:
        registry = _registry(tier=Tier.PERSONAL)
        registry.register(_extension_tool(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND in registry.tools


class TestEnterpriseTier:
    """Enterprise: only a tool the operator named in ``tools.policy.egress_allow``."""

    def test_an_unlisted_egress_tool_is_never_registered(self) -> None:
        registry = _registry(tier=Tier.ENTERPRISE)
        registry.register(_extension_tool(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND not in registry.tools

    def test_a_listed_egress_tool_registers(self) -> None:
        registry = _registry(tier=Tier.ENTERPRISE, egress_allow=[_SEND])
        registry.register(_extension_tool(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND in registry.tools

    def test_deny_beats_an_egress_allow_entry(self) -> None:
        """The gate can only SUBTRACT — an egress permission never re-admits a deny."""
        registry = _registry(tier=Tier.ENTERPRISE, egress_allow=[_SEND], deny=[_SEND])
        registry.register(_extension_tool(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND not in registry.tools


class TestFederalTier:
    """Federal: an operator-signed capability may send; an extension never may."""

    def test_an_extension_egress_tool_is_never_registered(self) -> None:
        registry = _registry(tier=Tier.FEDERAL)
        registry.register(_extension_tool(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND not in registry.tools

    def test_an_operator_signed_capability_with_the_same_tag_registers(self) -> None:
        """Same egress tag, different origin — origin is the whole federal rule."""
        registry = _registry(tier=Tier.FEDERAL)
        registry.register(_signed_capability(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND in registry.tools

    def test_an_egress_allow_entry_cannot_re_admit_an_extension(self) -> None:
        """A config list must not buy at federal what only a signature buys."""
        registry = _registry(tier=Tier.FEDERAL, egress_allow=[_SEND])
        registry.register(_extension_tool(_SEND, tags=[_EGRESS_TAG]))
        assert _SEND not in registry.tools


@pytest.mark.parametrize("tier", [Tier.PERSONAL, Tier.ENTERPRISE, Tier.FEDERAL])
def test_a_read_only_extension_tool_registers_at_every_tier(tier: Tier) -> None:
    """The gate speaks only to sending. Reading is untouched at every tier."""
    registry = _registry(tier=tier)
    registry.register(_extension_tool(_READ, tags=["file_read"]))
    assert _READ in registry.tools


# --- the install path: refused before anything is written --------------------

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "1.0.0"
attachment = "native"

[tools]
allow = ["{tool}"]

[[tools.declared]]
name = "{tool}"
classification = "{classification}"
capability_tags = [{tags}]

[config.native]
entrypoint = "acme_tickets_impl:build"
"""


def _manifest(*, tool: str, tags: list[str], classification: str = "state_modifying") -> str:
    rendered = ", ".join(f'"{tag}"' for tag in tags)
    return _MANIFEST.format(tool=tool, tags=rendered, classification=classification)


def _bundle(root: Path, manifest: str) -> Path:
    folder = root / _EXTENSION
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "extension.toml").write_text(manifest, encoding="utf-8")
    return folder


def _sign(path: Path, signer: AgentIdentity) -> None:
    artifact_signing.write_signature(
        path, path.read_bytes(), signer_did=signer.did, private_key=signer.signing_seed
    )


@pytest.fixture()
def operator() -> AgentIdentity:
    return AgentIdentity.generate(org="arc", agent_type="operator")


class FakeAttachment:
    """A reachable connection serving exactly the verb the manifest declared."""

    def __init__(self, tool: str, tags: list[str]) -> None:
        self._spec = ToolSpec(name=tool, capability_tags=tags)

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=True, tools=[self._spec], detail="ok")

    async def describe_tools(self) -> list[ToolSpec]:
        return [self._spec]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, outcome=ToolOutcome.OK)


def _agent_dir(tmp_path: Path) -> Path:
    agent = tmp_path / "sales_agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "arcagent.toml").write_text('[agent]\nname = "sales_agent"\n', encoding="utf-8")
    return agent


async def _install(
    tmp_path: Path,
    *,
    tool: str,
    tags: list[str],
    tier: Tier,
    operator: AgentIdentity,
    egress_allow: tuple[str, ...] = (),
    classification: str = "state_modifying",
) -> Any:
    """Run the whole install, signed so only the egress rule can refuse it."""
    root = tmp_path / "extensions"
    bundle = _bundle(root, _manifest(tool=tool, tags=tags, classification=classification))
    _sign(bundle / "extension.toml", operator)
    plan = plan_connector(
        extensions_root=[root],
        extension=_EXTENSION,
        instance="sales",
        tier=tier,
        audit_sink=RecordingSink(),
        egress_allow=egress_allow,
    )
    return await install_connector(
        plan,
        agent_dir=_agent_dir(tmp_path),
        agent="sales_agent",
        secret_values={},
        store=SecretStore(LocalFileSecretBackend(tmp_path / "arc.env")),
        caller_did="did:arc:example:org:agent:abc",
        attachment_factory=lambda _m, _b: FakeAttachment(tool, tags),
        trusted_public_key=operator.public_key,
    )


@pytest.mark.asyncio
class TestInstallRefusal:
    """A bundle declaring a send the tier forbids is refused before anything is written."""

    async def test_personal_installs_an_egress_bundle(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        report = await _install(
            tmp_path, tool=_SEND, tags=[_EGRESS_TAG], tier=Tier.PERSONAL, operator=operator
        )
        assert _SEND in report.tools

    async def test_enterprise_refuses_an_unlisted_egress_bundle(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        with pytest.raises(ExtensionError) as caught:
            await _install(
                tmp_path, tool=_SEND, tags=[_EGRESS_TAG], tier=Tier.ENTERPRISE, operator=operator
            )
        assert caught.value.details["step"] == "manifest"
        assert caught.value.details["tool"] == _SEND

    async def test_enterprise_installs_a_listed_egress_bundle(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        report = await _install(
            tmp_path,
            tool=_SEND,
            tags=[_EGRESS_TAG],
            tier=Tier.ENTERPRISE,
            operator=operator,
            egress_allow=(_SEND,),
        )
        assert _SEND in report.tools

    async def test_federal_refuses_an_egress_bundle(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        with pytest.raises(ExtensionError) as caught:
            await _install(
                tmp_path, tool=_SEND, tags=[_EGRESS_TAG], tier=Tier.FEDERAL, operator=operator
            )
        assert caught.value.details["step"] == "manifest"

    async def test_federal_refusal_survives_an_egress_allow_entry(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        """A config list must not buy at federal what only a signature buys."""
        with pytest.raises(ExtensionError):
            await _install(
                tmp_path,
                tool=_SEND,
                tags=[_EGRESS_TAG],
                tier=Tier.FEDERAL,
                operator=operator,
                egress_allow=(_SEND,),
            )

    async def test_nothing_is_written_when_the_install_is_refused(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        """The refusal is a refusal, not a cleanup: no config block is left behind."""
        with pytest.raises(ExtensionError):
            await _install(
                tmp_path, tool=_SEND, tags=[_EGRESS_TAG], tier=Tier.FEDERAL, operator=operator
            )
        config = (tmp_path / "sales_agent" / "arcagent.toml").read_text(encoding="utf-8")
        assert "extensions" not in config

    @pytest.mark.parametrize("tier", [Tier.PERSONAL, Tier.ENTERPRISE, Tier.FEDERAL])
    async def test_a_read_only_bundle_installs_at_every_tier(
        self, tmp_path: Path, operator: AgentIdentity, tier: Tier
    ) -> None:
        report = await _install(
            tmp_path,
            tool=_READ,
            tags=["file_read"],
            tier=tier,
            operator=operator,
            classification="read_only",
        )
        assert _READ in report.tools


@pytest.mark.asyncio
class TestRefusalMessage:
    """An operator must be able to read which verb was refused and what refused it."""

    async def test_the_enterprise_message_names_the_tool_and_the_tier(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        with pytest.raises(ExtensionError) as caught:
            await _install(
                tmp_path, tool=_SEND, tags=[_EGRESS_TAG], tier=Tier.ENTERPRISE, operator=operator
            )
        message = caught.value.message
        assert _SEND in message
        assert "enterprise" in message
        assert "egress_allow" in message

    async def test_the_federal_message_names_the_tool_the_tier_and_the_remedy(
        self, tmp_path: Path, operator: AgentIdentity
    ) -> None:
        with pytest.raises(ExtensionError) as caught:
            await _install(
                tmp_path, tool=_SEND, tags=[_EGRESS_TAG], tier=Tier.FEDERAL, operator=operator
            )
        message = caught.value.message
        assert _SEND in message
        assert "federal" in message
        assert "capabilit" in message  # the remedy: an operator-signed capability


# --- the load path, end to end: the loader refuses a forbidden bundle --------


@pytest.mark.asyncio
async def test_the_loader_refuses_a_federal_egress_bundle_and_registers_nothing(
    tmp_path: Path, operator: AgentIdentity
) -> None:
    """The end-to-end proof: refused at LOAD, with an empty registry afterwards.

    A signed bundle, so the signature gate cannot be what refuses it — the only
    thing left to refuse it is the egress rule.
    """
    root = tmp_path / "extensions"
    bundle = _bundle(root, _manifest(tool=_SEND, tags=[_EGRESS_TAG]))
    _sign(bundle / "extension.toml", operator)
    registry = CapabilityRegistry()

    loader = ExtensionLoader(
        roots=[root],
        registry=registry,
        tier=Tier.FEDERAL,
        audit_sink=RecordingSink(),
        trusted_public_key=operator.public_key,
    )
    with pytest.raises(ExtensionError) as caught:
        await loader.load(_EXTENSION)

    assert caught.value.details["reason"] == "egress_forbidden"
    assert _SEND in caught.value.message
    assert await registry.get_tool(_SEND) is None


@pytest.mark.asyncio
async def test_the_loader_admits_a_read_only_bundle_at_federal(
    tmp_path: Path, operator: AgentIdentity
) -> None:
    """The positive control: the same bundle without the send loads cleanly."""
    root = tmp_path / "extensions"
    bundle = _bundle(root, _manifest(tool=_READ, tags=["file_read"], classification="read_only"))
    _sign(bundle / "extension.toml", operator)

    loader = ExtensionLoader(
        roots=[root],
        registry=CapabilityRegistry(),
        tier=Tier.FEDERAL,
        audit_sink=RecordingSink(),
        trusted_public_key=operator.public_key,
    )
    loaded = await loader.load(_EXTENSION)
    assert loaded.name == _EXTENSION


# --- the origin signal reaches the gate down BOTH real paths ----------------
#
# The gate reads an origin the producers have to set. A field nothing populates
# would let every one of the tests above pass against wiring that is dead in
# production, so each producer is exercised on its real path.


@pytest.mark.asyncio
async def test_a_bundles_own_capability_carries_its_root_into_the_registry() -> None:
    """A ``.py`` shipped inside a bundle registers through an ``extension:`` root.

    That root is the only thing distinguishing it from a capability the operator
    placed, so ``bridge_capability_tools_to_registry`` must carry it across.
    """

    async def execute(**kwargs: Any) -> str:
        return "sent"

    capabilities = CapabilityRegistry()
    await capabilities.register_tool(
        ToolEntry(
            meta=ToolMetadata(
                name=_SEND,
                description="sends a message upstream",
                input_schema={"type": "object", "properties": {}},
                classification="state_modifying",
                capability_tags=(_EGRESS_TAG,),
            ),
            execute=execute,
            source_path=Path(f"/extensions/{_EXTENSION}/send.py"),
            scan_root=f"extension:{_EXTENSION}",
        )
    )
    tool_registry = _registry(tier=Tier.FEDERAL)
    agent = SimpleNamespace(
        _capability_registry=capabilities,
        _tool_registry=tool_registry,
        _capability_tool_names=set(),
    )

    await bridge_capability_tools_to_registry(cast("Any", agent))

    assert _SEND not in tool_registry.tools


def test_an_attachment_verb_carries_its_extension_origin_into_the_registry() -> None:
    """The bridge's own registrations are extension-origin by construction."""
    tool_registry = _registry(tier=Tier.FEDERAL)
    report = CapabilityBridge(
        registry=tool_registry,
        attachment=FakeAttachment(_SEND, [_EGRESS_TAG]),
        transport=ToolTransport.PROCESS,
        source=f"extension:{_EXTENSION}",
        allow=[_SEND],
    ).register([ToolSpec(name=_SEND, capability_tags=[_EGRESS_TAG])])

    assert _SEND not in tool_registry.tools
    assert report.denied == (_SEND,)
