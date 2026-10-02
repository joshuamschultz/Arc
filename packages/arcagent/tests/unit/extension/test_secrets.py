"""SecretStore seam over sealed custody rows (SPEC-062 COMP-010, P18-2).

Covers REQ-265 (a connector secret reaches only the secret store and never a config
file, a log, a prompt, or model context). The store is the sealed custody row, so
the artifacts searched for a leak are the agent-home files, every log record, every
audit event, every exception rendering, and the raw stored row JSON.

The leak tests are the point of this file, so they are written as *searches over
artifacts* rather than as assertions about one call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import make_cipher

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import (
    CREDENTIAL_COLLECTION,
    CredentialRowStore,
    SealedCredentialBackend,
)
from arcagent.extension.secrets import Secret, SecretBackend, SecretRef, SecretStore

SECRET_VALUE = "atlassian-refresh-tok-9f2c4e7a1b8d6"
CALLER = "did:arc:agent:coder"


class RecordingSink:
    """Audit sink that keeps every event so a test can scan it for the value."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """An agent home holding the config files a secret must never reach."""
    home = tmp_path / "team" / "coder"
    home.mkdir(parents=True)
    (home / "arcagent.toml").write_text('[identity]\ndid = "did:arc:agent:coder"\n')
    (home / "gateway.toml").write_text("[platforms.coder_telegram]\nenabled = true\n")
    (home / "context.md").write_text("# open loops\n")
    return home


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def rows(backend: FakeBackend) -> CredentialRowStore:
    return CredentialRowStore(backend, make_cipher())


@pytest.fixture
def ref() -> SecretRef:
    return SecretRef(connection="atlassian_work", field="refresh_token")


@pytest.fixture
def sealed_store(rows: CredentialRowStore) -> SecretStore:
    return SecretStore(SealedCredentialBackend(rows))


def _files_containing(root: Path, needle: str) -> list[Path]:
    """Every file under ``root`` whose bytes contain ``needle``."""
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and needle.encode("utf-8") in path.read_bytes()
    ]


async def _raw_row_json(backend: FakeBackend, connection: str) -> str:
    raw = await backend.mutable_read(CREDENTIAL_COLLECTION, connection)
    return json.dumps(raw, sort_keys=True)


# ---------------------------------------------------------------------------
# REQ-265 — the value reaches the store and nothing else
# ---------------------------------------------------------------------------


async def test_value_is_written_only_to_the_secret_store(
    sealed_store: SecretStore, agent_home: Path, ref: SecretRef
) -> None:
    """No file in the agent home holds the value; the store opens it back."""
    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    assert _files_containing(agent_home, SECRET_VALUE) == []
    resolved = await sealed_store.get(ref, caller_did=CALLER)
    assert resolved is not None
    assert resolved.reveal() == SECRET_VALUE


async def test_stored_row_is_sealed_never_plaintext(
    sealed_store: SecretStore, backend: FakeBackend, rows: CredentialRowStore, ref: SecretRef
) -> None:
    """The raw row exists in custody, opens through the row store, and holds no plaintext."""
    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    row = await rows.read(ref.connection)
    assert row is not None
    opened = rows.open_field(row, ref.field)
    assert opened is not None
    assert opened.reveal() == SECRET_VALUE
    assert SECRET_VALUE not in await _raw_row_json(backend, ref.connection)


async def test_nothing_logs_the_value(
    sealed_store: SecretStore, ref: SecretRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A full write/read/delete cycle at DEBUG never emits the value to a log."""
    caplog.set_level(logging.DEBUG)

    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    await sealed_store.get(ref, caller_did=CALLER)
    await sealed_store.delete(ref, caller_did=CALLER)

    for record in caplog.records:
        assert SECRET_VALUE not in record.getMessage()
        assert SECRET_VALUE not in str(record.args)


async def test_audit_events_record_coordinates_never_the_value(
    rows: CredentialRowStore, ref: SecretRef
) -> None:
    """The credential carve-out: store, item, field, caller, outcome — no value."""
    sink = RecordingSink()
    store = SecretStore(SealedCredentialBackend(rows), sink=sink)

    await store.put(ref, SECRET_VALUE, caller_did=CALLER)
    await store.get(ref, caller_did=CALLER)

    assert sink.events, "a credential write and read must be auditable"
    for event in sink.events:
        assert SECRET_VALUE not in event.model_dump_json()
        assert event.actor_did == CALLER
        assert "atlassian_work" in event.target


def test_secret_is_opaque_to_every_rendering() -> None:
    """A Secret cannot leak by being interpolated into a prompt, a log, or a repr."""
    secret = Secret(SECRET_VALUE)

    assert SECRET_VALUE not in repr(secret)
    assert SECRET_VALUE not in str(secret)
    assert SECRET_VALUE not in f"token={secret}"
    assert SECRET_VALUE not in "{}".format(secret)  # noqa: UP032 — exercises __format__
    assert secret.reveal() == SECRET_VALUE


async def test_rejected_value_is_absent_from_the_error(
    sealed_store: SecretStore, ref: SecretRef
) -> None:
    """A refusal names the coordinate, never the rejected material."""
    poisoned = f"{SECRET_VALUE}\nARC_SECRET_CODER_OTHER_TOKEN=injected"

    with pytest.raises(ExtensionError) as excinfo:
        await sealed_store.put(ref, poisoned, caller_did=CALLER)

    rendered = f"{excinfo.value}{excinfo.value.details}"
    assert SECRET_VALUE not in rendered
    assert "injected" not in rendered


async def test_a_refused_value_is_never_stored(
    sealed_store: SecretStore, backend: FakeBackend, ref: SecretRef
) -> None:
    """A control character is refused before anything reaches custody."""
    with pytest.raises(ExtensionError):
        await sealed_store.put(ref, "tok\nARC_SECRET_CODER_OTHER_TOKEN=stolen", caller_did=CALLER)

    assert await backend.mutable_read(CREDENTIAL_COLLECTION, ref.connection) is None


# ---------------------------------------------------------------------------
# Keying — (connection, field)
# ---------------------------------------------------------------------------


async def test_secrets_are_keyed_by_connection_and_field(
    sealed_store: SecretStore, ref: SecretRef
) -> None:
    """Two coordinates, distinct slots — nothing shares a cell.

    No agent appears in the key, which is what makes a grant a grant: two agents
    granted one account read the same entry rather than each holding a copy.
    """
    others = [
        SecretRef(connection="atlassian_personal", field="refresh_token"),
        SecretRef(connection="atlassian_work", field="client_secret"),
    ]
    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    for index, other in enumerate(others):
        await sealed_store.put(other, f"other-{index}", caller_did=CALLER)

    resolved = await sealed_store.get(ref, caller_did=CALLER)
    assert resolved is not None
    assert resolved.reveal() == SECRET_VALUE
    for index, other in enumerate(others):
        stored = await sealed_store.get(other, caller_did=CALLER)
        assert stored is not None
        assert stored.reveal() == f"other-{index}"


async def test_missing_secret_resolves_to_none(sealed_store: SecretStore, ref: SecretRef) -> None:
    assert await sealed_store.get(ref, caller_did=CALLER) is None


async def test_present_names_stored_fields_without_opening_them(
    sealed_store: SecretStore, ref: SecretRef
) -> None:
    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    assert await sealed_store.present(ref.connection) == frozenset({ref.field})
    assert await sealed_store.present("nothing_here") == frozenset()


async def test_delete_removes_the_value_from_the_store(
    sealed_store: SecretStore, backend: FakeBackend, ref: SecretRef
) -> None:
    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    assert await sealed_store.delete(ref, caller_did=CALLER) is True
    assert await sealed_store.get(ref, caller_did=CALLER) is None
    assert SECRET_VALUE not in await _raw_row_json(backend, ref.connection)
    assert await sealed_store.delete(ref, caller_did=CALLER) is False


@pytest.mark.parametrize(
    "connection,field",
    [
        ("../../etc", "refresh_token"),
        ("atlassian work", "refresh_token"),
        ("atlassian=work", "refresh_token"),
        ("atlassian\nwork", "refresh_token"),
        ("", "refresh_token"),
        ("atlassian_work", ""),
        ("a" * 65, "refresh_token"),
    ],
)
def test_coordinates_that_could_escape_their_cell_are_refused(connection: str, field: str) -> None:
    """A coordinate is bound into the ciphertext and a row key — it is untrusted input."""
    with pytest.raises(ExtensionError):
        SecretRef(connection=connection, field=field)


def test_case_variants_cannot_collide_into_one_cell() -> None:
    """Uppercase would fold onto the same cell and read another connection's secret."""
    with pytest.raises(ExtensionError):
        SecretRef(connection="Atlassian_Work", field="refresh_token")


# ---------------------------------------------------------------------------
# Durability — concurrent writers cannot lose each other
# ---------------------------------------------------------------------------


async def test_a_second_write_preserves_the_first(
    sealed_store: SecretStore, ref: SecretRef
) -> None:
    other = SecretRef(connection="atlassian_work", field="client_secret")
    await sealed_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    await sealed_store.put(other, "client-secret-value", caller_did=CALLER)

    first = await sealed_store.get(ref, caller_did=CALLER)
    assert first is not None
    assert first.reveal() == SECRET_VALUE


async def test_concurrent_writes_do_not_lose_each_other(
    sealed_store: SecretStore, ref: SecretRef
) -> None:
    """Two writers forced to the same instant both land (read-modify-write hazard).

    [[feedback_concurrency_tests_must_interleave]] — the barrier is what makes the
    two coroutines reach their write together; without it ``gather`` runs them one
    after the other and a lost update never fires.
    """
    other = SecretRef(connection="atlassian_work", field="client_secret")
    barrier = asyncio.Barrier(2)

    async def write(target: SecretRef, value: str) -> None:
        await barrier.wait()
        await sealed_store.put(target, value, caller_did=CALLER)

    await asyncio.gather(write(ref, SECRET_VALUE), write(other, "client-secret-value"))

    first = await sealed_store.get(ref, caller_did=CALLER)
    second = await sealed_store.get(other, caller_did=CALLER)
    assert first is not None and first.reveal() == SECRET_VALUE
    assert second is not None and second.reveal() == "client-secret-value"


async def test_a_ciphertext_moved_to_another_field_does_not_open(
    sealed_store: SecretStore, backend: FakeBackend
) -> None:
    """The coordinate is bound into the seal: a swapped value is unreadable, not leaked."""
    first = SecretRef(connection="atlassian_work", field="refresh_token")
    second = SecretRef(connection="atlassian_work", field="client_secret")
    await sealed_store.put(first, SECRET_VALUE, caller_did=CALLER)
    await sealed_store.put(second, "client-secret-value", caller_did=CALLER)
    raw = await backend.mutable_read(CREDENTIAL_COLLECTION, "atlassian_work")
    assert raw is not None
    swapped = {**raw["fields"], "client_secret": raw["fields"]["refresh_token"]}
    await backend.mutable_merge(
        CREDENTIAL_COLLECTION, "atlassian_work", {"fields": swapped}, actor_did=CALLER
    )

    with pytest.raises(ExtensionError):
        await sealed_store.get(second, caller_did=CALLER)


def test_the_sealed_backend_satisfies_the_one_interface(rows: CredentialRowStore) -> None:
    """Structural conformance, so a backend cannot be wired in half-implemented."""
    backend: SecretBackend = SealedCredentialBackend(rows)
    assert backend is not None
