"""arc gateway connect-telegram — token storage + per-agent block wiring.

The guided command's testable core: bind a Telegram bot to one agent without any
prompts. Proves the token lands in the env file (0600) and NOT the config, and that
the gateway block is per-agent (platform="telegram", the agent's DID, allowlist).
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from arccli.commands.gateway_connect import connect_telegram

_GOOD_TOKEN = "8012345678:AAExampleExampleExampleExampleExample1"


def _agent(root: Path, name: str = "sales_agent", did: str = "did:arc:local:executor/7e3e") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\n[identity]\ndid = "{did}"\n', encoding="utf-8"
    )
    return d


def test_connect_writes_token_to_env_not_config(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    gw = tmp_path / "gateway.toml"
    env = tmp_path / "arc.env"
    result = connect_telegram(
        agent_dir=agent, token=_GOOD_TOKEN, user_id=8293394811, gateway_config=gw, env_file=env
    )
    # token in env, referenced by name from config
    assert f"{result['token_env']}={_GOOD_TOKEN}" in env.read_text()
    assert _GOOD_TOKEN not in gw.read_text()  # NEVER in the config
    # env file is owner-only
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_connect_writes_per_agent_block_bound_to_did(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    gw = tmp_path / "gateway.toml"
    connect_telegram(
        agent_dir=agent,
        token=_GOOD_TOKEN,
        user_id=8293394811,
        gateway_config=gw,
        env_file=tmp_path / "arc.env",
    )
    cfg = tomllib.loads(gw.read_text())
    block = cfg["platforms"]["sales_agent_telegram"]
    assert block["platform"] == "telegram"  # multi-bot: reuses the telegram plugin
    assert block["enabled"] is True
    assert block["agent_did"] == "did:arc:local:executor/7e3e"
    assert block["allowed_user_ids"] == [8293394811]
    assert cfg["security"]["require_pairing"] is True


def test_connect_preserves_existing_bot_block(tmp_path: Path) -> None:
    """A second agent's bot must not clobber the first (1 bot per agent, N per fleet)."""
    gw = tmp_path / "gateway.toml"
    gw.write_text(
        '[platforms.telegram]\nenabled = true\ntoken_env = "TELEGRAM_BOT_TOKEN"\n',
        encoding="utf-8",
    )
    connect_telegram(
        agent_dir=_agent(tmp_path, "sales_agent", "did:sales"),
        token=_GOOD_TOKEN,
        user_id=1,
        gateway_config=gw,
        env_file=tmp_path / "arc.env",
    )
    cfg = tomllib.loads(gw.read_text())
    assert "telegram" in cfg["platforms"]  # the original bot survives
    assert "sales_agent_telegram" in cfg["platforms"]  # plus the new one


def test_connect_rejects_a_non_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Telegram bot token"):
        connect_telegram(
            agent_dir=_agent(tmp_path),
            token="not-a-real-token",
            user_id=1,
            gateway_config=tmp_path / "gateway.toml",
            env_file=tmp_path / "arc.env",
        )
    # nothing written on rejection
    assert not (tmp_path / "arc.env").exists()


def test_connect_requires_agent_did(tmp_path: Path) -> None:
    d = tmp_path / "bare_agent"
    d.mkdir()
    (d / "arcagent.toml").write_text('[agent]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        connect_telegram(
            agent_dir=d,
            token=_GOOD_TOKEN,
            user_id=1,
            gateway_config=tmp_path / "gateway.toml",
            env_file=tmp_path / "arc.env",
        )
