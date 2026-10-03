"""SPEC-083 T-1216 (COMP-026, REQ-510) — the Jev key goes through the one key store.

Decision #12: one fleet-wide ``TYPESAFE_API_KEY`` in the existing write-only
store (``arc keys`` / ``/api/keys``). The store's allowlist is
``arcrun.model_provider_keys()``; the key is accepted *only* because arcllm's
Jev drop-in declares it and arcrun surfaces that declaration. arcagent never
imports arcllm and never names the vendor itself.

The allowlist must not widen past what is declared: an undeclared name is still
an arbitrary env-var write and must still be refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import arcrun
import pytest
from arctrust import causal
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.keys import KeyStore, default_env_file
from arcagent.modules.memory.config import MemoryConfig, MemoryPromotionConfig

JEV_KEY = "TYPESAFE_API_KEY"
CALLER = "did:arc:local:operator"


@pytest.fixture(autouse=True)
def _bound_caller() -> Iterator[None]:
    """The caller is the bound causal initiator; the store reads it from there (item 20)."""
    with causal.bind(causal.root("operator", CALLER)):
        yield


KEY_VALUE = "ts-live-do-not-leak-me-7a1b3c"


class RecordingSink:
    """Audit sink that keeps every event so a test can scan it for the value."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return default_env_file(tmp_path / "arc")


# ---------------------------------------------------------------------------
# Declaration — surfaced by the arcrun facade, not invented by arcagent
# ---------------------------------------------------------------------------


def test_model_provider_keys_includes_the_jev_key() -> None:
    envs = [key.api_key_env for key in arcrun.model_provider_keys()]
    assert JEV_KEY in envs


def test_the_jev_key_is_declared_exactly_once() -> None:
    envs = [key.api_key_env for key in arcrun.model_provider_keys()]
    assert envs.count(JEV_KEY) == 1


def test_the_jev_key_entry_names_a_provider_and_is_not_required() -> None:
    # Promotion is off by default (REQ-447), so a missing Jev key must never be
    # reported as a required-but-absent provider key.
    entry = next(key for key in arcrun.model_provider_keys() if key.api_key_env == JEV_KEY)
    assert entry.provider
    assert entry.required is False


def test_model_provider_keys_still_carries_every_llm_provider() -> None:
    # Adding classifier keys must not displace the LLM provider catalog.
    envs = {key.api_key_env for key in arcrun.model_provider_keys()}
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} <= envs


# ---------------------------------------------------------------------------
# KeyStore — accepts the declared key, audits it without the value
# ---------------------------------------------------------------------------


async def test_keystore_set_accepts_the_jev_key(env_file: Path) -> None:
    await KeyStore(env_file).set(JEV_KEY, KEY_VALUE)

    assert f"{JEV_KEY}=" in env_file.read_text()


async def test_keystore_list_reports_jev_key_presence_without_a_value(env_file: Path) -> None:
    store = KeyStore(env_file)
    before = {s.env_var: s.present for s in await store.list()}
    await store.set(JEV_KEY, KEY_VALUE)
    after = await store.list()

    assert before[JEV_KEY] is False
    status = next(s for s in after if s.env_var == JEV_KEY)
    assert status.present is True
    assert KEY_VALUE not in repr(after)


async def test_keystore_set_of_the_jev_key_is_audited_without_the_value(env_file: Path) -> None:
    sink = RecordingSink()
    await KeyStore(env_file, sink=sink).set(JEV_KEY, KEY_VALUE)

    assert [event.action for event in sink.events] == ["provider_key.write"]
    event = sink.events[0]
    assert event.actor_did == CALLER
    assert JEV_KEY in event.target
    assert event.outcome == "allow"
    assert KEY_VALUE not in event.model_dump_json()


async def test_keystore_delete_of_the_jev_key_is_accepted_and_audited(env_file: Path) -> None:
    sink = RecordingSink()
    store = KeyStore(env_file, sink=sink)
    await store.set(JEV_KEY, KEY_VALUE)

    assert await store.delete(JEV_KEY) is True
    assert JEV_KEY not in env_file.read_text()
    assert [e.action for e in sink.events] == ["provider_key.write", "provider_key.delete"]
    for event in sink.events:
        assert KEY_VALUE not in event.model_dump_json()


# ---------------------------------------------------------------------------
# Narrowness — the allowlist grew by one declared name, not by a pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_var",
    ["PATH", "LD_PRELOAD", "TYPESAFE_BASE_URL", "TYPESAFE_API_KEY_2", "typesafe_api_key"],
)
async def test_keystore_still_refuses_an_undeclared_env_var(env_file: Path, env_var: str) -> None:
    with pytest.raises(ExtensionError) as excinfo:
        await KeyStore(env_file).set(env_var, KEY_VALUE)

    assert excinfo.value.code == "PROVIDER_KEY_UNKNOWN"
    assert not env_file.exists() or env_var not in env_file.read_text()


# ---------------------------------------------------------------------------
# Config — the key is a coordinate in config, never a value
# ---------------------------------------------------------------------------


def test_memory_promotion_config_defaults_the_key_coordinate() -> None:
    config = MemoryPromotionConfig()
    assert config.api_key_env == JEV_KEY
    assert config.vault_path is None


def test_memory_promotion_config_accepts_a_vault_path() -> None:
    config = MemoryPromotionConfig(vault_path="secret/arc/typesafe")
    assert config.vault_path == "secret/arc/typesafe"


def test_memory_config_toml_block_parses_the_key_coordinate() -> None:
    config = MemoryConfig.model_validate(
        {"promotion": {"api_key_env": JEV_KEY, "vault_path": "secret/arc/typesafe"}}
    )
    assert config.promotion.api_key_env == JEV_KEY
    assert config.promotion.vault_path == "secret/arc/typesafe"


def test_memory_promotion_config_has_no_field_that_holds_a_key_value() -> None:
    # A key value must never be representable in agent TOML (REQ-510).
    fields = set(MemoryPromotionConfig.model_fields)
    assert not {"api_key", "key", "token", "secret"} & fields


def test_arcmemory_promotion_config_mirrors_the_key_coordinate() -> None:
    # The module-local mirror and arcmemory's PromotionConfig must stay in step
    # (same fields, same defaults) — the sweep passes these to the classifier.
    promotion_config = pytest.importorskip("arcmemory.promotion.config")
    config = promotion_config.PromotionConfig()
    assert config.api_key_env == JEV_KEY
    assert config.vault_path is None
