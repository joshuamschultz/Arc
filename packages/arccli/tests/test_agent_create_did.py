"""`arc agent create` auto-registers on DID-keyed identity (REQ-003).

A freshly-created agent must be registered with a real cryptographic DID
sourced from arctrust and a handle equal to its name, so it is immediately
addressable — fixing the `sender_unauthorized` DLQ bug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    yield fake_home


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


def _registry_records(home: Path) -> list[dict]:
    reg_dir = home / ".arc" / "team" / "messages" / "registry"
    if not reg_dir.is_dir():
        return []
    return [json.loads(p.read_text()) for p in reg_dir.glob("*.json")]


def _create_agent(tmp_path: Path, name: str) -> None:
    """Scaffold an agent in-process so the injected backend sees the register."""
    from arccli.commands.agent.create import _create

    _create(
        argparse.Namespace(
            name=name,
            parent_dir=str(tmp_path),
            model="anthropic/claude-sonnet-4-5-20250929",
            no_register=False,
        )
    )


class TestCreateRegistersWithDid:
    def test_registered_entity_has_did_and_handle(self, tmp_path, isolated_home, team_backend: Any):
        _create_agent(tmp_path, "researcher")

        records = asyncio.run(team_backend.query("messages/registry"))
        assert len(records) == 1
        rec = records[0]
        assert rec["did"].startswith("did:arc:"), f"expected real DID, got {rec.get('did')!r}"
        assert rec["handle"] == "researcher"

    def test_config_did_matches_registry_did(self, tmp_path, isolated_home, team_backend: Any):
        """The registered DID is the SAME DID the agent uses at startup.

        `AgentIdentity.from_config` persists the minted DID into the agent's
        arcagent.toml; registration must use that identity, not invent one.
        """
        import tomllib

        _create_agent(tmp_path, "researcher")
        cfg = tomllib.loads((tmp_path / "researcher" / "arcagent.toml").read_text())
        config_did = cfg["identity"]["did"]
        assert config_did.startswith("did:arc:")

        records = asyncio.run(team_backend.query("messages/registry"))
        assert records[0]["did"] == config_did


def _agent_identity(tmp_path: Path, name: str) -> Any:
    """Load the created agent's persisted signing identity from its config."""
    from arcagent.core.config import load_config
    from arctrust import AgentIdentity

    config_path = tmp_path / name / "arcagent.toml"
    config = load_config(config_path)
    return AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=config_path,
    )


class TestCreateRegistersVerifiableKey:
    """FIX-1: the registered record must carry the agent's real verify key.

    Without ``public_key``, the signed bus quarantines every message from a
    real agent as ``bad_signature`` — the entity is registered but unverifiable.
    """

    def test_registered_public_key_verifies_agent_signature(
        self, tmp_path, isolated_home, team_backend: Any
    ):
        from arcteam.crypto import MessageSigner, sign_message, verify_message
        from arcteam.types import Message

        _create_agent(tmp_path, "researcher")

        records = asyncio.run(team_backend.query("messages/registry"))
        record = records[0]
        assert record["public_key"], "registered entity must carry a verify key"

        # The agent signs with the SAME identity it registered under.
        signer = MessageSigner.from_identity(_agent_identity(tmp_path, "researcher"))
        message = Message(
            sender="agent://researcher",
            to=["agent://peer"],
            body="signed by the real agent key",
            signer_did=signer.did,
            nonce="nonce-1",
        )
        sign_message(message, signer.private_key)

        # This is the exact bug: the registered key must verify that signature.
        assert verify_message(message, bytes.fromhex(record["public_key"])) is True
