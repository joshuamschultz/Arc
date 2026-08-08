"""SPEC-064 T-001 — arcllm declares which env var a provider reads, and nothing else may.

``providers/*.toml`` has always carried ``api_key_env``. What it lacked was a way to
ask, so every surface that needed the map kept its own copy — and the copy in
``arccli.commands.init`` drifted to eight of the sixteen providers arcllm ships.
These tests pin the property that made the drift possible: the answer comes from
the packaged TOML through the existing loader, so a new provider file is visible to
every surface the moment it lands, with no second parser to update.
"""

from __future__ import annotations

from pathlib import Path

import arcllm
from arcllm.config import list_provider_keys

_PROVIDERS_DIR = Path(arcllm.__file__).parent / "providers"


def _packaged_provider_names() -> set[str]:
    return {path.stem for path in _PROVIDERS_DIR.glob("*.toml")}


def test_every_packaged_provider_is_reported() -> None:
    """The map is the directory — a provider file cannot be shipped and invisible."""
    reported = {key.provider for key in list_provider_keys()}
    assert reported == _packaged_provider_names()


def test_a_record_carries_the_env_var_the_toml_declares() -> None:
    keys = {key.provider: key for key in list_provider_keys()}
    assert keys["anthropic"].api_key_env == "ANTHROPIC_API_KEY"
    assert keys["huggingface"].api_key_env == "HF_TOKEN"


def test_required_follows_api_key_required() -> None:
    """A local provider needs no key; a cloud one does. Both are declared, not guessed."""
    keys = {key.provider: key for key in list_provider_keys()}
    assert keys["anthropic"].required is True
    assert keys["ollama"].required is False


def test_records_are_ordered_and_frozen() -> None:
    keys = list_provider_keys()
    assert [key.provider for key in keys] == sorted(key.provider for key in keys)
    assert isinstance(keys, tuple)


def test_the_public_name_is_exported() -> None:
    """Surfaces import ``arcllm.list_provider_keys``, not a private config helper."""
    assert arcllm.list_provider_keys() == list_provider_keys()
    assert "list_provider_keys" in arcllm.__all__
    assert "ProviderKey" in arcllm.__all__
