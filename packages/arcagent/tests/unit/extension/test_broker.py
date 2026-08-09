"""The credential broker — a connector never receives the credential (SPEC-062, D-575).

Every test here is written as a *search over what actually happened*, not as an
assertion about a call: a real child process is spawned and its environment is
dumped and scanned, a real loopback upstream records the bytes that reached it,
every file under the agent home is read, and every log record captured at DEBUG is
scanned. A broker that leaks through a path nobody thought to assert on still
fails here.

The two governing tests are :func:`test_child_process_never_sees_the_credential`
— which spawns a child, hands it only the handle and the endpoint, has it make a
real request, and proves the upstream saw the credential while the child's own
environment never held it — and
:func:`test_a_connector_cannot_choose_where_the_credential_goes`, which is the
confused-deputy case: the broker attaches a credential, so the one thing a hostile
connector must never be able to do is pick the recipient.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Self

import httpx
import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.extension.broker import MAX_BODY_BYTES, MAX_HEAD_BYTES, CredentialBroker
from arcagent.extension.launcher import NoConfinement, ProcessDefinition, ProcessLauncher
from arcagent.extension.secrets import LocalFileSecretBackend, SecretRef, SecretStore

CREDENTIAL = "atlassian-refresh-tok-9f2c4e7a1b8d6"
CALLER = "did:arc:agent:coder"

#: A child that behaves like a connector: it is given a localhost endpoint and a
#: handle, makes a real request through them, and reports its whole environment so
#: a test can scan what it was actually able to see.
_CONNECTOR_CHILD = """
import json, os, urllib.request

request = urllib.request.Request(
    os.environ["ARC_BROKER_ENDPOINT"] + "/rest/api/3/issue",
    data=b'{"summary":"from the connector"}',
    headers={
        "authorization": "Bearer " + os.environ["ARC_BROKER_HANDLE"],
        "content-type": "application/json",
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    answer = response.read().decode()
print(json.dumps({"env": dict(os.environ), "answer": answer}))
"""


@dataclasses.dataclass(frozen=True)
class ReceivedRequest:
    """One request as it actually arrived at the upstream."""

    method: str
    target: str
    headers: dict[str, str]
    body: bytes


class RecordingUpstream:
    """A real loopback HTTP server that records exactly what reached it."""

    def __init__(
        self, *, status: int = 200, body: bytes = b'{"key":"ARC-1"}', location: str = ""
    ) -> None:
        self.status = status
        self.body = body
        self.location = location
        self.requests: list[ReceivedRequest] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append(ReceivedRequest(method, target, headers, body))

        head_lines = [f"HTTP/1.1 {self.status} OK", "content-type: application/json"]
        if self.location:
            head_lines.append(f"location: {self.location}")
        head_lines += [f"content-length: {len(self.body)}", "connection: close", "", ""]
        writer.write("\r\n".join(head_lines).encode("latin-1") + self.body)
        await writer.drain()
        writer.close()


class RecordingSink:
    """Audit sink that keeps every event so a test can scan it for the value."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """An agent home holding the config files a credential must never reach."""
    home = tmp_path / "team" / "coder"
    home.mkdir(parents=True)
    (home / "arcagent.toml").write_text('[identity]\ndid = "did:arc:agent:coder"\n')
    (home / "context.md").write_text("# open loops\n")
    return home


@pytest.fixture
def env_file(agent_home: Path) -> Path:
    return agent_home / "arc.env"


@pytest.fixture
def ref() -> SecretRef:
    return SecretRef(connection="atlassian_work", field="access_token")


@pytest.fixture
async def secrets(env_file: Path, ref: SecretRef) -> SecretStore:
    store = SecretStore(LocalFileSecretBackend(env_file))
    await store.put(ref, CREDENTIAL, caller_did=CALLER)
    return store


async def _raw_status(endpoint: str, raw: str) -> str:
    """Send bytes an HTTP client would refuse to build, and return the status line.

    A broker that never answers is a failure, not a reason to wait: a parser that
    trusts a declared length instead of bounding it blocks here forever, so the wait
    is bounded and the timeout is reported as the status.
    """
    host, _, port = endpoint.removeprefix("http://").partition(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    try:
        writer.write(raw.encode("latin-1"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10)
    except TimeoutError:
        return "no answer (the broker never replied)"
    finally:
        writer.close()
    return line.decode("latin-1")


def _files_containing(root: Path, needle: str) -> list[Path]:
    """Every file under ``root`` whose bytes contain ``needle``."""
    hits: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if needle.encode("utf-8") in path.read_bytes():
            hits.append(path)
    return hits


# ---------------------------------------------------------------------------
# The invariant: the connector holds a handle, the broker holds the credential
# ---------------------------------------------------------------------------


async def test_a_grant_carries_no_credential(secrets: SecretStore, ref: SecretRef) -> None:
    """Everything handed to the connector is scanned — none of it is the value."""
    async with CredentialBroker(secrets) as broker:
        grant = await broker.issue(
            ref, upstream="https://example.atlassian.net", caller_did=CALLER
        )

        assert CREDENTIAL not in json.dumps(dataclasses.asdict(grant))
        assert CREDENTIAL not in repr(grant)
        assert grant.handle and grant.handle != CREDENTIAL
        assert grant.endpoint.startswith("http://127.0.0.1:")


async def test_handles_are_unguessable_and_unique(secrets: SecretStore, ref: SecretRef) -> None:
    """A handle is a bearer token for the credential, so it carries real entropy."""
    async with CredentialBroker(secrets) as broker:
        handles = {
            (await broker.issue(ref, upstream="https://example.test", caller_did=CALLER)).handle
            for _ in range(8)
        }

        assert len(handles) == 8
        assert all(len(handle) >= 40 for handle in handles)


async def test_the_broker_attaches_the_real_credential_on_the_way_out(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """The connector sends the handle; the upstream receives the credential."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

        async with httpx.AsyncClient() as connector:
            answer = await connector.post(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
                content=b'{"summary":"x"}',
            )

        assert answer.status_code == 200
        assert answer.content == b'{"key":"ARC-1"}'
        received = upstream.requests[-1]
        assert received.headers["authorization"] == f"Bearer {CREDENTIAL}"
        assert received.method == "POST"
        assert received.target == "/rest/api/3/issue"
        assert received.body == b'{"summary":"x"}'
        # The handle went to the broker and stopped there.
        assert grant.handle not in json.dumps(received.headers)


async def test_child_process_never_sees_the_credential(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """A real connector process: it works, and its whole environment is scanned.

    This is the governing test. The child is spawned through the one launcher
    every extension process uses, is given only the endpoint and the handle, makes
    a real call that reaches the upstream with the credential attached — and its
    own environment, dumped by the child itself, holds no part of the value.
    """
    launcher = ProcessLauncher(policy=NoConfinement())
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)
        definition = ProcessDefinition(
            key="atlassian_work",
            argv=[sys.executable, "-c", _CONNECTOR_CHILD],
            env={"ARC_BROKER_ENDPOINT": grant.endpoint, "ARC_BROKER_HANDLE": grant.handle},
        )
        handle = await launcher.acquire(definition)
        try:
            stdout, stderr = await asyncio.wait_for(handle.process.communicate(), timeout=30)
        finally:
            await launcher.shutdown()

    assert handle.process.returncode == 0, stderr.decode()
    reported = json.loads(stdout.decode())

    assert reported["answer"] == '{"key":"ARC-1"}', "the connector's call must have worked"
    assert upstream.requests[-1].headers["authorization"] == f"Bearer {CREDENTIAL}"

    child_environment = reported["env"]
    assert child_environment["ARC_BROKER_HANDLE"] == grant.handle
    leaked = [name for name, value in child_environment.items() if CREDENTIAL in value]
    assert not leaked, f"the credential reached the child environment via {leaked}"
    assert CREDENTIAL not in json.dumps(child_environment)


# ---------------------------------------------------------------------------
# Fail closed: no path hands the raw value over instead
# ---------------------------------------------------------------------------


async def test_issue_refuses_when_no_credential_is_stored(env_file: Path) -> None:
    """Nothing to hold means no grant — never a grant that resolves to nothing."""
    store = SecretStore(LocalFileSecretBackend(env_file))
    unstored = SecretRef(connection="never_authorized", field="access_token")

    async with CredentialBroker(store) as broker:
        with pytest.raises(ExtensionError) as excinfo:
            await broker.issue(unstored, upstream="https://example.test", caller_did=CALLER)

    assert excinfo.value.code == "BROKER_CREDENTIAL_MISSING"


async def test_issue_refuses_before_the_broker_is_listening(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """A grant names an endpoint, so there is no grant without one."""
    broker = CredentialBroker(secrets)

    with pytest.raises(ExtensionError) as excinfo:
        await broker.issue(ref, upstream="https://example.test", caller_did=CALLER)

    assert excinfo.value.code == "BROKER_NOT_STARTED"


async def test_an_unknown_handle_reaches_no_upstream(secrets: SecretStore, ref: SecretRef) -> None:
    """A refused request is refused before anything is attached to it."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

        async with httpx.AsyncClient() as connector:
            answer = await connector.get(
                f"{broker.endpoint}/rest/api/3/issue",
                headers={"authorization": "Bearer not-a-handle"},
            )

        assert answer.status_code == 401
        assert upstream.requests == []


async def test_a_revoked_handle_reaches_no_upstream(secrets: SecretStore, ref: SecretRef) -> None:
    """Revocation is immediate: the next call is refused, not merely the next grant."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

        assert await broker.revoke(grant.handle) is True
        assert await broker.revoke(grant.handle) is False

        async with httpx.AsyncClient() as connector:
            answer = await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
            )

        assert answer.status_code == 401
        assert upstream.requests == []


async def test_a_deleted_credential_fails_the_call(secrets: SecretStore, ref: SecretRef) -> None:
    """The credential is resolved per call, so its removal stops the next one."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)
        await secrets.delete(ref, caller_did=CALLER)

        async with httpx.AsyncClient() as connector:
            answer = await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
            )

        assert answer.status_code == 503
        assert upstream.requests == []


async def test_closing_the_broker_revokes_every_grant(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """Nothing outlives the broker that was holding it."""
    broker = CredentialBroker(secrets)
    await broker.start()
    endpoint = broker.endpoint
    grant = await broker.issue(ref, upstream="https://example.test", caller_did=CALLER)
    await broker.aclose()

    async with httpx.AsyncClient() as connector:
        with pytest.raises(httpx.ConnectError):
            await connector.get(
                f"{endpoint}/anything", headers={"authorization": f"Bearer {grant.handle}"}
            )


# ---------------------------------------------------------------------------
# The confused deputy: a connector must not choose where the credential goes
# ---------------------------------------------------------------------------


async def test_a_connector_cannot_choose_where_the_credential_goes(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """An absolute request target is refused, so the pin cannot be re-aimed."""
    async with RecordingUpstream() as pinned, RecordingUpstream() as attacker:
        async with CredentialBroker(secrets) as broker:
            grant = await broker.issue(ref, upstream=pinned.origin, caller_did=CALLER)
            status = await _raw_status(
                grant.endpoint,
                f"GET {attacker.origin}/steal HTTP/1.1\r\n"
                f"host: 127.0.0.1\r\n"
                f"authorization: Bearer {grant.handle}\r\n\r\n",
            )

        assert "400" in status
        assert attacker.requests == [], "the credential must never be aimed at another host"
        assert pinned.requests == []


@pytest.mark.parametrize(
    "target", ["/../../admin", "/%2e%2e/admin", "/v1/../../admin", "/v1/%2E%2E/%2e%2e/admin"]
)
async def test_a_connector_cannot_climb_out_of_a_path_narrowed_pin(
    secrets: SecretStore, ref: SecretRef, target: str
) -> None:
    """A pin narrowed to a path prefix is a boundary, so ``..`` cannot walk out of it.

    Percent-encoded spellings are the standard way past a literal check, so they are
    exercised alongside the plain one.
    """
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(
            ref, upstream=f"{upstream.origin}/v3/repos/acme", caller_did=CALLER
        )

        status = await _raw_status(
            grant.endpoint,
            f"GET {target} HTTP/1.1\r\nhost: x\r\nauthorization: Bearer {grant.handle}\r\n\r\n",
        )

        assert "400" in status, f"{target} was not refused: {status!r}"
        assert upstream.requests == [], f"{target} carried a credential outside the pin"


async def test_a_forged_host_header_does_not_reach_the_upstream(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """The upstream sees its own authority, not one the connector supplied."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

        async with httpx.AsyncClient() as connector:
            await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={
                    "authorization": f"Bearer {grant.handle}",
                    "host": "attacker.example",
                },
            )

        assert upstream.requests[-1].headers["host"] == f"127.0.0.1:{upstream.port}"


async def test_a_redirect_does_not_carry_the_credential_onward(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """A 302 is handed back, never followed — following it would forward the value."""
    async with RecordingUpstream() as attacker:
        async with RecordingUpstream(
            status=302, body=b"", location=f"{attacker.origin}/steal"
        ) as upstream:
            async with CredentialBroker(secrets) as broker:
                grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

                async with httpx.AsyncClient(follow_redirects=False) as connector:
                    answer = await connector.get(
                        f"{grant.endpoint}/rest/api/3/issue",
                        headers={"authorization": f"Bearer {grant.handle}"},
                    )

        assert answer.status_code == 302
        assert attacker.requests == []


# ---------------------------------------------------------------------------
# Malformed framing is refused, never guessed at
# ---------------------------------------------------------------------------

#: Requests an HTTP client would not build. Each one, read charitably, becomes a
#: *different* call than the connector wrote — and the broker would put a live
#: credential on it. Refusing is the only safe reading.
_MALFORMED = {
    "chunked framing, which this parser does not read": (
        "POST /rest/api/3/issue HTTP/1.1\r\nhost: x\r\nauthorization: Bearer {handle}\r\n"
        "transfer-encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    ),
    "two content-length headers": (
        "POST /rest/api/3/issue HTTP/1.1\r\nhost: x\r\nauthorization: Bearer {handle}\r\n"
        "content-length: 5\r\ncontent-length: 6\r\n\r\nhello"
    ),
    "a content-length that is not a number": (
        "POST /x HTTP/1.1\r\nauthorization: Bearer {handle}\r\ncontent-length: 5x\r\n\r\nhello"
    ),
    "a content-length past the body ceiling": (
        "POST /x HTTP/1.1\r\nauthorization: Bearer {handle}\r\n"
        f"content-length: {MAX_BODY_BYTES + 1}\r\n\r\n"
    ),
    "a request line that is not one": "GARBAGE\r\nauthorization: Bearer {handle}\r\n\r\n",
    "an obs-fold continuation line": (
        "GET /x HTTP/1.1\r\nauthorization: Bearer {handle}\r\nx-note: a\r\n b\r\n\r\n"
    ),
    "a head past the ceiling": (
        "GET /x HTTP/1.1\r\nauthorization: Bearer {handle}\r\nx-pad: "
        + "A" * MAX_HEAD_BYTES
        + "\r\n\r\n"
    ),
}


@pytest.mark.parametrize("shape", list(_MALFORMED), ids=list(_MALFORMED))
async def test_malformed_framing_reaches_no_upstream(
    secrets: SecretStore, ref: SecretRef, shape: str
) -> None:
    """A live handle does not buy a request the parser cannot read exactly."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

        status = await _raw_status(grant.endpoint, _MALFORMED[shape].format(handle=grant.handle))

        assert "400" in status, f"{shape} was not refused: {status!r}"
        assert upstream.requests == [], f"{shape} carried a credential upstream"


async def test_a_request_with_no_handle_is_refused(secrets: SecretStore, ref: SecretRef) -> None:
    """Nothing reaches the upstream on an unauthenticated request."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)

        status = await _raw_status(broker.endpoint, "GET /x HTTP/1.1\r\nhost: x\r\n\r\n")

        assert "401" in status
        assert upstream.requests == []


# ---------------------------------------------------------------------------
# The value exists in the store and in flight, and nowhere else
# ---------------------------------------------------------------------------


async def test_no_artifact_on_disk_holds_the_credential(
    secrets: SecretStore, ref: SecretRef, env_file: Path, tmp_path: Path
) -> None:
    """After a full cycle, only the secret store itself holds the value."""
    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)
        async with httpx.AsyncClient() as connector:
            await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
            )

    hits = _files_containing(tmp_path, CREDENTIAL)
    assert hits == [env_file], f"credential leaked into {[str(path) for path in hits]}"
    assert _files_containing(tmp_path, grant.handle) == [], "the handle is not persisted either"


async def test_nothing_logs_the_credential(
    secrets: SecretStore, ref: SecretRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A whole cycle at DEBUG — including the refusals — never emits the value."""
    caplog.set_level(logging.DEBUG)

    async with RecordingUpstream() as upstream, CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)
        async with httpx.AsyncClient() as connector:
            await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
            )
            await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": "Bearer not-a-handle"},
            )
        await broker.revoke(grant.handle)

    for record in caplog.records:
        assert CREDENTIAL not in record.getMessage()
        assert CREDENTIAL not in str(record.args)


async def test_audit_records_the_coordinate_never_the_value_or_the_handle(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """Grant and attach are auditable events that carry neither bearer token."""
    sink = RecordingSink()
    async with RecordingUpstream() as upstream, CredentialBroker(secrets, sink=sink) as broker:
        grant = await broker.issue(ref, upstream=upstream.origin, caller_did=CALLER)
        async with httpx.AsyncClient() as connector:
            await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
            )

    actions = {event.action for event in sink.events}
    assert "credential.broker_issue" in actions
    assert "credential.broker_attach" in actions
    for event in sink.events:
        rendered = event.model_dump_json()
        assert CREDENTIAL not in rendered
        assert grant.handle not in rendered
        assert event.actor_did == CALLER
        assert "atlassian_work" in event.target


async def test_an_upstream_must_be_an_absolute_origin(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """A pin that is not an origin is not a pin, so it is refused at issue time."""
    async with CredentialBroker(secrets) as broker:
        for upstream in ("/rest/api", "ftp://example.test", "example.test", ""):
            with pytest.raises(ExtensionError) as excinfo:
                await broker.issue(ref, upstream=upstream, caller_did=CALLER)
            assert excinfo.value.code == "BROKER_UPSTREAM_INVALID"


async def test_an_unreachable_upstream_fails_the_call(
    secrets: SecretStore, ref: SecretRef
) -> None:
    """The broker held the credential; the connector gets an error, not the value."""
    async with RecordingUpstream() as upstream:
        origin = upstream.origin
    # The upstream is now closed, so the pinned origin refuses connections.

    async with CredentialBroker(secrets) as broker:
        grant = await broker.issue(ref, upstream=origin, caller_did=CALLER)
        async with httpx.AsyncClient() as connector:
            answer = await connector.get(
                f"{grant.endpoint}/rest/api/3/issue",
                headers={"authorization": f"Bearer {grant.handle}"},
            )

    assert answer.status_code == 502
    assert CREDENTIAL not in answer.text
