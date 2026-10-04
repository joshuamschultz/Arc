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
from collections.abc import Iterator
from pathlib import Path

import pytest
from arctrust import causal
from arctrust.audit import AuditEvent
from arctrust.paths import arc_config

from arcagent.core.errors import ExtensionError
from arcagent.keys import ENV_FILENAME, KeyStatus, KeyStore, default_env_file

CALLER = "did:arc:local:operator"


@pytest.fixture(autouse=True)
def _bound_caller() -> Iterator[None]:
    """The caller is the bound causal initiator; the store reads it from there (item 20)."""
    with causal.bind(causal.root("operator", CALLER)):
        yield


KEY_VALUE = "sk-ant-api03-do-not-leak-me-4f2c9e"


class RecordingSink:
    """Audit sink that keeps every event so a test can scan it for the value."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return default_env_file(tmp_path / "arc")


@pytest.fixture
def store(env_file: Path) -> KeyStore:
    return KeyStore(env_file)


def _status(statuses: tuple[KeyStatus, ...], provider: str) -> KeyStatus:
    return next(status for status in statuses if status.provider == provider)


# ---------------------------------------------------------------------------
# list — the whole map, presence only
# ---------------------------------------------------------------------------


async def test_list_covers_every_provider_and_classifier_arcllm_declares(
    store: KeyStore,
) -> None:
    from arcllm import list_classifier_keys, list_provider_keys

    statuses = await store.list()
    declared = [*list_provider_keys(), *list_classifier_keys()]
    assert {s.provider for s in statuses if s.kind == "model"} == {k.provider for k in declared}


async def test_list_reports_presence_for_a_set_and_an_unset_key(store: KeyStore) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)

    statuses = await store.list()
    assert _status(statuses, "anthropic").present is True
    assert _status(statuses, "openai").present is False


async def test_list_carries_the_declared_requirement(store: KeyStore) -> None:
    statuses = await store.list()
    assert _status(statuses, "anthropic").required is True
    assert _status(statuses, "ollama").required is False


async def test_a_status_never_carries_the_value_in_any_field(store: KeyStore) -> None:
    """D-583 — presence is the whole answer. No value, no prefix, no length, no hash."""
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)

    status = _status(await store.list(), "anthropic")
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
        await store.set("PATH", KEY_VALUE)

    assert excinfo.value.code == "PROVIDER_KEY_UNKNOWN"
    assert not env_file.exists()


async def test_set_refuses_a_value_containing_a_newline(store: KeyStore, env_file: Path) -> None:
    """The store is line-oriented: an unchecked newline forges an entry nobody asked for."""
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)
    before = env_file.read_bytes()

    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", "sk-openai\nPATH=/tmp/evil")

    assert excinfo.value.code == "PROVIDER_KEY_VALUE_INVALID"
    assert env_file.read_bytes() == before


async def test_set_refuses_a_value_containing_a_nul(store: KeyStore) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", "sk-openai\x00truncated")
    assert excinfo.value.code == "PROVIDER_KEY_VALUE_INVALID"


async def test_set_refuses_an_empty_value(store: KeyStore) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", "")
    assert excinfo.value.code == "PROVIDER_KEY_EMPTY"


async def test_a_refusal_names_the_coordinate_and_never_the_rejected_material(
    store: KeyStore,
) -> None:
    """An error message travels to a log, a terminal, and an HTTP body. It carries no key."""
    rejected = f"{KEY_VALUE}\nPATH=/tmp/evil"
    with pytest.raises(ExtensionError) as excinfo:
        await store.set("OPENAI_API_KEY", rejected)

    rendered = f"{excinfo.value.message} {excinfo.value.details} {excinfo.value!s}"
    assert KEY_VALUE not in rendered
    assert "OPENAI_API_KEY" in rendered


async def test_set_replaces_the_previous_value_and_keeps_its_neighbours(
    store: KeyStore, env_file: Path
) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)
    await store.set("OPENAI_API_KEY", "sk-openai-value")
    await store.set("ANTHROPIC_API_KEY", "sk-ant-rotated")

    body = env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-rotated" in body
    assert "OPENAI_API_KEY=sk-openai-value" in body
    assert KEY_VALUE not in body


async def test_the_env_file_is_owner_only_after_a_write(store: KeyStore, env_file: Path) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


async def test_a_loosened_env_file_is_refused(store: KeyStore, env_file: Path) -> None:
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)
    env_file.chmod(0o644)

    with pytest.raises(ExtensionError) as excinfo:
        await store.list()
    assert excinfo.value.code == "SECRET_STORE_LOOSE_PERMS"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_reports_whether_a_key_was_there(store: KeyStore) -> None:
    await store.set("GROQ_API_KEY", "gsk-value")
    assert await store.delete("GROQ_API_KEY") is True
    assert await store.delete("GROQ_API_KEY") is False


async def test_delete_refuses_an_env_var_no_provider_declares(store: KeyStore) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await store.delete("PATH")
    assert excinfo.value.code == "PROVIDER_KEY_UNKNOWN"


# ---------------------------------------------------------------------------
# audit — the coordinate lands in the chain, the value never does
# ---------------------------------------------------------------------------


async def test_a_write_is_audited_by_coordinate_and_caller(env_file: Path) -> None:
    sink = RecordingSink()
    await KeyStore(env_file, sink=sink).set("ANTHROPIC_API_KEY", KEY_VALUE)

    assert [event.action for event in sink.events] == ["provider_key.write"]
    event = sink.events[0]
    assert event.actor_did == CALLER
    assert "ANTHROPIC_API_KEY" in event.target
    assert event.outcome == "allow"


async def test_a_delete_is_audited_with_its_outcome(env_file: Path) -> None:
    sink = RecordingSink()
    store = KeyStore(env_file, sink=sink)
    await store.set("GROQ_API_KEY", "gsk-value")
    await store.delete("GROQ_API_KEY")
    await store.delete("GROQ_API_KEY")

    outcomes = [(event.action, event.outcome) for event in sink.events]
    assert outcomes == [
        ("provider_key.write", "allow"),
        ("provider_key.delete", "allow"),
        ("provider_key.delete", "not_found"),
    ]


async def test_no_audit_event_ever_carries_the_value(env_file: Path) -> None:
    sink = RecordingSink()
    store = KeyStore(env_file, sink=sink)
    await store.set("ANTHROPIC_API_KEY", KEY_VALUE)
    await store.delete("ANTHROPIC_API_KEY")

    for event in sink.events:
        assert KEY_VALUE not in event.model_dump_json()


# ---------------------------------------------------------------------------
# The default location — one resolver, so every surface writes the same file
# ---------------------------------------------------------------------------


def test_the_default_env_file_is_the_one_the_deployment_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``arc.env`` — the name the systemd unit sources and the gateway writes.

    The *name* is pinned as a literal on purpose: this is the one place it is
    allowed to be spelled out, because agreeing with the deployment is the whole
    property. The directory comes from the resolver, so relocating the config
    root moves this file with it instead of stranding it.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", "/tmp/arc-home-under-test")
    assert default_env_file() == arc_config() / "arc.env"


def test_an_overridden_arc_dir_still_goes_through_the_one_resolver(tmp_path: Path) -> None:
    assert default_env_file(tmp_path) == arc_config(tmp_path) / ENV_FILENAME


# ---------------------------------------------------------------------------
# web search / extract keys — the same store, a second declared family
# ---------------------------------------------------------------------------


async def test_list_includes_the_web_provider_keys_the_web_module_reads(store: KeyStore) -> None:
    from arcagent.keys import WEB_PROVIDER_KEY_ENV

    statuses = await store.list()
    web = {status.env_var: status for status in statuses if status.kind == "web"}

    assert set(web) == set(WEB_PROVIDER_KEY_ENV.values())
    assert set(WEB_PROVIDER_KEY_ENV) == {"parallel", "firecrawl", "tavily"}
    assert all(status.required is False for status in web.values())
    assert {status.kind for status in statuses if status.kind != "web"} == {"model"}


async def test_a_web_key_is_stored_with_the_same_custody_and_reported_as_present(
    store: KeyStore, env_file: Path
) -> None:
    await store.set("TAVILY_API_KEY", KEY_VALUE)

    web = {s.provider: s for s in await store.list() if s.kind == "web"}
    assert web["tavily"].present is True
    assert web["firecrawl"].present is False
    assert KEY_VALUE not in repr(web["tavily"])
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


async def test_a_web_key_can_be_forgotten(store: KeyStore) -> None:
    await store.set("FIRECRAWL_API_KEY", KEY_VALUE)

    assert await store.delete("FIRECRAWL_API_KEY") is True
    assert await store.delete("FIRECRAWL_API_KEY") is False


async def test_the_allowlist_still_refuses_a_name_no_provider_declares(
    store: KeyStore, env_file: Path
) -> None:
    for name in ("TAVILY_API_KEY2", "tavily_api_key", "BRAVE_API_KEY", "PATH"):
        with pytest.raises(ExtensionError) as excinfo:
            await store.set(name, KEY_VALUE)
        assert excinfo.value.code == "PROVIDER_KEY_UNKNOWN"
    assert not env_file.exists()


def test_the_web_module_reads_the_variables_this_store_writes() -> None:
    """One map: a key set here is the key the web module resolves, by construction."""
    from arcagent.keys import WEB_PROVIDER_KEY_ENV
    from arcagent.modules.web import _runtime

    assert _runtime._ENV_VAR_BY_PROVIDER is WEB_PROVIDER_KEY_ENV
