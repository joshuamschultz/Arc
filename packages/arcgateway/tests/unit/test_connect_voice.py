"""arcgateway.connect — voice listening/wake persistence (item 13)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from arcgateway.connect import connect_voice, set_voice_listening, set_voice_wake


def _connect(tmp_path: Path, **kwargs: object) -> Path:
    gw = tmp_path / "gateway.toml"
    connect_voice(
        agent_did="did:arc:olivia",
        gateway_config=gw,
        env_file=tmp_path / "arc.env",
        **kwargs,  # type: ignore[arg-type]
    )
    return gw


def _voice(gw: Path) -> dict[str, object]:
    return tomllib.loads(gw.read_text())["platforms"]["voice"]


def test_connect_writes_listening_on_and_default_wake_from_channel(tmp_path: Path) -> None:
    voice = _voice(_connect(tmp_path))
    assert voice["listening"] is True
    assert voice["wake"]["words"] == ["olivia"]  # type: ignore[index]
    assert voice["wake"]["mode"] == "stt"  # type: ignore[index]


def test_connect_accepts_typed_wake_words(tmp_path: Path) -> None:
    voice = _voice(_connect(tmp_path, wake_words=["computer", "hey jarvis"]))
    assert voice["wake"]["words"] == ["computer", "hey jarvis"]  # type: ignore[index]


def test_connect_rejects_bad_wake_word_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _connect(tmp_path, wake_words=["bad:word"])
    assert not (tmp_path / "gateway.toml").exists()


def test_reconnect_keeps_listening_off_and_existing_words(tmp_path: Path) -> None:
    gw = _connect(tmp_path, wake_words=["computer"])
    set_voice_listening(gateway_config=gw, on=False)
    _connect(tmp_path)  # rotate token / re-run connect
    voice = _voice(gw)
    assert voice["listening"] is False
    assert voice["wake"]["words"] == ["computer"]  # type: ignore[index]


def test_set_listening_persists(tmp_path: Path) -> None:
    gw = _connect(tmp_path)
    set_voice_listening(gateway_config=gw, on=False)
    assert _voice(gw)["listening"] is False


def test_set_wake_validates_and_persists(tmp_path: Path) -> None:
    gw = _connect(tmp_path)
    stored = set_voice_wake(gateway_config=gw, words=["Jarvis"], match="exact")
    assert stored.words == ["jarvis"]
    assert _voice(gw)["wake"]["match"] == "exact"  # type: ignore[index]
    with pytest.raises(ValueError, match="wake word"):
        set_voice_wake(gateway_config=gw, words=["no/good"])
    assert _voice(gw)["wake"]["words"] == ["jarvis"]  # type: ignore[index]


def test_set_without_connected_voice_is_a_plain_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="connect-voice"):
        set_voice_listening(gateway_config=tmp_path / "gateway.toml", on=True)
