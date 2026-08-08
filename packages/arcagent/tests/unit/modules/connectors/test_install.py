"""SPEC-062 T-914 — the connector install path (COMP-016's write half).

Serves REQ-260 (read the manifest, take the declared secrets, probe, and persist
only after the probe succeeds) and REQ-261 (a failure at ANY step leaves no
configuration, no secret, and no partial state, and names the step that failed).

The rollback assertions are the reason this file exists. "Persists only on
success" is trivially satisfiable by any implementation that happens to run the
steps in a lucky order and is trivially broken by a later reordering, so every
failure test here asserts the same three absences — no secret in the store, no
``[extensions.*]`` block in the config, no connection record — rather than
asserting that an exception was raised. An exception proves the call failed; only
the absences prove nothing was left behind.

The prompting half lives in ``arccli.commands.connector``; this module is
surface-agnostic on purpose (D-561) so the TUI and the web panel drive the same
code the CLI drives instead of reimplementing the order of operations.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.secrets import LocalFileSecretBackend, SecretRef, SecretStore
from arcagent.modules.connectors.install import (
    ConnectorPlan,
    install_connector,
    load_instances,
    plan_connector,
    remove_connector,
)

_AGENT = "sales_agent"
_INSTANCE = "sales"
_EXTENSION = "acme_tickets"
_CALLER = "did:arc:example:org:agent:abc"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "1.0.0"
attachment = "cli"

[[secrets]]
name = "api_token"
prompt = "Paste the API token"

[tools]
allow = ["create_issue"]

[approval]
default = "outbound"

[config.cli]
binary = "acme"
probe_argv = ["--version"]

[[config.cli.commands]]
tool = "create_issue"
argv = ["issue", "create"]
description = "Open a ticket."
classification = "state_modifying"
capability_tags = ["network_egress"]
"""


class RecordingSink:
    """Audit sink that keeps every event. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeAttachment:
    """A real attachment whose probe verdict the test chooses."""

    def __init__(self, *, reachable: bool = True, detail: str = "") -> None:
        self._reachable = reachable
        self._detail = detail

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(
            reachable=self._reachable,
            tools=[ToolSpec(name="create_issue")] if self._reachable else [],
            detail=self._detail,
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="create_issue")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, outcome=ToolOutcome.OK)


def _bundle(root: Path, *, manifest: str = _MANIFEST) -> Path:
    folder = root / _EXTENSION
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "extension.toml").write_text(manifest, encoding="utf-8")
    return folder


def _agent_dir(tmp_path: Path) -> Path:
    agent = tmp_path / _AGENT
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "sales_agent"\n\n[llm]\nmodel = "none"\n', encoding="utf-8"
    )
    return agent


def _store(tmp_path: Path) -> tuple[SecretStore, Path]:
    env_file = tmp_path / "arc.env"
    return SecretStore(LocalFileSecretBackend(env_file)), env_file


def _plan(tmp_path: Path, *, manifest: str = _MANIFEST) -> ConnectorPlan:
    root = tmp_path / "extensions"
    _bundle(root, manifest=manifest)
    return plan_connector(
        extensions_root=[root],
        extension=_EXTENSION,
        instance=_INSTANCE,
        tier=Tier.PERSONAL,
        audit_sink=RecordingSink(),
    )


def _blocks(agent_dir: Path) -> dict[str, Any]:
    raw = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    extensions = raw.get("extensions", {})
    assert isinstance(extensions, dict)
    return extensions


class TestPlan:
    """The read-only half: what the operator must supply, before anything is written."""

    def test_plan_reports_the_declared_secrets(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        assert [secret.name for secret in plan.secrets] == ["api_token"]
        assert plan.approval_mode == "outbound"

    def test_plan_names_the_resolve_step_for_an_unknown_extension(self, tmp_path: Path) -> None:
        root = tmp_path / "extensions"
        root.mkdir()
        with pytest.raises(ExtensionError) as caught:
            plan_connector(
                extensions_root=[root],
                extension="nothing_here",
                instance=_INSTANCE,
                tier=Tier.PERSONAL,
                audit_sink=RecordingSink(),
            )
        assert caught.value.details["step"] == "resolve"

    def test_plan_names_the_manifest_step_for_a_broken_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(ExtensionError) as caught:
            _plan(tmp_path, manifest='[extension]\nname = "acme_tickets"\n')
        assert caught.value.details["step"] == "manifest"

    def test_plan_reports_an_unsatisfied_host_prerequisite(self, tmp_path: Path) -> None:
        manifest = _MANIFEST + '\n[[host_requires]]\nname = "definitely_not_installed_xyz"\n'
        plan = _plan(tmp_path, manifest=manifest)
        assert [v.name for v in plan.unsatisfied_host] == ["definitely_not_installed_xyz"]
        assert plan.unsatisfied_host[0].instruction


@pytest.mark.asyncio
class TestInstall:
    """REQ-260 — persist only after the probe succeeds."""

    async def test_a_successful_install_writes_secret_config_and_state(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        store, env_file = _store(tmp_path)

        report = await install_connector(
            _plan(tmp_path),
            agent_dir=agent_dir,
            agent=_AGENT,
            secret_values={"api_token": "s3cr3t"},
            store=store,
            caller_did=_CALLER,
            attachment_factory=lambda _m, _b: FakeAttachment(),
        )

        assert report.instance == _INSTANCE
        assert "create_issue" in report.tools
        stored = await store.get(
            SecretRef(agent=_AGENT, instance=_INSTANCE, field="api_token"), caller_did=_CALLER
        )
        assert stored is not None
        assert stored.reveal() == "s3cr3t"
        assert _blocks(agent_dir)[_INSTANCE]["extension"] == _EXTENSION
        assert _blocks(agent_dir)[_INSTANCE]["approval"] == "outbound"
        # The secret is in the store and nowhere else.
        assert "s3cr3t" not in (agent_dir / "arcagent.toml").read_text(encoding="utf-8")
        assert "s3cr3t" in env_file.read_text(encoding="utf-8")

    async def test_the_written_block_is_readable_back(self, tmp_path: Path) -> None:
        # A write nothing can read is dead wiring; the reader ships with the writer.
        agent_dir = _agent_dir(tmp_path)
        store, _env = _store(tmp_path)
        await install_connector(
            _plan(tmp_path),
            agent_dir=agent_dir,
            agent=_AGENT,
            secret_values={"api_token": "s3cr3t"},
            store=store,
            caller_did=_CALLER,
            attachment_factory=lambda _m, _b: FakeAttachment(),
        )
        instances = load_instances(agent_dir)
        assert instances[_INSTANCE].extension == _EXTENSION
        assert instances[_INSTANCE].approval == "outbound"

    async def test_an_unsigned_bundle_is_refused_before_its_code_runs(
        self, tmp_path: Path
    ) -> None:
        # REQ-282/REQ-283. Building an attachment imports the extension's own
        # module, so the signature gate has to close BEFORE the factory is reached
        # and before the credential is written. Recording whether the factory ran
        # is the assertion that matters: an install can fail for unrelated reasons
        # and still have executed the bundle, which makes a broken gate look shut.
        agent_dir = _agent_dir(tmp_path)
        store, env_file = _store(tmp_path)
        root = tmp_path / "extensions"
        _bundle(root)
        built: list[str] = []

        def _factory(_manifest: object, bundle: Path) -> FakeAttachment:
            built.append(str(bundle))
            return FakeAttachment()

        plan = plan_connector(
            extensions_root=[root],
            extension=_EXTENSION,
            instance=_INSTANCE,
            tier=Tier.ENTERPRISE,
            audit_sink=RecordingSink(),
        )
        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                plan,
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                attachment_factory=_factory,
            )

        assert caught.value.details["step"] == "verify"
        assert built == [], "an unverified bundle's code was executed"
        assert not env_file.exists()
        assert _blocks(agent_dir) == {}

    async def test_a_missing_secret_names_the_secrets_step_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        store, env_file = _store(tmp_path)

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={},
                store=store,
                caller_did=_CALLER,
                attachment_factory=lambda _m, _b: FakeAttachment(),
            )

        assert caught.value.details["step"] == "secrets"
        assert not env_file.exists()
        assert _blocks(agent_dir) == {}

    async def test_an_unsatisfied_host_prerequisite_names_the_host_step(
        self, tmp_path: Path
    ) -> None:
        manifest = _MANIFEST + '\n[[host_requires]]\nname = "definitely_not_installed_xyz"\n'
        agent_dir = _agent_dir(tmp_path)
        store, env_file = _store(tmp_path)

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path, manifest=manifest),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                attachment_factory=lambda _m, _b: FakeAttachment(),
            )

        assert caught.value.details["step"] == "host"
        assert "definitely_not_installed_xyz" in str(caught.value)
        assert not env_file.exists()
        assert _blocks(agent_dir) == {}

    async def test_a_failed_probe_rolls_the_secret_back(self, tmp_path: Path) -> None:
        # The step that most often fails is the one that runs AFTER the secret is
        # written, so this is the rollback that actually has to work.
        agent_dir = _agent_dir(tmp_path)
        store, _env = _store(tmp_path)

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                attachment_factory=lambda _m, _b: FakeAttachment(
                    reachable=False, detail="acme: command not found"
                ),
            )

        assert caught.value.details["step"] == "probe"
        assert "acme: command not found" in str(caught.value)
        left = await store.get(
            SecretRef(agent=_AGENT, instance=_INSTANCE, field="api_token"), caller_did=_CALLER
        )
        assert left is None
        assert _blocks(agent_dir) == {}

    async def test_an_attachment_that_cannot_be_built_rolls_the_secret_back(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        store, _env = _store(tmp_path)

        def explode(_manifest: object, _bundle: object) -> FakeAttachment:
            raise RuntimeError("bad entrypoint")

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                attachment_factory=explode,
            )

        assert caught.value.details["step"] == "probe"
        left = await store.get(
            SecretRef(agent=_AGENT, instance=_INSTANCE, field="api_token"), caller_did=_CALLER
        )
        assert left is None
        assert _blocks(agent_dir) == {}

    async def test_a_second_instance_of_one_bundle_is_independent(self, tmp_path: Path) -> None:
        # One bundle backs several named accounts (SDD data model), so installing
        # the second must not disturb the first's block or its credential.
        agent_dir = _agent_dir(tmp_path)
        store, _env = _store(tmp_path)
        root = tmp_path / "extensions"
        _bundle(root)

        for instance, value in (("sales", "one"), ("support", "two")):
            plan = plan_connector(
                extensions_root=[root],
                extension=_EXTENSION,
                instance=instance,
                tier=Tier.PERSONAL,
                audit_sink=RecordingSink(),
            )
            await install_connector(
                plan,
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": value},
                store=store,
                caller_did=_CALLER,
                attachment_factory=lambda _m, _b: FakeAttachment(),
            )

        assert set(_blocks(agent_dir)) == {"sales", "support"}
        first = await store.get(
            SecretRef(agent=_AGENT, instance="sales", field="api_token"), caller_did=_CALLER
        )
        assert first is not None
        assert first.reveal() == "one"


@pytest.mark.asyncio
class TestRemove:
    """Removing a connection leaves nothing behind either."""

    async def test_remove_drops_the_secret_and_the_block(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        store, _env = _store(tmp_path)
        plan = _plan(tmp_path)
        await install_connector(
            plan,
            agent_dir=agent_dir,
            agent=_AGENT,
            secret_values={"api_token": "s3cr3t"},
            store=store,
            caller_did=_CALLER,
            attachment_factory=lambda _m, _b: FakeAttachment(),
        )

        report = await remove_connector(
            agent_dir=agent_dir,
            agent=_AGENT,
            instance=_INSTANCE,
            store=store,
            caller_did=_CALLER,
            secret_fields=[secret.name for secret in plan.secrets],
        )

        assert report.removed_secrets == ("api_token",)
        assert _blocks(agent_dir) == {}
        assert load_instances(agent_dir) == {}
        left = await store.get(
            SecretRef(agent=_AGENT, instance=_INSTANCE, field="api_token"), caller_did=_CALLER
        )
        assert left is None

    async def test_removing_an_unknown_instance_is_not_an_error(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        store, _env = _store(tmp_path)
        report = await remove_connector(
            agent_dir=agent_dir,
            agent=_AGENT,
            instance="never_installed",
            store=store,
            caller_did=_CALLER,
            secret_fields=[],
        )
        assert report.removed_config is False
