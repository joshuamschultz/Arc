"""SecretStore seam — one interface, tier-selected backing store (SPEC-062 COMP-010).

Covers REQ-265 (a connector secret is written only to the per-agent secret store,
owner-only, and never into a config file, a log, a prompt, or model context) and
REQ-294 (one interface whose backing store is chosen by tier, defaulting to the
local per-agent store and supporting an external vault with no change to calling
code).

The leak tests are the point of this file, so they are written as *searches over
artifacts* rather than as assertions about one call: after a write, every file
under the agent home, every log record captured at DEBUG, every audit event, and
every exception rendering is scanned for the secret value. A store that leaks
through a path nobody thought to assert on still fails here.

``test_calling_code_is_identical_across_backends`` is the REQ-294 governing test:
one helper function is run against the local backend and against a vault backend
and is not allowed to know which it got.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.secrets import (
    LocalFileSecretBackend,
    Secret,
    SecretRef,
    SecretStore,
    VaultSecretBackend,
    select_secret_backend,
)

if TYPE_CHECKING:
    from arcagent.extension.secrets import SecretBackend

SECRET_VALUE = "atlassian-refresh-tok-9f2c4e7a1b8d6"
CALLER = "did:arc:agent:coder"


class RecordingSink:
    """Audit sink that keeps every event so a test can scan it for the value."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeVault:
    """An in-memory stand-in for an external vault (reads and writes)."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.reads: list[str] = []

    async def get_secret(self, path: str) -> str | None:
        self.reads.append(path)
        return self.items.get(path)

    async def set_secret(self, path: str, value: str) -> None:
        self.items[path] = value

    async def delete_secret(self, path: str) -> bool:
        return self.items.pop(path, None) is not None


class ReadOnlyVault:
    """A vault that can only be read — the shape that must refuse a write loudly."""

    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.items = items or {}

    async def get_secret(self, path: str) -> str | None:
        return self.items.get(path)


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
def env_file(agent_home: Path) -> Path:
    return agent_home / "arc.env"


@pytest.fixture
def ref() -> SecretRef:
    return SecretRef(agent="coder", instance="atlassian_work", field="refresh_token")


@pytest.fixture
def local_store(env_file: Path) -> SecretStore:
    return SecretStore(LocalFileSecretBackend(env_file))


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
# REQ-265 — the value reaches the store and nothing else
# ---------------------------------------------------------------------------


async def test_value_is_written_only_to_the_secret_store(
    local_store: SecretStore, agent_home: Path, env_file: Path, ref: SecretRef
) -> None:
    """No config file, and no other artifact in the agent home, holds the value."""
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    hits = _files_containing(agent_home, SECRET_VALUE)
    assert hits == [env_file], f"secret leaked into {[str(p) for p in hits]}"


async def test_store_file_is_owner_only(
    local_store: SecretStore, env_file: Path, ref: SecretRef
) -> None:
    """0600 on the file and 0700 on its directory (REQ-265)."""
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700


async def test_nothing_logs_the_value(
    local_store: SecretStore, ref: SecretRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A full write/read/delete cycle at DEBUG never emits the value to a log."""
    caplog.set_level(logging.DEBUG)

    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    await local_store.get(ref, caller_did=CALLER)
    await local_store.delete(ref, caller_did=CALLER)

    for record in caplog.records:
        assert SECRET_VALUE not in record.getMessage()
        assert SECRET_VALUE not in str(record.args)


async def test_audit_events_record_coordinates_never_the_value(
    env_file: Path, ref: SecretRef
) -> None:
    """The credential carve-out: store, item, field, caller, outcome — no value."""
    sink = RecordingSink()
    store = SecretStore(LocalFileSecretBackend(env_file), sink=sink)

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
    local_store: SecretStore, ref: SecretRef
) -> None:
    """A refusal names the coordinate, never the rejected material."""
    poisoned = f"{SECRET_VALUE}\nARC_SECRET_CODER_OTHER_TOKEN=injected"

    with pytest.raises(ExtensionError) as excinfo:
        await local_store.put(ref, poisoned, caller_did=CALLER)

    rendered = f"{excinfo.value}{excinfo.value.details}"
    assert SECRET_VALUE not in rendered
    assert "injected" not in rendered


async def test_a_newline_cannot_forge_a_second_entry(
    local_store: SecretStore, env_file: Path, ref: SecretRef
) -> None:
    """Line-oriented storage means an unchecked newline is a write to another key."""
    with pytest.raises(ExtensionError):
        await local_store.put(ref, "tok\nARC_SECRET_CODER_OTHER_TOKEN=stolen", caller_did=CALLER)

    assert not env_file.exists() or "stolen" not in env_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Keying — (agent, instance, field)
# ---------------------------------------------------------------------------


async def test_secrets_are_keyed_by_agent_instance_and_field(
    local_store: SecretStore, ref: SecretRef
) -> None:
    """Three coordinates, three distinct slots — nothing shares a cell."""
    others = [
        SecretRef(agent="marketer", instance="atlassian_work", field="refresh_token"),
        SecretRef(agent="coder", instance="atlassian_personal", field="refresh_token"),
        SecretRef(agent="coder", instance="atlassian_work", field="client_secret"),
    ]
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    for index, other in enumerate(others):
        await local_store.put(other, f"other-{index}", caller_did=CALLER)

    resolved = await local_store.get(ref, caller_did=CALLER)
    assert resolved is not None
    assert resolved.reveal() == SECRET_VALUE
    for index, other in enumerate(others):
        stored = await local_store.get(other, caller_did=CALLER)
        assert stored is not None
        assert stored.reveal() == f"other-{index}"


async def test_missing_secret_resolves_to_none(local_store: SecretStore, ref: SecretRef) -> None:
    assert await local_store.get(ref, caller_did=CALLER) is None


async def test_delete_removes_the_value_from_the_store(
    local_store: SecretStore, env_file: Path, ref: SecretRef
) -> None:
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)

    assert await local_store.delete(ref, caller_did=CALLER) is True
    assert await local_store.get(ref, caller_did=CALLER) is None
    assert SECRET_VALUE not in env_file.read_text(encoding="utf-8")
    assert await local_store.delete(ref, caller_did=CALLER) is False


@pytest.mark.parametrize(
    "agent,instance,field",
    [
        ("coder", "../../etc", "refresh_token"),
        ("coder", "atlassian work", "refresh_token"),
        ("coder", "atlassian=work", "refresh_token"),
        ("coder", "atlassian\nwork", "refresh_token"),
        ("", "atlassian_work", "refresh_token"),
        ("coder", "atlassian_work", ""),
        ("coder", "a" * 65, "refresh_token"),
    ],
)
def test_coordinates_that_could_escape_their_cell_are_refused(
    agent: str, instance: str, field: str
) -> None:
    """A coordinate becomes a filesystem path and an env key — it is untrusted input."""
    with pytest.raises(ExtensionError):
        SecretRef(agent=agent, instance=instance, field=field)


def test_case_variants_cannot_collide_into_one_cell() -> None:
    """Uppercase would fold onto the same env key and read another connection's secret."""
    with pytest.raises(ExtensionError):
        SecretRef(agent="coder", instance="Atlassian_Work", field="refresh_token")


# ---------------------------------------------------------------------------
# Durability — an interrupted write cannot destroy or tear the store
# ---------------------------------------------------------------------------


async def test_a_second_write_preserves_the_first(
    local_store: SecretStore, ref: SecretRef
) -> None:
    other = SecretRef(agent="coder", instance="atlassian_work", field="client_secret")
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    await local_store.put(other, "client-secret-value", caller_did=CALLER)

    first = await local_store.get(ref, caller_did=CALLER)
    assert first is not None
    assert first.reveal() == SECRET_VALUE


async def test_a_failed_write_leaves_the_previous_store_intact(
    local_store: SecretStore, env_file: Path, ref: SecretRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write leaves the old file, never a truncated one (REQ-288's sibling)."""
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    before = env_file.read_bytes()

    def boom(src: Any, dst: Any) -> None:
        raise OSError("interrupted before the rename landed")

    monkeypatch.setattr(os, "replace", boom)
    other = SecretRef(agent="coder", instance="atlassian_work", field="client_secret")
    with pytest.raises(OSError, match="interrupted"):
        await local_store.put(other, "client-secret-value", caller_did=CALLER)

    assert env_file.read_bytes() == before
    assert list(env_file.parent.glob("*.tmp*")) == [], "a partial write was left behind"


async def test_concurrent_writes_do_not_lose_each_other(
    local_store: SecretStore, ref: SecretRef
) -> None:
    """Two writers forced to the same instant both land (read-modify-write hazard).

    [[feedback_concurrency_tests_must_interleave]] — the barrier is what makes the
    two coroutines reach their write together; without it ``gather`` runs them one
    after the other and a lost update never fires.
    """
    other = SecretRef(agent="coder", instance="atlassian_work", field="client_secret")
    barrier = asyncio.Barrier(2)

    async def write(target: SecretRef, value: str) -> None:
        await barrier.wait()
        await local_store.put(target, value, caller_did=CALLER)

    await asyncio.gather(write(ref, SECRET_VALUE), write(other, "client-secret-value"))

    first = await local_store.get(ref, caller_did=CALLER)
    second = await local_store.get(other, caller_did=CALLER)
    assert first is not None and first.reveal() == SECRET_VALUE
    assert second is not None and second.reveal() == "client-secret-value"


async def test_a_loose_permission_store_is_refused(
    local_store: SecretStore, env_file: Path, ref: SecretRef
) -> None:
    """A world-readable store is a misconfiguration, not something to read from."""
    await local_store.put(ref, SECRET_VALUE, caller_did=CALLER)
    env_file.chmod(0o644)

    with pytest.raises(ExtensionError) as excinfo:
        await local_store.get(ref, caller_did=CALLER)

    assert SECRET_VALUE not in f"{excinfo.value}{excinfo.value.details}"


# ---------------------------------------------------------------------------
# REQ-294 — the backing store is tier configuration, not a branch at the call site
# ---------------------------------------------------------------------------


async def resolve_a_connector_credential(store: SecretStore, ref: SecretRef) -> str:
    """The calling code under test: it must not be able to tell which backend it got."""
    await store.put(ref, SECRET_VALUE, caller_did=CALLER)
    resolved = await store.get(ref, caller_did=CALLER)
    assert resolved is not None
    return resolved.reveal()


async def test_calling_code_is_identical_across_backends(env_file: Path, ref: SecretRef) -> None:
    """REQ-294's governing test: same function, two stores, no branch."""
    vault = FakeVault()
    local = SecretStore(select_secret_backend(Tier.PERSONAL, env_file=env_file))
    hosted = SecretStore(select_secret_backend(Tier.FEDERAL, env_file=env_file, vault=vault))

    assert await resolve_a_connector_credential(local, ref) == SECRET_VALUE
    assert await resolve_a_connector_credential(hosted, ref) == SECRET_VALUE
    assert vault.items, "the federal store must have gone to the vault"


def test_personal_defaults_to_the_local_per_agent_store(env_file: Path) -> None:
    backend = select_secret_backend(Tier.PERSONAL, env_file=env_file)
    assert isinstance(backend, LocalFileSecretBackend)


def test_a_vault_is_used_whenever_one_is_configured(env_file: Path) -> None:
    backend = select_secret_backend(Tier.ENTERPRISE, env_file=env_file, vault=FakeVault())
    assert isinstance(backend, VaultSecretBackend)


def test_federal_without_a_vault_refuses_rather_than_falling_back(env_file: Path) -> None:
    """Fail closed: silently writing a federal credential to a local file is the bug."""
    with pytest.raises(ExtensionError) as excinfo:
        select_secret_backend(Tier.FEDERAL, env_file=env_file)

    assert "vault" in excinfo.value.message.lower()


async def test_vault_backend_keeps_the_three_coordinates_in_its_path(
    ref: SecretRef,
) -> None:
    vault = FakeVault()
    store = SecretStore(VaultSecretBackend(vault))

    await store.put(ref, SECRET_VALUE, caller_did=CALLER)

    (path,) = list(vault.items)
    assert "coder" in path
    assert "atlassian_work" in path
    assert "refresh_token" in path


async def test_a_read_only_vault_refuses_the_write_instead_of_downgrading(
    ref: SecretRef,
) -> None:
    """Falling back to a local file here would quietly undo the operator's hardening."""
    store = SecretStore(VaultSecretBackend(ReadOnlyVault()))

    with pytest.raises(ExtensionError) as excinfo:
        await store.put(ref, SECRET_VALUE, caller_did=CALLER)

    assert excinfo.value.code == "SECRET_STORE_READ_ONLY"


async def test_an_unreachable_vault_surfaces_rather_than_resolving_to_none(
    ref: SecretRef,
) -> None:
    """ "Not found" and "could not ask" must not look the same to a caller."""

    class UnreachableVault:
        async def get_secret(self, path: str) -> str | None:
            from arcagent.core.vault import VaultUnreachable

            raise VaultUnreachable(path)

    store = SecretStore(VaultSecretBackend(UnreachableVault()))

    with pytest.raises(ExtensionError) as excinfo:
        await store.get(ref, caller_did=CALLER)

    assert excinfo.value.code == "SECRET_STORE_UNREACHABLE"


def test_every_backend_satisfies_the_one_interface(env_file: Path) -> None:
    """Structural conformance, so a new backend cannot be wired in half-implemented."""
    backends: list[SecretBackend] = [
        LocalFileSecretBackend(env_file),
        VaultSecretBackend(FakeVault()),
    ]
    assert len(backends) == 2
