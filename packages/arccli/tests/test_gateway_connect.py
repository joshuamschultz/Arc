"""arc gateway connect-telegram — the arccli-owned part: agent DID resolution.

The wiring core is tested in arcgateway (test_connect.py); here we only cover the
CLI's job of reading the agent's DID from its arcagent.toml before delegating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arccli.commands.gateway_connect import _agent_did


def test_reads_did_from_arcagent_toml(tmp_path: Path) -> None:
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "sales"\n[identity]\ndid = "did:arc:local:executor/7e3e"\n',
        encoding="utf-8",
    )
    assert _agent_did(tmp_path) == "did:arc:local:executor/7e3e"


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"no arcagent\.toml"):
        _agent_did(tmp_path)


def test_missing_did_raises(tmp_path: Path) -> None:
    (tmp_path / "arcagent.toml").write_text('[agent]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        _agent_did(tmp_path)
