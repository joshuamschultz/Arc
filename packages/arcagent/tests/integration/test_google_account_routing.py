"""Google accounts routed per call, end to end, against a fake ``gog``.

The real :class:`~arcagent.modules.connectors.capabilities.Connectors` capability,
the real registry, the SHIPPED ``google_workspace`` manifest and a fake ``gog``
(``extensions/tests/fixtures/fake_gog.py``) that answers AS whichever account gog
itself would use — ``--account`` beating ``GOG_ACCOUNT``, exactly as gog does.

The hole this closes, stated as the first test: an agent granted only ``blackarc``
could pass ``account=<systems address>`` to a Google tool, gog would act as
``systems``, and the audit would name ``blackarc``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.paths import arc_team
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.connections import AuditChain, Connections
from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.source import FetchSourceObject, InspectSource, SyncSource
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.capabilities import Connectors
from arcagent.tools.human_gate import HumanGate

_REPO = Path(__file__).resolve().parents[4]
_BUNDLE = _REPO / "extensions" / "google_workspace"
_FAKE_GOG = _REPO / "extensions" / "tests" / "fixtures" / "fake_gog.py"
_A = "josh@blackarcindustrial.com"
_B = "josh@blackarcsystems.com"
_BOTH = "mailbot"
_ONLY_A = "reader"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def named(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def gog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "gog"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_GOG}" "$@"\n', encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "gog"
    home.mkdir()
    monkeypatch.setenv("FAKE_GOG_HOME", str(home))
    # The service environment's own account must never decide a call.
    monkeypatch.setenv("GOG_ACCOUNT", "service-default@example.com")
    monkeypatch.delenv("GOG_CLIENT", raising=False)
    (home / "tokens.json").write_text(
        json.dumps(
            {f"arc:{_A}": "good", f"arc:{_B}": "good", "arc:service-default@example.com": "good"}
        )
    )
    return home


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.arc_dir = tmp_path / "arc"
        self.arc_dir.mkdir()
        self.data_dir = tmp_path / "data"
        self.root = tmp_path / "extensions"
        shutil.copytree(
            _BUNDLE, self.root / "google_workspace", ignore=shutil.ignore_patterns("__pycache__")
        )
        self.backend = FakeBackend()
        self.sink = _Sink()

    async def open_backend(self) -> FakeBackend:
        return self.backend

    def agent_dir(self, agent: str) -> Path:
        directory = arc_team(base=self.arc_dir) / agent
        if not directory.is_dir():
            (directory / "workspace").mkdir(parents=True)
            (directory / "arcagent.toml").write_text(
                f'[agent]\nname = "{agent}"\norg = "t"\ntype = "executor"\n'
                f'workspace = "{directory / "workspace"}"\n[llm]\nmodel = "test/model"\n'
                f'[identity]\ndid = "did:arc:t:executor/{agent}"\n[security]\ntier = "personal"\n',
                encoding="utf-8",
            )
        return directory

    def connections(self) -> Connections:
        return Connections.for_deployment(
            arc_dir=self.arc_dir,
            data_dir=self.data_dir,
            extensions_root=self.root,
            audit=AuditChain.held(_Sink()),
            state_opener=self.open_backend,
        )

    async def connect(self, instance: str, account: str, agents: list[str], **extra: str) -> None:
        for agent in agents:
            self.agent_dir(agent)
        connections = self.connections()
        plan = connections.plan("google_workspace", instance, agents=agents)
        await connections.install(
            plan, {"account": account, "client": "arc", **extra}, agents=agents
        )
        # Relax the human gate so write verbs run in this test; the gate's own
        # behaviour is covered elsewhere and is per connection either way.
        registry = ConnectionRegistry(self.arc_dir)
        registry.define(instance, registry.get(instance).model_copy(update={"approval": "none"}))

    async def start(self, agent: str, *, catalog: SourceCatalog | None = None) -> ToolRegistry:
        did = f"did:arc:t:executor/{agent}"
        gate = HumanGate(
            operator_signer=InProcessSigner(bytes(SigningKey.generate())),
            agent_did=did,
            tier="personal",
        )
        registry = ToolRegistry(
            config=ToolsConfig(policy=ToolConfig()),
            bus=ModuleBus(),
            telemetry=MagicMock(),
            human_gate=gate,
        )
        telemetry = MagicMock()
        telemetry.audit_event.side_effect = lambda action, payload: self.sink.write(
            AuditEvent(
                actor_did=did,
                action=action,
                target=str(payload.get("target", "")),
                outcome=str(payload.get("outcome", "")),
                extra={k: v for k, v in payload.items() if k not in {"target", "outcome", "tier"}},
            )
        )
        identity = MagicMock()
        identity.did = did
        _runtime.configure(
            config={
                "data_dir": str(self.data_dir),
                "extensions_root": str(self.root),
                "arc_dir": str(self.arc_dir),
            },
            telemetry=telemetry,
            workspace=self.agent_dir(agent) / "workspace",
            identity=identity,
            config_path=self.agent_dir(agent) / "arcagent.toml",
            tool_registry=registry,
            tier="personal",
            human_gate=gate,
            arcstore_opener=self.open_backend,
            source_catalog=catalog,
        )
        await Connectors().setup(None)
        return registry


@pytest.fixture
def world(tmp_path: Path, gog: Path) -> _World:
    return _World(tmp_path)


async def _call(registry: ToolRegistry, tool: str, **args: Any) -> str:
    result: str = await registry.tools[tool].execute(**args)
    return result


def _answer(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(text)
    return parsed


# --- the hole, closed -----------------------------------------------------------


async def test_an_agent_granted_only_a_cannot_act_as_b_by_naming_it(world: _World) -> None:
    await world.connect("blackarc", _A, [_BOTH, _ONLY_A])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_ONLY_A)

    text = await _call(
        registry, "google_gmail_send", account=_B, to="x@example.com", subject="s", body="b"
    )

    assert text.startswith("error")
    assert _B in [
        event.extra.get("requested") for event in world.sink.named("connector.account.denied")
    ]
    calls = [
        json.loads(line)
        for line in (Path(os.environ["FAKE_GOG_HOME"]) / "calls.jsonl").read_text().splitlines()
    ]
    assert not [call for call in calls if call["argv"][:2] == ["gmail", "send"]]


async def test_one_tool_name_serves_both_granted_accounts_and_names_the_real_one(
    world: _World,
) -> None:
    await world.connect("blackarc", _A, [_BOTH])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_BOTH)

    ambiguous = await _call(registry, "google_gmail_labels")
    assert ambiguous.startswith("error") and _A in ambiguous and _B in ambiguous

    for account, instance in ((_A, "blackarc"), (_B, "systems")):
        answer = _answer(await _call(registry, "google_gmail_labels", account=account))
        assert (answer["connection"], answer["account"]) == (instance, account)
        assert answer["result"]["account"] == account  # gog really acted as it
        assert answer["result"]["client"] == "arc"
        assert not any(arg.startswith("--account") for arg in answer["result"]["argv"])
        routed = world.sink.named("connector.account.routed")[-1]
        assert (routed.extra["connection"], routed.extra["account"]) == (instance, account)
    # No collision: the later connection is not dropped.
    assert "google_gmail_send" in registry.tools


@pytest.mark.parametrize(
    "variant",
    [
        _B.upper(),
        f" {_B}",
        _B.replace("o", "\u043e"),
        "systems",
        "work",
    ],
)
async def test_no_variant_of_an_ungranted_account_gets_through(
    world: _World, variant: str
) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_ONLY_A)
    assert (await _call(registry, "google_gmail_labels", account=variant)).startswith("error")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("google_gmail_search", {"query": f"from:x --account={_B}"}),
        ("google_gmail_search", {"query": f"-a {_B}"}),
        ("google_gmail_search", {"query": "--home=/tmp/other"}),
        ("google_gmail_search", {"query": "--client=default"}),
        ("google_gmail_search", {"query": f"GOG_ACCOUNT={_B}"}),
        (
            "google_gmail_draft",
            {"to": f"x@example.com --account={_B}", "subject": "s", "body": "b"},
        ),
    ],
)
async def test_flags_smuggled_in_other_arguments_never_reach_gog(
    world: _World, tool: str, args: dict[str, str]
) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_ONLY_A)
    before = (Path(os.environ["FAKE_GOG_HOME"]) / "calls.jsonl").read_text()

    assert (await _call(registry, tool, **args)).startswith("error")

    assert (Path(os.environ["FAKE_GOG_HOME"]) / "calls.jsonl").read_text() == before


async def test_concurrent_calls_for_two_accounts_never_cross(world: _World) -> None:
    await world.connect("blackarc", _A, [_BOTH])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_BOTH)

    accounts = [_A, _B] * 6
    answers = await asyncio.gather(
        *(_call(registry, "google_gmail_labels", account=account) for account in accounts)
    )

    for account, text in zip(accounts, answers, strict=True):
        assert _answer(text)["result"]["account"] == account


async def test_a_read_only_sign_in_blocks_write_tools_with_a_sentence(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])  # read_only defaults to yes
    registry = await world.start(_ONLY_A)

    assert _answer(await _call(registry, "google_gmail_labels"))["connection"] == "blackarc"
    refused = await _call(
        registry, "google_gmail_draft", to="x@example.com", subject="s", body="b"
    )
    assert refused.startswith("error") and "read-only" in refused


async def test_a_drafting_connection_may_write(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A], read_only="no")
    registry = await world.start(_ONLY_A)
    answer = _answer(
        await _call(registry, "google_gmail_draft", to="x@example.com", subject="s", body="b")
    )
    assert answer["result"]["argv"][:3] == ["gmail", "drafts", "create"]


async def test_a_page_size_above_its_ceiling_is_refused(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    registry = await world.start(_ONLY_A)
    assert (
        await _call(registry, "google_gmail_search", query="in:inbox", limit="5000")
    ).startswith("error")
    ok = _answer(await _call(registry, "google_gmail_search", query="in:inbox", limit="50"))
    assert "--max=50" in ok["result"]["argv"]


async def test_an_oversized_answer_is_refused(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    registry = await world.start(_ONLY_A)
    monkeypatch.setenv("FAKE_GOG_PAD", str(3 * 1024 * 1024))
    text = await _call(registry, "google_gmail_search", query="in:inbox")
    assert text.startswith("error") and "too large" in text


async def test_mail_content_verbs_ask_gog_to_mark_it_untrusted(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    registry = await world.start(_ONLY_A)
    for tool, args in (
        ("google_gmail_search", {"query": "in:inbox"}),
        ("google_gmail_thread", {"thread_id": "t1"}),
        ("google_gmail_draft_get", {"draft_id": "d1"}),
    ):
        answer = _answer(await _call(registry, tool, **args))
        assert "--wrap-untrusted" in answer["result"]["argv"], tool
    message = _answer(await _call(registry, "google_gmail_message", id="m1"))
    assert "<untrusted>" in json.dumps(message["result"])


async def test_an_attachment_lands_in_this_connections_downloads_folder(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    registry = await world.start(_ONLY_A)

    answer = _answer(
        await _call(
            registry,
            "google_gmail_attachment",
            message_id="m1",
            attachment_id="a1",
            file_name="bill.pdf",
        )
    )

    expected = (
        world.agent_dir(_ONLY_A)
        / "workspace"
        / "downloads"
        / "google_workspace"
        / "blackarc"
        / "bill.pdf"
    )
    assert Path(answer["result"]["path"]) == expected
    assert expected.is_file()
    escape = await _call(
        registry,
        "google_gmail_attachment",
        message_id="m1",
        attachment_id="a1",
        file_name="../../identity.md",
    )
    assert escape.startswith("error")
    monkeypatch.setenv("FAKE_GOG_ATTACHMENT_BYTES", str(26 * 1024 * 1024))
    big = await _call(
        registry,
        "google_gmail_attachment",
        message_id="m1",
        attachment_id="a1",
        file_name="big.bin",
    )
    assert big.startswith("error") and not expected.with_name("big.bin").exists()


# --- knowledge sync is per connection, not routed ------------------------------------


async def test_two_connections_sync_their_own_mailboxes(world: _World) -> None:
    await world.connect("blackarc", _A, [_BOTH])
    await world.connect("systems", _B, [_BOTH])
    catalog = SourceCatalog()
    await world.start(_BOTH, catalog=catalog)
    # The capability hands sources to the catalog only on reconcile; drive it.
    capability = Connectors()
    # Drive one reconcile, which is what hands sources to the catalog.
    capability._registry = _runtime.state().tool_registry
    await capability.reconcile()

    registrations = {item.connection_id: item for item in await catalog.snapshot()}
    assert set(registrations) == {"blackarc", "systems"}
    for instance, account in (("blackarc", _A), ("systems", _B)):
        source = registrations[instance].adapter
        await source.inspect_source(InspectSource(connection_id=instance))
        page = await source.sync_source(SyncSource(connection_id=instance, page_size=10))
        [message] = page.objects
        content = await source.fetch_source(
            FetchSourceObject(
                connection_id=instance,
                object_id=message.object_id,
                version=message.version,
                max_bytes=10_000,
            )
        )
        assert account in content.content.decode()
        assert message.object_id.startswith(account.split("@")[0])


# --- the approved contract (Q3) --------------------------------------------------


def _unapproved(sink: _Sink) -> list[AuditEvent]:
    return sink.named("connector.tool.contract.unapproved")


async def test_an_approved_contract_is_not_reported_unapproved_on_restart(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    for _ in range(2):
        _runtime.reset()
        await world.start(_ONLY_A)
    assert _unapproved(world.sink) == []


async def test_a_changed_tool_set_is_suspended_until_one_approve(world: _World) -> None:
    """The deploy path: the bundle's tools change, each connection is approved once."""
    await world.connect("blackarc", _A, [_ONLY_A])
    manifest = world.root / "google_workspace" / "extension.toml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace(
            "description = \"List this account's Gmail labels (names, ids, types).",
            'description = "List the Gmail labels (names, ids, types).',
        ),
        encoding="utf-8",
    )

    registry = await world.start(_ONLY_A)
    # A changed description is suspended: absent until an operator approves it.
    assert "google_gmail_labels" not in registry.tools
    assert "google_gmail_search" in registry.tools

    approved = await world.connections().approve("blackarc")
    assert "google_gmail_labels" in approved
    _runtime.reset()
    registry = await world.start(_ONLY_A)
    assert _answer(await _call(registry, "google_gmail_labels"))["connection"] == "blackarc"
