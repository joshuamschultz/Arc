"""SPEC-064 T-004 + T-010 — ``arc keys``, and the provider map that now has one home.

Two things are pinned here.

**The verb cannot be handed a credential on argv.** ``arc keys set`` prompts with
``getpass`` and deliberately has no ``--value`` flag, because a credential on argv
lands in shell history and in the process table — the same line
``arc connector add`` holds (D-555). ``test_set_has_no_flag_that_accepts_a_value``
asserts the parser *rejects* one, so the flag cannot be reintroduced quietly.

**There is one provider→env-var map.** ``arccli.commands.init`` used to carry its
own copy of eight pairs while arcllm packaged sixteen providers, so ``arc init
--provider xai`` could not name the variable it needed. The copy is gone; the tests
below assert the module defines no such mapping and that ``arc init`` still names
the right variable for a provider arcllm declares (D-581).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from arcagent.keys import KeyStore

from arccli.commands import init as init_module
from arccli.commands.keys import _build_parser, keys_handler

_KEY_VALUE = "sk-ant-api03-do-not-leak-me-4f2c9e"


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    """An isolated Arc config home, so no test touches the operator's real one."""
    path = tmp_path / "arc"
    path.mkdir()
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def paths(arc_dir: Path, data_dir: Path) -> list[str]:
    return ["--arc-dir", str(arc_dir), "--data-dir", str(data_dir)]


def _entries(arc_dir: Path) -> dict[str, str]:
    env_file = arc_dir / ".env"
    if not env_file.exists():
        return {}
    return dict(
        line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if line
    )


# ---------------------------------------------------------------------------
# set — hidden prompt, and no way to pass a value on the command line
# ---------------------------------------------------------------------------


def test_set_has_no_flag_that_accepts_a_value() -> None:
    """A credential on argv is shell history and a process-table entry (D-555)."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["set", "anthropic", "--value", _KEY_VALUE])


def test_set_prompts_with_getpass_and_never_input(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str], capsys: Any
) -> None:
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": _KEY_VALUE)
    monkeypatch.setattr(
        "builtins.input", lambda *_: pytest.fail("a key must never be read with input()")
    )

    keys_handler(["set", "anthropic", *paths])

    assert _entries(arc_dir) == {"ANTHROPIC_API_KEY": _KEY_VALUE}
    captured = capsys.readouterr()
    assert _KEY_VALUE not in captured.out
    assert _KEY_VALUE not in captured.err


def test_set_resolves_the_env_var_from_arcllm(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str]
) -> None:
    """A provider arccli never hardcoded still resolves, because arcllm declares it."""
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": "xai-value")
    keys_handler(["set", "xai", *paths])
    assert _entries(arc_dir) == {"XAI_API_KEY": "xai-value"}


def test_set_names_an_unknown_provider_and_exits_one(
    arc_dir: Path, paths: list[str], capsys: Any
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        keys_handler(["set", "not_a_provider", *paths])
    assert excinfo.value.code == 1
    assert "not_a_provider" in capsys.readouterr().err


def test_an_empty_prompt_is_refused_without_writing(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str], capsys: Any
) -> None:
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": "")
    with pytest.raises(SystemExit) as excinfo:
        keys_handler(["set", "anthropic", *paths])
    assert excinfo.value.code == 1
    assert _entries(arc_dir) == {}
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# list — presence, never a value
# ---------------------------------------------------------------------------


def test_list_shows_status_and_never_the_value(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str], capsys: Any
) -> None:
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": _KEY_VALUE)
    keys_handler(["set", "anthropic", *paths])
    capsys.readouterr()

    keys_handler(["list", *paths])
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out
    assert "OPENAI_API_KEY" in out
    assert _KEY_VALUE not in out


def test_list_json_is_scriptable_and_carries_no_value(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str], capsys: Any
) -> None:
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": _KEY_VALUE)
    keys_handler(["set", "anthropic", *paths])
    capsys.readouterr()

    keys_handler(["list", "--json", *paths])
    out = capsys.readouterr().out
    payload = json.loads(out)
    by_provider = {row["provider"]: row for row in payload}
    assert by_provider["anthropic"]["present"] is True
    assert by_provider["openai"]["present"] is False
    assert _KEY_VALUE not in out


# ---------------------------------------------------------------------------
# remove — never an error when there is nothing to remove
# ---------------------------------------------------------------------------


def test_remove_is_not_an_error_when_nothing_was_stored(
    arc_dir: Path, paths: list[str], capsys: Any
) -> None:
    keys_handler(["remove", "anthropic", *paths])
    assert "anthropic" in capsys.readouterr().out


def test_remove_drops_the_stored_key(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str]
) -> None:
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": _KEY_VALUE)
    keys_handler(["set", "anthropic", *paths])
    keys_handler(["remove", "anthropic", *paths])
    assert _entries(arc_dir) == {}


# ---------------------------------------------------------------------------
# The verb is registered, and it writes where every other surface reads
# ---------------------------------------------------------------------------


def test_keys_is_in_the_command_registry() -> None:
    from arccli.commands.registry import COMMAND_REGISTRY

    assert any(command.name == "keys" for command in COMMAND_REGISTRY)


def test_the_cli_and_the_key_store_agree_on_the_file(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, paths: list[str]
) -> None:
    """One resolver: a key set by the CLI is present to any surface using KeyStore."""
    import arccli.commands.keys as keys_module

    monkeypatch.setattr(keys_module.getpass, "getpass", lambda prompt="": _KEY_VALUE)
    keys_handler(["set", "anthropic", *paths])

    store = KeyStore(arc_dir / ".env")
    statuses = asyncio.run(store.list(caller_did="did:arc:local:operator"))
    assert next(s for s in statuses if s.provider == "anthropic").present is True


# ---------------------------------------------------------------------------
# arc init no longer keeps its own copy of the map
# ---------------------------------------------------------------------------


def test_init_defines_no_provider_env_map() -> None:
    """D-581 — arcllm declares which env var a provider reads; nothing else may."""
    for name in ("PROVIDER_ENV_VARS", "VALID_PROVIDERS", "_PROVIDER_ENV_VARS"):
        assert not hasattr(init_module, name), f"{name} is a second copy of arcllm's map"


def test_init_names_the_env_var_for_a_provider_only_arcllm_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    config_dir = tmp_path / "arc"

    init_module._init(
        argparse.Namespace(
            tier="personal",
            config_dir=str(config_dir),
            provider="xai",
            quick=True,
            blueprint=None,
            team=None,
        )
    )

    out = capsys.readouterr().out
    assert "XAI_API_KEY" in out


def test_init_still_refuses_a_provider_arcllm_does_not_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        init_module._init(
            argparse.Namespace(
                tier="personal",
                config_dir=str(tmp_path / "arc"),
                provider="not_a_provider",
                quick=True,
                blueprint=None,
                team=None,
            )
        )
    assert excinfo.value.code == 1
