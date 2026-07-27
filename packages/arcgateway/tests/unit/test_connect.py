"""arcgateway.connect — the shared Telegram wiring core (used by arccli + arcui).

Proves the token lands in the env file (0600) and NOT the config, that the block is
per-agent (platform="telegram", the agent DID, allowlist), that a second agent's bot
does not clobber the first (1 bot per agent, N per fleet), and that a malformed token
writes nothing.
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from arcgateway.connect import connect_telegram

_GOOD_TOKEN = "8012345678:AAExampleExampleExampleExampleExample1"


def test_token_to_env_not_config(tmp_path: Path) -> None:
    gw, env = tmp_path / "gateway.toml", tmp_path / "arc.env"
    result = connect_telegram(
        agent_slug="sales_agent",
        agent_did="did:arc:local:executor/7e3e",
        token=_GOOD_TOKEN,
        user_id=8293394811,
        gateway_config=gw,
        env_file=env,
    )
    assert f"{result['token_env']}={_GOOD_TOKEN}" in env.read_text()
    assert _GOOD_TOKEN not in gw.read_text()  # NEVER in the config
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_per_agent_block_bound_to_did(tmp_path: Path) -> None:
    gw = tmp_path / "gateway.toml"
    connect_telegram(
        agent_slug="sales_agent",
        agent_did="did:arc:local:executor/7e3e",
        token=_GOOD_TOKEN,
        user_id=8293394811,
        gateway_config=gw,
        env_file=tmp_path / "arc.env",
    )
    block = tomllib.loads(gw.read_text())["platforms"]["sales_agent_telegram"]
    assert block["platform"] == "telegram"
    assert block["enabled"] is True
    assert block["agent_did"] == "did:arc:local:executor/7e3e"
    assert block["allowed_user_ids"] == [8293394811]
    assert tomllib.loads(gw.read_text())["security"]["require_pairing"] is True


def test_second_agent_does_not_clobber_first(tmp_path: Path) -> None:
    gw = tmp_path / "gateway.toml"
    gw.write_text(
        '[platforms.telegram]\nenabled = true\ntoken_env = "TELEGRAM_BOT_TOKEN"\n', encoding="utf-8"
    )
    connect_telegram(
        agent_slug="sales_agent",
        agent_did="did:sales",
        token=_GOOD_TOKEN,
        user_id=1,
        gateway_config=gw,
        env_file=tmp_path / "arc.env",
    )
    platforms = tomllib.loads(gw.read_text())["platforms"]
    assert "telegram" in platforms and "sales_agent_telegram" in platforms


def test_reconnect_replaces_block_no_duplicate(tmp_path: Path) -> None:
    gw, env = tmp_path / "gateway.toml", tmp_path / "arc.env"
    for _ in range(2):
        connect_telegram(
            agent_slug="sales_agent",
            agent_did="did:sales",
            token=_GOOD_TOKEN,
            user_id=1,
            gateway_config=gw,
            env_file=env,
        )
    # A second connect must not produce a duplicate table (which would fail to parse).
    assert tomllib.loads(gw.read_text())  # parses cleanly


def test_rejects_non_token_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Telegram bot token"):
        connect_telegram(
            agent_slug="sales_agent",
            agent_did="did:x",
            token="nope",
            user_id=1,
            gateway_config=tmp_path / "gateway.toml",
            env_file=tmp_path / "arc.env",
        )
    assert not (tmp_path / "arc.env").exists()


def test_rejects_empty_did(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="agent DID"):
        connect_telegram(
            agent_slug="x",
            agent_did="  ",
            token=_GOOD_TOKEN,
            user_id=1,
            gateway_config=tmp_path / "gateway.toml",
            env_file=tmp_path / "arc.env",
        )
