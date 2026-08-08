"""SPEC-064 T-003 — the provider-key store, which can be written but never read back.

``arc init`` used to end by telling an operator to append a key to a dotfile by
hand. :class:`~arcagent.keys.KeyStore` is the verb behind that, and it is shaped by
one decision (D-583): a key value is **write-only across every surface**. There is
no ``get``, and :class:`~arcagent.keys.KeyStatus` carries no value, no prefix, no
length, and no hash — a surface that cannot display a key cannot leak one, so a web
panel and a terminal table are equally safe by construction rather than by care.

The other property under test is the allowlist. ``set`` writes an environment
variable into a file the deployment sources, so an unchecked name is an arbitrary
env-var write; only a variable some packaged provider declares is accepted.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.keys import KeyStatus, KeyStore, default_env_file

CALLER = "did:arc:local:operator"
KEY_VALUE = "sk-ant-api03-do-not-leak-me-4f2c9e"


class RecordingSink:
    """Audit sink that keeps every event so a test can scan it for the value."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return tmp_path / "arc" / ".env"


@pytest.fixture
def store(env_file: Path) -> KeyStore:
    return KeyStore(env_file)


def _status(statuses: tuple[KeyStatus, ...], provider: str) -> KeyStatus:
    return next(status for status in statuses if status.provider == provider)


# ---------------------------------------------------------------------------
# list — the whole map, presence only
# ---------------------------------------------------------------------------


async def test_list_covers_every_provider_arcllm_declares(store: KeyStore) -> None:
    from arcllm import list_provider_keys

    statuses = await store.list(caller_did=CALLER)
    assert {status.provider for status in statuses} == {
        key.provider for key in list_provider_keys()
    }


async def test_list_reports_presence_for_a_set_and_an_unset_key(store: KeyStore) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)

    statuses = await store.list(caller_did=CALLER)
    assert _status(statuses, "anthropic").present is True
    assert _status(statuses, "openai").present is False


async def test_list_carries_the_declared_requirement(store: KeyStore) -> None:
    statuses = await store.list(caller_did=CALLER)
    assert _status(statuses, "anthropic").required is True
    assert _status(statuses, "ollama").required is False


async def test_a_status_never_carries_the_value_in_any_field(store: KeyStore) -> None:
    """D-583 — presence is the whole answer. No value, no prefix, no length, no hash."""
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)

    status = _status(await store.list(caller_did=CALLER), "anthropic")
    rendered = repr(status)
    for fragment in (KEY_VALUE, KEY_VALUE[:8], str(len(KEY_VALUE))):
        assert fragment not in rendered
    assert not hasattr(store, "get"), "there is no read verb; the value is read from os.environ"


# ---------------------------------------------------------------------------
# set — the allowlist, and material that could forge a second entry
# ---------------------------------------------------------------------------


async def test_set_refuses_an_env_var_no_provider_declares(
    store: KeyStore, env_file: Path
) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("PATH", KEY_VALUE, caller_did=CALLER)

    assert excinfo.value.code == "PROVIDER_KEY_UNKNOWN"
    assert not env_file.exists()


async def test_set_refuses_a_value_containing_a_newline(store: KeyStore, env_file: Path) -> None:
    """The store is line-oriented: an unchecked newline forges an entry nobody asked for."""
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)
    before = env_file.read_bytes()

    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", "sk-openai\nPATH=/tmp/evil", caller_did=CALLER)

    assert excinfo.value.code == "PROVIDER_KEY_VALUE_INVALID"
    assert env_file.read_bytes() == before


async def test_set_refuses_a_value_containing_a_nul(store: KeyStore) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", "sk-openai\x00truncated", caller_did=CALLER)
    assert excinfo.value.code == "PROVIDER_KEY_VALUE_INVALID"


async def test_set_refuses_an_empty_value(store: KeyStore) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", "", caller_did=CALLER)
    assert excinfo.value.code == "PROVIDER_KEY_EMPTY"


async def test_a_refusal_names_the_coordinate_and_never_the_rejected_material(
    store: KeyStore,
) -> None:
    """An error message travels to a log, a terminal, and an HTTP body. It carries no key."""
    rejected = f"{KEY_VALUE}\nPATH=/tmp/evil"
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", rejected, caller_did=CALLER)

    rendered = f"{excinfo.value.message} {excinfo.value.details} {excinfo.value!s}"
    assert KEY_VALUE not in rendered
    assert "OPENAI_API_KEY" in rendered


async def test_set_replaces_the_previous_value_and_keeps_its_neighbours(
    store: KeyStore, env_file: Path
) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)
    await store.set("OPENAI_API_KEY", "sk-openai-value", caller_did=CALLER)
    await store.set("ANTHROPIC_API_KEY", "sk-ant-rotated", caller_did=CALLER)

    body = env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-rotated" in body
    assert "OPENAI_API_KEY=sk-openai-value" in body
    assert KEY_VALUE not in body


async def test_the_env_file_is_owner_only_after_a_write(store: KeyStore, env_file: Path) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


async def test_a_loosened_env_file_is_refused(store: KeyStore, env_file: Path) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)
    env_file.chmod(0o644)

    with pytest.raises(ExtensionError) as excinfo:
        await store.list(caller_did=CALLER)
    assert excinfo.value.code == "SECRET_STORE_LOOSE_PERMS"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_reports_whether_a_key_was_there(store: KeyStore) -> None:
    await store.set("GROQ_API_KEY", "gsk-value", caller_did=CALLER)
    assert await store.delete("GROQ_API_KEY", caller_did=CALLER) is True
    assert await store.delete("GROQ_API_KEY", caller_did=CALLER) is False


async def test_delete_refuses_an_env_var_no_provider_declares(store: KeyStore) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.delete("PATH", caller_did=CALLER)
    assert excinfo.value.code == "PROVIDER_KEY_UNKNOWN"


# ---------------------------------------------------------------------------
# audit — the coordinate lands in the chain, the value never does
# ---------------------------------------------------------------------------


async def test_a_write_is_audited_by_coordinate_and_caller(env_file: Path) -> None:
    sink = RecordingSink()
    await KeyStore(env_file, sink=sink).set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)

    assert [event.action for event in sink.events] == ["provider_key.write"]
    event = sink.events[0]
    assert event.actor_did == CALLER
    assert "ANTHROPIC_API_KEY" in event.target
    assert event.outcome == "allow"


async def test_a_delete_is_audited_with_its_outcome(env_file: Path) -> None:
    sink = RecordingSink()
    store = KeyStore(env_file, sink=sink)
    await store.set("GROQ_API_KEY", "gsk-value", caller_did=CALLER)
    await store.delete("GROQ_API_KEY", caller_did=CALLER)
    await store.delete("GROQ_API_KEY", caller_did=CALLER)

    outcomes = [(event.action, event.outcome) for event in sink.events]
    assert outcomes == [
        ("provider_key.write", "allow"),
        ("provider_key.delete", "allow"),
        ("provider_key.delete", "not_found"),
    ]


async def test_no_audit_event_ever_carries_the_value(env_file: Path) -> None:
    sink = RecordingSink()
    store = KeyStore(env_file, sink=sink)
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE, caller_did=CALLER)
    await store.delete("ANTHROPIC_API_KEY", caller_did=CALLER)

    for event in sink.events:
        assert KEY_VALUE not in event.model_dump_json()


# ---------------------------------------------------------------------------
# The default location — one resolver, so every surface writes the same file
# ---------------------------------------------------------------------------


def test_the_default_env_file_is_the_one_arc_init_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", "/tmp/arc-home-under-test")
    assert default_env_file() == Path("/tmp/arc-home-under-test/.env")


def test_an_overridden_arc_dir_still_goes_through_the_one_resolver(tmp_path: Path) -> None:
    assert default_env_file(tmp_path) == tmp_path / ".env"
