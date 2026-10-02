"""Item 13 — the typed wake word: pure transcript matching and config validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arcgateway.adapters.voice.config import VoicePlatformConfig, WakeConfig
from arcgateway.adapters.voice.wake import match_wake


@pytest.mark.parametrize(
    ("transcript", "words", "mode", "expected"),
    [
        ("Olivia, what time is it", ["olivia"], "exact", True),
        ("what time is it olivia", ["olivia"], "exact", True),
        ("hey olivia turn it up", ["olivia"], "exact", True),
        ("ok olivia", ["hey olivia"], "exact", True),
        ("alivia what's the weather", ["olivia"], "fuzzy", True),
        ("alivia what's the weather", ["olivia"], "exact", False),
        ("olive oil is on sale", ["olivia"], "fuzzy", False),
        ("olive oil is on sale", ["olivia"], "exact", False),
        ("we talked to alivia yesterday at lunch", ["olivia"], "fuzzy", False),
        ("max do this", ["max"], "fuzzy", True),
        ("mix do this", ["max"], "fuzzy", False),
        ("", ["olivia"], "fuzzy", False),
        ("hello there", [], "fuzzy", False),
        ("computer lights on", ["computer", "olivia"], "exact", True),
    ],
)
def test_match_wake_table(transcript: str, words: list[str], mode: str, expected: bool) -> None:
    assert match_wake(transcript, words, mode) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize("word", ["olivia", "hey olivia", "o'brien", "ab"])
def test_valid_wake_words_are_accepted(word: str) -> None:
    assert WakeConfig(words=[word]).words == [word]


@pytest.mark.parametrize(
    "word",
    ["a", "x" * 33, "olivia1", "oli:via", 'oli"via', "oli\nvia", "../etc", "ol/ivia", "'", "  "],
)
def test_invalid_wake_words_are_refused(word: str) -> None:
    with pytest.raises(ValidationError):
        WakeConfig(words=[word])


def test_wake_words_are_lowercased_and_capped_at_three() -> None:
    assert WakeConfig(words=["Olivia"]).words == ["olivia"]
    with pytest.raises(ValidationError):
        WakeConfig(words=["aa", "bb", "cc", "dd"])


def test_unknown_wake_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        WakeConfig.model_validate({"words": ["olivia"], "evil": 1})


def test_platform_config_defaults_listening_on_and_empty_words() -> None:
    cfg = VoicePlatformConfig.model_validate({"enabled": True})
    assert cfg.listening is True
    assert cfg.wake.words == []
