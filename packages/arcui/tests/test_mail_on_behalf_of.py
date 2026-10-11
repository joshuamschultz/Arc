"""SPEC-087 T-1285: mail sent for a person names the person and the app (REQ-570).

COMP-009 / D-827: the transport sender stays the deployment's operator mail
identity (it signs and receives replies). The acting person goes in the signed
``Message.meta.on_behalf_of`` and the app in ``Message.meta.via``. The inbox
thread JSON returns ``on_behalf_of`` and ``via`` per message so the web inbox
can show 'from <person> via <app>'.

Covered surfaces:

* ``POST /api/agents/{id}/inbox`` (compose) by an app acting for a person, by a
  signed-in person, and by the static operator token;
* ``GET /api/agents/{id}/inbox/{thread_id}`` (the thread read model);
* ``POST /api/team/tasks`` task-assignment mail to the owner agent.

The signed envelope is read from the durable outbox row the inbox repository
commits atomically with the inbox copy: that row is exactly what the delivery
worker later puts on the wire.

Real ``create_app`` + real ``AuthMiddleware`` + real ``UserStore`` and
``PrincipalStore`` (``principal_fixtures``); real ``AgentMailService``,
``DurableInboxService``, ``MailOutbox`` and ``TaskStore``. Only the NATS
transport (records what it would put on the wire) and the inbox repository
(the shared contract fake) are doubles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from arcstore.inbox_projection import DurableInboxService
from arcstore.mail_outbox import MailOutbox
from arcstore.tasks import TaskStore
from arcteam.crypto import MessageSigner, verify_message
from arcteam.mail import AgentMailService
from arcteam.types import Message
from arctrust import AgentIdentity
from arctrust.principals import Scope
from arctrust.users import OPERATOR, VIEWER
from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    IssuedPrincipal,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

OLIVIA = agent_did()
OBO = "X-Arc-On-Behalf-Of"
COMPOSE = "/api/agents/olivia/inbox"
OPERATOR_ADDRESS = "user://operator"


# --- doubles at the external boundary ----------------------------------------------


class RecordingTransport:
    """The NATS wire: records every signed envelope it is handed."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def send(self, message: Message) -> Message:
        self.messages.append(message)
        return message


class AddressBook:
    """Registry stand-in: operator and Olivia transport addresses <-> DIDs."""

    def __init__(self, operator_did: str) -> None:
        self._addresses = {OPERATOR_ADDRESS: operator_did, "agent://olivia": OLIVIA}

    async def did_for(self, address: str) -> str:
        return address if address.startswith("did:") else self._addresses[address]

    async def address_for(self, did: str) -> str:
        return next(addr for addr, value in self._addresses.items() if value == did)


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def team(tmp_path: Path) -> Path:
    return write_team(tmp_path / "team", {"olivia": OLIVIA})


class Harness:
    """One built app with the operator mail identity wired to a recording wire."""

    def __init__(self, built: PrincipalApp, tmp_path: Path, backend: Any) -> None:
        self.built = built
        self.operator = AgentIdentity.generate("test", "operator-mail")
        self.transport = RecordingTransport()
        self.repository = FakeInboxRepository()
        self.inbox = DurableInboxService(self.repository)
        state = built.app.state
        state.inbox_service = self.inbox
        state.inbox_clearance = "UNCLASSIFIED"
        state.agent_mail = AgentMailService(
            self.transport,
            self.inbox,
            outbox=MailOutbox(tmp_path / "mail-outbox.jsonl"),
            address_book=AddressBook(self.operator.did),
            signer=MessageSigner.from_identity(self.operator),
        )
        state.task_store = TaskStore(backend)
        self.client = TestClient(built.app, raise_server_exceptions=False)

    def sent(self) -> Message:
        """The one signed envelope committed for the wire (atomic with the inbox copy).

        Asserts it is the operator mail identity's own valid signature first.
        """
        (envelope,) = self.repository.mail_outbox.values()
        message = Message.model_validate(envelope)
        assert message.sender == OPERATOR_ADDRESS
        assert message.signer_did == self.operator.did
        assert verify_message(message, self.operator.public_key) is True
        return message

    def thread_messages(self, thread_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
        resp = self.client.get(f"{COMPOSE}/{thread_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        messages: list[dict[str, Any]] = resp.json()["messages"]
        return messages


@pytest.fixture
def h(tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, arcstore_backend: Any) -> Harness:
    built = build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team
    )
    return Harness(built, tmp_path, arcstore_backend)


def _session(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _inbox_app(rig: PrincipalRig, person: str) -> IssuedPrincipal:
    return issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=(person,))


def _tasks_app(rig: PrincipalRig, person: str) -> IssuedPrincipal:
    return issue_principal(rig.open(), scopes={Scope.TASKS_WRITE: (OLIVIA,)}, subjects=(person,))


MAIL = {"subject": "Q3 close", "body": "Please own the Q3 close."}


# --- inbox compose -------------------------------------------------------------------


class TestComposeMeta:
    def test_app_for_person_signs_person_and_app_into_mail_meta(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        _, ada = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _inbox_app(rig, ada)

        resp = h.client.post(
            COMPOSE, json=MAIL, headers=app.bearer_headers(**{OBO: ada, "Idempotency-Key": "c-1"})
        )

        assert resp.status_code == 201, resp.text
        meta = h.sent().meta
        assert meta["on_behalf_of"] == ada
        assert meta["via"] == app.did

    def test_app_for_a_viewer_cannot_send_mail_the_viewer_could_not(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        """REQ-562 / DEV-9: the person's roles cap the app. A viewer cannot
        compose to an agent, so an app acting for that viewer cannot either,
        even though the app holds inbox:send for the agent."""
        _, vic = h.built.sign_in("vic@example.com", (VIEWER,))
        app = _inbox_app(rig, vic)

        resp = h.client.post(
            COMPOSE, json=MAIL, headers=app.bearer_headers(**{OBO: vic, "Idempotency-Key": "c-1"})
        )

        assert resp.status_code == 403, resp.text
        assert h.repository.mail_outbox == {}

    def test_signed_in_person_is_on_behalf_of_with_no_app(self, h: Harness) -> None:
        token, ada = h.built.sign_in("ada@example.com", (OPERATOR,))

        resp = h.client.post(COMPOSE, json=MAIL, headers=_session(token, "c-1"))

        assert resp.status_code == 201, resp.text
        meta = h.sent().meta
        assert meta["on_behalf_of"] == ada
        assert "via" not in meta

    def test_session_person_cannot_name_someone_else_with_the_header(self, h: Harness) -> None:
        token, ada = h.built.sign_in("ada@example.com", (OPERATOR,))
        _, bob = h.built.sign_in("bob@example.com", (OPERATOR,))

        resp = h.client.post(COMPOSE, json=MAIL, headers={**_session(token, "c-1"), OBO: bob})

        assert resp.status_code == 201, resp.text
        meta = h.sent().meta
        assert meta["on_behalf_of"] == ada
        assert "via" not in meta


# --- thread read model ----------------------------------------------------------------


class TestThreadJson:
    def test_thread_returns_person_and_app_for_app_compose(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        token, ada = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _inbox_app(rig, ada)
        sent = h.client.post(
            COMPOSE, json=MAIL, headers=app.bearer_headers(**{OBO: ada, "Idempotency-Key": "c-1"})
        )
        assert sent.status_code == 201, sent.text

        (message,) = h.thread_messages(sent.json()["thread_id"], _session(token))

        assert message["on_behalf_of"] == ada
        assert message["via"] == app.did
        assert message["sender"]["participant_id"] == h.operator.did

    def test_thread_returns_person_and_null_app_for_session_compose(self, h: Harness) -> None:
        token, ada = h.built.sign_in("ada@example.com", (OPERATOR,))
        sent = h.client.post(COMPOSE, json=MAIL, headers=_session(token, "c-1"))
        assert sent.status_code == 201, sent.text

        (message,) = h.thread_messages(sent.json()["thread_id"], _session(token))

        assert message["on_behalf_of"] == ada
        assert message["via"] is None
        assert message["sender"]["participant_id"] == h.operator.did

    def test_static_operator_token_names_no_person(self, h: Harness) -> None:
        sent = h.client.post(COMPOSE, json=MAIL, headers=_session(OPERATOR_TOKEN, "c-1"))
        assert sent.status_code == 201, sent.text

        meta = h.sent().meta
        assert "on_behalf_of" not in meta
        assert "via" not in meta
        (message,) = h.thread_messages(sent.json()["thread_id"], _session(OPERATOR_TOKEN))
        assert message["on_behalf_of"] is None
        assert message["via"] is None


# --- task-assignment mail -------------------------------------------------------------


class TestTaskAssignmentMail:
    TASK: ClassVar[dict[str, str]] = {"title": "Close Q3", "owner_did": OLIVIA}

    def test_task_by_session_person_mails_owner_on_behalf_of_person(self, h: Harness) -> None:
        token, ada = h.built.sign_in("ada@example.com", (OPERATOR,))

        resp = h.client.post("/api/team/tasks", json=self.TASK, headers=_session(token))

        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_notification"] in {"sent", "pending"}
        meta = h.sent().meta
        assert meta["on_behalf_of"] == ada
        assert "via" not in meta

    def test_task_by_app_for_person_mails_owner_with_person_and_app(
        self, h: Harness, rig: PrincipalRig
    ) -> None:
        token, ada = h.built.sign_in("ada@example.com", (OPERATOR,))
        app = _tasks_app(rig, ada)

        resp = h.client.post(
            "/api/team/tasks", json=self.TASK, headers=app.bearer_headers(**{OBO: ada})
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_notification"] in {"sent", "pending"}
        meta = h.sent().meta
        assert meta["on_behalf_of"] == ada
        assert meta["via"] == app.did
        threads = h.client.get(COMPOSE, headers=_session(token)).json()["threads"]
        (thread,) = threads
        (message,) = h.thread_messages(thread["thread_id"], _session(token))
        assert message["on_behalf_of"] == ada
        assert message["via"] == app.did
