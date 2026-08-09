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
from arcagent.extension.state import ConnectionStateStore
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

#: A name the coordinate rule refuses. Hyphens are legal in a bare TOML key, so a
#: block carrying one can exist on disk; it is refused because the name also
#: becomes an env-var segment, where a hyphen is not a legal shell variable name.
_ILLEGAL_INSTANCE = "personal-mail"

_MANIFEST = """
[extension]
name = "acme_tickets"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "acme_attachment"

[[secrets]]
name = "api_token"
prompt = "Paste the API token"

[tools]
allow = ["create_issue"]

[[tools.declared]]
name = "create_issue"
description = "Open a ticket."
classification = "state_modifying"

[approval]
default = "outbound"
"""


#: The shape that got past every check: a bundle declaring NO ``[[secrets]]``, so
#: no :class:`~arcagent.extension.secrets.SecretRef` is ever constructed and the
#: only validator on the path never runs. ``google_workspace`` and ``dropbox``
#: are exactly this.
_MANIFEST_NO_SECRETS = """
[extension]
name = "acme_tickets"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "acme_attachment"

[tools]
allow = ["create_issue"]

[[tools.declared]]
name = "create_issue"
description = "Open a ticket."
classification = "state_modifying"

[approval]
default = "outbound"
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


class MemoryBackend:
    """The mutable plane in a dict, with the merge semantics the real one has.

    A real :class:`~arcagent.extension.state.ConnectionStateStore` over a fake
    plane rather than a fake store: ``_patch`` returning False for a row that does
    not exist is the behaviour every assertion here turns on, so the store's own
    code has to run.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Any,
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]:
        return [self.rows.setdefault(key, dict(value)) for key, value in entries]

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        row = self.rows.get(key)
        if row is None:
            return False
        _deep_merge(row, patch)
        return True

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        return self.rows.get(key)

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows.values()
            if all(row.get(field) == value for field, value in (where or {}).items())
        ]

    async def mutable_delete(
        self, collection: str, key: str, *, actor_did: str, sink: Any | None = None
    ) -> bool:
        return self.rows.pop(key, None) is not None


def _deep_merge(row: dict[str, Any], patch: dict[str, Any]) -> None:
    """Recursive merge — what a single-statement backend merge does to one row."""
    for field, value in patch.items():
        existing = row.get(field)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(existing, value)
        else:
            row[field] = value


def _state() -> ConnectionStateStore:
    """The connection directory an install is required to register into."""
    return ConnectionStateStore(MemoryBackend())


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


def _plan(
    tmp_path: Path, *, manifest: str = _MANIFEST, instance: str = _INSTANCE
) -> ConnectorPlan:
    root = tmp_path / "extensions"
    _bundle(root, manifest=manifest)
    return plan_connector(
        extensions_root=[root],
        extension=_EXTENSION,
        instance=instance,
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

    @pytest.mark.parametrize(
        "instance",
        [
            "blackarc industrial email",
            "Work Email",
            "../../etc",
            "work.email",
            "personal-dropbox",
            "a" * 65,
            "_leading",
        ],
    )
    def test_plan_refuses_a_name_that_is_not_a_legal_coordinate(
        self, tmp_path: Path, instance: str
    ) -> None:
        """One check, on the one function every path comes through.

        ``blackarc industrial email`` is the one that happened: written as the
        bare key ``[extensions.blackarc industrial email]``, ``arcagent.toml``
        stopped parsing, the agent vanished from the roster, and every one of its
        routes answered 404. The bundle here declares NO ``[[secrets]]`` — the
        shape whose name nothing else on the path ever looks at, and therefore
        the shape that got through.
        """
        with pytest.raises(ExtensionError) as caught:
            _plan(tmp_path, manifest=_MANIFEST_NO_SECRETS, instance=instance)
        assert caught.value.details["step"] == "resolve"

    def test_the_refusal_tells_a_non_technical_operator_what_to_type_instead(
        self, tmp_path: Path
    ) -> None:
        """Whoever reads this typed something perfectly reasonable."""
        with pytest.raises(ExtensionError) as caught:
            _plan(
                tmp_path, manifest=_MANIFEST_NO_SECRETS, instance="blackarc industrial email"
            )
        message = caught.value.message
        assert "blackarc industrial email" in message
        assert "blackarc_industrial_email" in message

    @pytest.mark.parametrize("instance", ["work_email", "work2", "a", "0account"])
    def test_plan_accepts_the_names_an_operator_would_reasonably_choose(
        self, tmp_path: Path, instance: str
    ) -> None:
        assert _plan(tmp_path, instance=instance).instance == instance

    def test_a_refused_name_is_refused_before_anything_is_written(
        self, tmp_path: Path
    ) -> None:
        """Byte-identical, not merely "no ``[extensions]`` block": the file an agent
        starts from must be untouched by a refusal."""
        agent_dir = _agent_dir(tmp_path)
        before = (agent_dir / "arcagent.toml").read_bytes()

        with pytest.raises(ExtensionError):
            _plan(tmp_path, manifest=_MANIFEST_NO_SECRETS, instance="work email")

        assert (agent_dir / "arcagent.toml").read_bytes() == before


@pytest.mark.asyncio
class TestInstall:
    """REQ-260 — persist only after the probe succeeds."""

    async def test_a_successful_install_writes_secret_config_and_state(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, env_file = _store(tmp_path)

        report = await install_connector(
            _plan(tmp_path),
            agent_dir=agent_dir,
            agent=_AGENT,
            secret_values={"api_token": "s3cr3t"},
            store=store,
            caller_did=_CALLER,
            state=state,
            attachment_factory=lambda _m, _b, _s: FakeAttachment(),
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
        # Connecting registers the connection AND approves the contract it just
        # probed: without the record the approval is a merge against nothing, and
        # without the approval every tool it serves is suspended at the next start.
        record = await state.get(_AGENT, _INSTANCE)
        assert record is not None
        assert record.health == "healthy"
        assert list(record.approved_tool_hashes) == ["create_issue"]
        # The secret is in the store and nowhere else.
        assert "s3cr3t" not in (agent_dir / "arcagent.toml").read_text(encoding="utf-8")
        assert "s3cr3t" in env_file.read_text(encoding="utf-8")

    async def test_the_written_block_is_readable_back(self, tmp_path: Path) -> None:
        # A write nothing can read is dead wiring; the reader ships with the writer.
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, _env = _store(tmp_path)
        await install_connector(
            _plan(tmp_path),
            agent_dir=agent_dir,
            agent=_AGENT,
            secret_values={"api_token": "s3cr3t"},
            store=store,
            caller_did=_CALLER,
            state=state,
            attachment_factory=lambda _m, _b, _s: FakeAttachment(),
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
        state = _state()
        store, env_file = _store(tmp_path)
        root = tmp_path / "extensions"
        _bundle(root)
        built: list[str] = []

        def _factory(_manifest: object, bundle: Path, _secrets: object) -> FakeAttachment:
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
                state=state,
                attachment_factory=_factory,
            )

        assert caught.value.details["step"] == "verify"
        assert built == [], "an unverified bundle's code was executed"
        assert not env_file.exists()
        assert _blocks(agent_dir) == {}
        assert await state.get(_AGENT, _INSTANCE) is None

    async def test_a_missing_secret_names_the_secrets_step_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, env_file = _store(tmp_path)

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={},
                store=store,
                caller_did=_CALLER,
                state=state,
                attachment_factory=lambda _m, _b, _s: FakeAttachment(),
            )

        assert caught.value.details["step"] == "secrets"
        assert not env_file.exists()
        assert _blocks(agent_dir) == {}
        assert await state.get(_AGENT, _INSTANCE) is None

    async def test_an_unsatisfied_host_prerequisite_names_the_host_step(
        self, tmp_path: Path
    ) -> None:
        manifest = _MANIFEST + '\n[[host_requires]]\nname = "definitely_not_installed_xyz"\n'
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, env_file = _store(tmp_path)

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path, manifest=manifest),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                state=state,
                attachment_factory=lambda _m, _b, _s: FakeAttachment(),
            )

        assert caught.value.details["step"] == "host"
        assert "definitely_not_installed_xyz" in str(caught.value)
        assert not env_file.exists()
        assert _blocks(agent_dir) == {}
        assert await state.get(_AGENT, _INSTANCE) is None

    async def test_a_failed_probe_rolls_the_secret_back(self, tmp_path: Path) -> None:
        # The step that most often fails is the one that runs AFTER the secret is
        # written, so this is the rollback that actually has to work.
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, _env = _store(tmp_path)

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                state=state,
                attachment_factory=lambda _m, _b, _s: FakeAttachment(
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
        assert await state.get(_AGENT, _INSTANCE) is None

    async def test_an_attachment_that_cannot_be_built_rolls_the_secret_back(
        self, tmp_path: Path
    ) -> None:
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, _env = _store(tmp_path)

        def explode(_manifest: object, _bundle: object, _secrets: object) -> FakeAttachment:
            raise RuntimeError("bad entrypoint")

        with pytest.raises(ExtensionError) as caught:
            await install_connector(
                _plan(tmp_path),
                agent_dir=agent_dir,
                agent=_AGENT,
                secret_values={"api_token": "s3cr3t"},
                store=store,
                caller_did=_CALLER,
                state=state,
                attachment_factory=explode,
            )

        assert caught.value.details["step"] == "probe"
        left = await store.get(
            SecretRef(agent=_AGENT, instance=_INSTANCE, field="api_token"), caller_did=_CALLER
        )
        assert left is None
        assert _blocks(agent_dir) == {}
        assert await state.get(_AGENT, _INSTANCE) is None

    async def test_a_second_instance_of_one_bundle_is_independent(self, tmp_path: Path) -> None:
        # One bundle backs several named accounts (SDD data model), so installing
        # the second must not disturb the first's block or its credential.
        agent_dir = _agent_dir(tmp_path)
        state = _state()
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
                state=state,
                attachment_factory=lambda _m, _b, _s: FakeAttachment(),
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
        state = _state()
        store, _env = _store(tmp_path)
        plan = _plan(tmp_path)
        await install_connector(
            plan,
            agent_dir=agent_dir,
            agent=_AGENT,
            secret_values={"api_token": "s3cr3t"},
            store=store,
            caller_did=_CALLER,
            state=state,
            attachment_factory=lambda _m, _b, _s: FakeAttachment(),
        )

        report = await remove_connector(
            agent_dir=agent_dir,
            agent=_AGENT,
            instance=_INSTANCE,
            store=store,
            caller_did=_CALLER,
            state=state,
            secret_fields=[secret.name for secret in plan.secrets],
        )

        assert report.removed_secrets == ("api_token",)
        assert report.removed_state is True
        # A record that outlives its account hands the next install under this name
        # the approvals an operator minted for the connection they disconnected.
        assert await state.get(_AGENT, _INSTANCE) is None
        assert _blocks(agent_dir) == {}
        assert load_instances(agent_dir) == {}
        left = await store.get(
            SecretRef(agent=_AGENT, instance=_INSTANCE, field="api_token"), caller_did=_CALLER
        )
        assert left is None

    async def test_removing_an_unknown_instance_is_not_an_error(self, tmp_path: Path) -> None:
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, _env = _store(tmp_path)
        report = await remove_connector(
            agent_dir=agent_dir,
            agent=_AGENT,
            instance="never_installed",
            store=store,
            caller_did=_CALLER,
            state=state,
            secret_fields=[],
        )
        assert report.removed_config is False

    async def test_a_connection_whose_name_the_rule_now_rejects_can_still_be_removed(
        self, tmp_path: Path
    ) -> None:
        """The strict path must have an exit, or the rule creates the undeletable.

        A block carrying a name ``plan_connector`` now refuses can exist — an
        operator hand-edited the config, or it predates the rule. Removal reads a
        connection's credential fields through the planner, so that read refuses;
        it must be treated as "no fields to delete" and the block and the record
        dropped anyway.

        Sound rather than an exception to the rule: ``SecretRef`` applies the very
        same coordinate check, so no credential can ever have been stored under
        this name, and there is nothing left behind to strand.
        """
        agent_dir = _agent_dir(tmp_path)
        state = _state()
        store, _env = _store(tmp_path)
        config = agent_dir / "arcagent.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + f'\n[extensions."{_ILLEGAL_INSTANCE}"]\nextension = "{_EXTENSION}"\n',
            encoding="utf-8",
        )

        report = await remove_connector(
            agent_dir=agent_dir,
            agent=_AGENT,
            instance=_ILLEGAL_INSTANCE,
            store=store,
            caller_did=_CALLER,
            # What Connections._declared_secret_fields resolves to when the planner
            # refuses the name: nothing to delete, because nothing could be stored.
            secret_fields=[],
            state=state,
        )

        assert report.removed_config is True
        assert _blocks(agent_dir) == {}
        assert load_instances(agent_dir) == {}
