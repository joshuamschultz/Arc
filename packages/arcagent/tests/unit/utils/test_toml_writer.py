"""Unit tests for the shared TOML emitter (`arcagent.utils.toml_writer`)."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

from arcagent.utils.toml_writer import dumps_toml


def test_scalar_types_emit_toml_literals() -> None:
    out = dumps_toml({"on": True, "off": False, "count": 3, "ratio": 1.5, "name": "arc"})

    assert out == 'on = true\noff = false\ncount = 3\nratio = 1.5\nname = "arc"\n'


def test_list_values_emit_inline_arrays() -> None:
    out = dumps_toml({"ids": [1, 2], "tags": ["a", "b"], "flags": [True], "empty": []})

    assert out == 'ids = [1, 2]\ntags = ["a", "b"]\nflags = [true]\nempty = []\n'


def test_strings_escape_backslashes_and_quotes() -> None:
    out = dumps_toml({"path": 'C:\\a\\"b"'})

    assert out == 'path = "C:\\\\a\\\\\\"b\\""\n'
    assert tomllib.loads(out)["path"] == 'C:\\a\\"b"'


def test_nested_tables_get_dotted_headers_with_scalars_first() -> None:
    data: dict[str, Any] = {
        "top": 1,
        "platforms": {"web": {"enabled": True}, "cli": {"enabled": False}},
    }

    assert dumps_toml(data) == (
        "top = 1\n"
        "[platforms]\n"
        "\n"
        "[platforms.web]\n"
        "enabled = true\n"
        "\n"
        "[platforms.cli]\n"
        "enabled = false\n"
    )


def test_deeply_nested_tables_round_trip_through_tomllib() -> None:
    data: dict[str, Any] = {
        "security": {"require_pairing": True},
        "platforms": {
            "josh_telegram": {
                "enabled": True,
                "platform": "telegram",
                "token_env": "TELEGRAM_BOT_TOKEN_JOSH",
                "allowed_user_ids": [42, 43],
                "limits": {"max_turns": 10, "budget": 2.5},
            }
        },
    }

    assert tomllib.loads(dumps_toml(data)) == data


def test_empty_table_emits_a_single_newline() -> None:
    assert dumps_toml({}) == "\n"


def test_output_is_deterministic_across_calls() -> None:
    data: dict[str, Any] = {"a": 1, "b": {"c": "x", "d": [1, 2]}, "e": {"f": {"g": True}}}

    assert dumps_toml(data) == dumps_toml(data)
    assert dumps_toml(data) == dumps_toml(dict(data))


def test_key_insertion_order_drives_output_order() -> None:
    assert dumps_toml({"a": 1, "b": 2}) == "a = 1\nb = 2\n"
    assert dumps_toml({"b": 2, "a": 1}) == "b = 2\na = 1\n"


#: Keys a bare-key emitter would write straight into a file that no longer parses.
#: The space is the one an operator actually typed — ``blackarc industrial email``
#: took a whole agent off a live fleet — and the rest are the neighbouring shapes
#: the same emitter would mangle just as silently.
_AWKWARD_KEYS = [
    "blackarc industrial email",
    "dotted.name",
    'quoted"name',
    "back\\slash",
    "dash-name",
    "UPPER",
    "hash#name",
    "[bracket]",
    "equals=name",
    "new\nline",
    "",
]


@pytest.mark.parametrize("key", _AWKWARD_KEYS)
def test_a_key_needing_quotes_round_trips_as_a_scalar_key(key: str) -> None:
    """An emitter that can write a file which will not re-parse is a landmine.

    Whoever validates upstream, no caller may be able to produce a config that
    ``tomllib`` then refuses — that is an agent that will not start.
    """
    document: dict[str, Any] = {key: "google_workspace"}

    assert tomllib.loads(dumps_toml(document)) == document


@pytest.mark.parametrize("key", _AWKWARD_KEYS)
def test_a_key_needing_quotes_round_trips_as_a_table_header(key: str) -> None:
    """The shape the connector install writes: ``[extensions.<instance>]``."""
    document: dict[str, Any] = {
        "extensions": {key: {"extension": "google_workspace", "approval": "outbound"}}
    }

    assert tomllib.loads(dumps_toml(document)) == document


def test_a_bare_key_is_still_written_bare() -> None:
    """Quoting only what needs it: every existing config keeps its current bytes."""
    assert dumps_toml({"extensions": {"work_email": {"approval": "outbound"}}}) == (
        '[extensions]\n\n[extensions.work_email]\napproval = "outbound"\n'
    )


def test_unsupported_value_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported TOML value type: NoneType"):
        dumps_toml({"nothing": None})


def test_unsupported_value_inside_a_list_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported TOML value type: set"):
        dumps_toml({"items": [{1, 2}]})
