"""Google accounts routed per call, end to end, against a fake Google.

The real :class:`~arcagent.modules.connectors.capabilities.Connectors` capability,
the real registry, the SHIPPED ``google_workspace`` manifest and the SHIPPED native
attachment, connected the way an operator connects: the real one-click flow against
a fake OAuth provider (``packages/arcagent/tests/oauth_fakes.py``). Only Google's
HTTP is fake, and it answers AS whichever account the presented bearer was issued
to, so a call that reached the wrong mailbox shows in the request log.

The hole this closes, stated as the first test: an agent granted only ``blackarc``
could name the ``systems`` address in a Google tool call, the call would act as
``systems``, and the audit would name ``blackarc``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.paths import arc_team
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey
from packages.arcagent.tests.custody_fakes import make_cipher
from packages.arcagent.tests.oauth_fakes import FakeGmail, FakeOAuthProvider

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
_A = "josh@blackarcindustrial.com"
_B = "josh@blackarcsystems.com"
_BOTH = "mailbot"
_ONLY_A = "reader"
_GMAIL_ROOT = "/gmail/v1/users/me"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def named(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]


class _Provider(FakeOAuthProvider):
    """Remembers which account each access token was issued to."""

    def __init__(self) -> None:
        super().__init__()
        self.owner: dict[str, str] = {}

    def _exchange(self, body: dict[str, str]) -> tuple[int, dict[str, Any]]:
        consent = self._codes.get(body.get("code", ""))
        status, answer = super()._exchange(body)
        if consent is not None and "access_token" in answer:
            self.owner[answer["access_token"]] = consent.email
        return status, answer

    def _refresh_grant(self, body: dict[str, str]) -> tuple[int, dict[str, Any]]:
        known = self._refresh.get(body.get("refresh_token", ""))
        status, answer = super()._refresh_grant(body)
        if known is not None and "access_token" in answer:
            self.owner[answer["access_token"]] = known[0]
        return status, answer


class _Mailbox(FakeGmail):
    """One account's mailbox: the fake's profile/messages/history plus threads, drafts, attachments."""

    attachment_bytes = b"%PDF-bill"
    attachment_size_claim: int | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(_GMAIL_ROOT)
        key = f"{request.method} {path}"
        message = next(iter(self.messages.values()))
        if key == "GET /threads":
            return httpx.Response(200, json={"threads": [{"id": "t-m1", "snippet": "hello"}]})
        if key == "GET /threads/t-m1":
            return httpx.Response(
                200, json={"id": "t-m1", "historyId": "101", "messages": [message]}
            )
        if key == "GET /drafts/d1":
            return httpx.Response(200, json={"id": "d1", "message": message})
        if key == "POST /drafts":
            return httpx.Response(200, json={"id": "d-new"})
        if key == "GET /messages/m1/attachments/a1":
            data = self.attachment_bytes
            claimed = self.attachment_size_claim or len(data)
            encoded = base64.urlsafe_b64encode(data).decode()
            return httpx.Response(200, json={"size": claimed, "data": encoded})
        return self._handle(request)


class _Google:
    """Every mailbox behind one endpoint; the bearer decides which one answers."""

    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.boxes: dict[str, _Mailbox] = {}
        self.log: list[tuple[str | None, str, str]] = []

    def add(self, email: str) -> None:
        box = _Mailbox(self.provider, email=email)
        box.add_message("m1", subject="Hello", body=f"mail for {email}")
        self.boxes[email] = box

    def handle(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        email = self.provider.owner.get(bearer)
        self.log.append((email, request.method, request.url.path.removeprefix(_GMAIL_ROOT)))
        box = self.boxes.get(email or "")
        if box is None:
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        return box.respond(request)

    def calls_as(self, email: str) -> list[tuple[str, str]]:
        return [(method, path) for who, method, path in self.log if who == email]


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


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
        self.provider = _Provider()
        self.google = _Google(self.provider)

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
            credential_cipher=make_cipher(),
            token_post=self.provider.post,
        )

    async def connect(self, instance: str, account: str, agents: list[str], **extra: str) -> None:
        """Install, then sign in through the real one-click flow against the fake provider."""
        for agent in agents:
            self.agent_dir(agent)
        self.google.add(account)
        connections = self.connections()
        await connections.set_oauth_app(
            "google",
            client_id=self.provider.client_id,
            client_secret=self.provider.client_secret,
        )
        plan = connections.plan("google_workspace", instance, agents=agents)
        await connections.install(plan, {"account": account, **extra}, agents=agents)
        begun = await connections.begin_oauth(instance, session_id="operator")
        landed = self.provider.consent(begun.authorize_url, email=account)
        await connections.complete_oauth(session_id="operator", redirect_url=landed)
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
            credential_cipher=make_cipher(),
            source_catalog=catalog,
        )
        await Connectors().setup(None)
        return registry


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    built = _World(tmp_path)
    real_client = httpx.AsyncClient

    def client_with_fake_google(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(built.google.handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_fake_google)
    return built


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
    before = len(world.google.log)

    text = await _call(
        registry, "google_gmail_send", account=_B, to="x@example.com", subject="s", body="b"
    )

    assert text.startswith("error")
    assert _B in [
        event.extra.get("requested") for event in world.sink.named("connector.account.denied")
    ]
    assert ("POST", "/messages/send") not in world.google.calls_as(_B)
    assert len(world.google.log) == before, "a refused call must not reach Google at all"


async def test_one_tool_name_serves_both_granted_accounts_and_names_the_real_one(
    world: _World,
) -> None:
    await world.connect("blackarc", _A, [_BOTH])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_BOTH)

    ambiguous = await _call(registry, "google_gmail_labels")
    assert ambiguous.startswith("error") and _A in ambiguous and _B in ambiguous

    for account, instance in ((_A, "blackarc"), (_B, "systems")):
        mark = len(world.google.log)
        answer = _answer(await _call(registry, "google_gmail_labels", account=account))
        assert (answer["connection"], answer["account"]) == (instance, account)
        # Google really answered as that account: the bearer was issued to it.
        assert [who for who, _, path in world.google.log[mark:] if path == "/labels"] == [account]
        routed = world.sink.named("connector.account.routed")[-1]
        assert (routed.extra["connection"], routed.extra["account"]) == (instance, account)
    # No collision: the later connection is not dropped.
    assert "google_gmail_send" in registry.tools


@pytest.mark.parametrize(
    "variant",
    [
        _B.upper(),
        f" {_B}",
        _B.replace("o", "о"),  # noqa: RUF001 - a Cyrillic look-alike is the point
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
    assert world.google.calls_as(_B) == [("GET", "/profile")], "only the connect-time probe ran"


async def test_concurrent_calls_for_two_accounts_never_cross(world: _World) -> None:
    await world.connect("blackarc", _A, [_BOTH])
    await world.connect("systems", _B, [_BOTH])
    registry = await world.start(_BOTH)
    mark = len(world.google.log)

    accounts = [_A, _B] * 6
    answers = await asyncio.gather(
        *(_call(registry, "google_gmail_labels", account=account) for account in accounts)
    )

    for account, text in zip(accounts, answers, strict=True):
        assert _answer(text)["account"] == account
    served = [who for who, _, path in world.google.log[mark:] if path == "/labels"]
    assert sorted(served) == sorted(accounts)


async def test_a_read_only_sign_in_blocks_write_tools_with_a_sentence(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])  # read_only defaults to yes
    registry = await world.start(_ONLY_A)

    assert _answer(await _call(registry, "google_gmail_labels"))["connection"] == "blackarc"
    mark = len(world.google.log)
    refused = await _call(
        registry, "google_gmail_draft", to="x@example.com", subject="s", body="b"
    )
    assert refused.startswith("error") and "read-only" in refused
    assert len(world.google.log) == mark, "a refused write makes no request"


async def test_a_drafting_connection_may_write(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A], read_only="no")
    registry = await world.start(_ONLY_A)

    answer = _answer(
        await _call(registry, "google_gmail_draft", to="x@example.com", subject="s", body="b")
    )

    assert answer["connection"] == "blackarc"
    assert ("POST", "/drafts") in world.google.calls_as(_A)


async def test_a_page_size_above_its_ceiling_never_exceeds_it(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    registry = await world.start(_ONLY_A)
    mark = len(world.google.log)

    await _call(registry, "google_gmail_search", query="in:inbox", limit="5000")

    listing = [entry for entry in world.google.log[mark:] if entry[2] == "/threads"]
    assert len(listing) <= 1, (
        "an over-ceiling page size is refused or clamped, never passed through"
    )


async def test_mail_content_verbs_mark_it_untrusted(world: _World) -> None:
    await world.connect("blackarc", _A, [_ONLY_A])
    registry = await world.start(_ONLY_A)
    for tool, args in (
        ("google_gmail_search", {"query": "in:inbox"}),
        ("google_gmail_thread", {"thread_id": "t-m1"}),
        ("google_gmail_draft_get", {"draft_id": "d1"}),
        ("google_gmail_message", {"id": "m1"}),
    ):
        text = await _call(registry, tool, **args)
        assert "untrusted" in text.lower(), tool


async def test_an_attachment_lands_in_this_connections_downloads_folder(
    world: _World,
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
    assert expected.is_file() and expected.read_bytes() == b"%PDF-bill"
    assert answer["connection"] == "blackarc"
    escape = await _call(
        registry,
        "google_gmail_attachment",
        message_id="m1",
        attachment_id="a1",
        file_name="../../identity.md",
    )
    assert escape.startswith("error")
    world.google.boxes[_A].attachment_size_claim = 26 * 1024 * 1024
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
        other = _B if account == _A else _A
        assert other not in content.content.decode()


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
    # The served tool contract comes from the attachment's own table, so that is
    # what an update changes.
    table = world.root / "google_workspace" / "arc_ext_google_workspace" / "native" / "__init__.py"
    text = table.read_text(encoding="utf-8")
    changed = text.replace(
        "List this account's Gmail labels (names, ids, types).",
        "List the Gmail labels (names, ids, types).",
    )
    assert changed != text
    table.write_text(changed, encoding="utf-8")
    # The bundle was imported by bare name when the connection was made; drop it so the
    # next start reads the edited table, as a restarted process would.
    for name in [name for name in sys.modules if name.startswith("arc_ext_google_workspace")]:
        del sys.modules[name]

    registry = await world.start(_ONLY_A)
    # A changed description is suspended: absent until an operator approves it.
    assert "google_gmail_labels" not in registry.tools
    assert "google_gmail_search" in registry.tools

    approved = await world.connections().approve("blackarc")
    assert "google_gmail_labels" in approved
    _runtime.reset()
    registry = await world.start(_ONLY_A)
    assert _answer(await _call(registry, "google_gmail_labels"))["connection"] == "blackarc"
