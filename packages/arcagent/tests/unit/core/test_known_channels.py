"""Known delivery channels — a per-agent, workspace-persisted picker source.

Raw chat ids only exist on inbound events (pairings/sessions store hashes), so a
non-technical operator has no way to pick a schedule's delivery target. The turn
path records each interactive channel it sees (``platform:chat_id`` + a friendly
label) into ``<workspace>/channels.json`` so arcui can offer a dropdown instead
of a raw ``platform:chat_id`` box.
"""

from __future__ import annotations

import json
from pathlib import Path

from arcagent.core import known_channels


def _load(ws: Path) -> list[dict]:
    return json.loads((ws / "channels.json").read_text())


def test_record_creates_file_with_entry(tmp_path: Path) -> None:
    known_channels.record(tmp_path, target="telegram:123", label="Telegram — Josh")
    entries = _load(tmp_path)
    assert entries[0]["target"] == "telegram:123"
    assert entries[0]["label"] == "Telegram — Josh"
    assert entries[0]["last_seen"]


def test_record_dedupes_by_target_newest_first(tmp_path: Path) -> None:
    known_channels.record(tmp_path, target="telegram:1", label="A")
    known_channels.record(tmp_path, target="telegram:2", label="B")
    known_channels.record(tmp_path, target="telegram:1", label="A2")
    entries = _load(tmp_path)
    assert [e["target"] for e in entries] == ["telegram:1", "telegram:2"]
    # Re-seen target moves to front and refreshes its label.
    assert entries[0]["label"] == "A2"


def test_record_caps_history(tmp_path: Path) -> None:
    for i in range(known_channels.MAX_CHANNELS + 5):
        known_channels.record(tmp_path, target=f"telegram:{i}", label=str(i))
    entries = _load(tmp_path)
    assert len(entries) == known_channels.MAX_CHANNELS


def test_record_ignores_blank_target(tmp_path: Path) -> None:
    known_channels.record(tmp_path, target="", label="x")
    assert not (tmp_path / "channels.json").exists()


def test_list_channels_returns_target_and_label(tmp_path: Path) -> None:
    known_channels.record(tmp_path, target="telegram:1", label="Telegram — Josh")
    out = known_channels.list_channels(tmp_path)
    assert out == [{"target": "telegram:1", "label": "Telegram — Josh"}]


def test_list_channels_missing_file_is_empty(tmp_path: Path) -> None:
    assert known_channels.list_channels(tmp_path) == []


def test_list_channels_corrupt_file_is_empty(tmp_path: Path) -> None:
    (tmp_path / "channels.json").write_text("{ not json")
    assert known_channels.list_channels(tmp_path) == []
