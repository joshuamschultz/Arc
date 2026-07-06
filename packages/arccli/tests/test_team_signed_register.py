"""FIX-1 (`arc team register`) + `_signer_for` error branches (SPEC-031).

``arc team register`` must register an entity under its PERSISTED identity —
the same Ed25519 key the running agent signs with — so the signed bus can
verify its messages. The previous code minted a throwaway keypair, leaving a
DID/key that never matched the agent's runtime identity.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from arccli.commands.team import _build_service, _init_cmd, _register, _signer_for

_MODEL = "anthropic/claude-sonnet-4-5-20250929"


def _init_root(tmp_path: Path) -> Path:
    _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
    return tmp_path


def _one_record(backend: Any) -> dict[str, Any]:
    records = asyncio.run(backend.query("messages/registry"))
    assert len(records) == 1, f"expected one registered entity, got {records}"
    return records[0]


def _scaffold_agent(tmp_path: Path, name: str) -> Path:
    """Scaffold an agent (config only, no auto-register) and return its dir."""
    from arccli.commands.agent.create import _create

    _create(
        argparse.Namespace(
            name=name,
            parent_dir=str(tmp_path),
            model=_MODEL,
            no_register=True,
        )
    )
    return tmp_path / name


def _agent_identity(agent_dir: Path) -> Any:
    from arcagent.core.config import load_config
    from arctrust import AgentIdentity

    config_path = agent_dir / "arcagent.toml"
    config = load_config(config_path)
    return AgentIdentity.from_config(
        config.identity,
        org=config.agent.org,
        agent_type=config.agent.type,
        config_path=config_path,
    )


class TestRegisterPersistedIdentity:
    def test_register_agent_key_verifies_its_signature(
        self, tmp_path: Path, team_backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        root = _init_root(tmp_path)
        agent_dir = _scaffold_agent(tmp_path, "worker")

        _register(
            argparse.Namespace(
                root=str(root),
                entity_id="agent://worker",
                name="Worker",
                entity_type="agent",
                roles="",
                workspace=str(agent_dir / "workspace"),
            )
        )

        record = _one_record(team_backend)
        assert record["public_key"], "registered agent must carry a verify key"

        from arcteam.crypto import MessageSigner, sign_message, verify_message
        from arcteam.types import Message

        signer = MessageSigner.from_identity(_agent_identity(agent_dir))
        assert signer.did == record["did"], "registered DID must match the signing key"
        message = Message(
            sender="agent://worker",
            to=["agent://peer"],
            body="signed by the persisted agent key",
            signer_did=signer.did,
            nonce="nonce-1",
        )
        sign_message(message, signer.private_key)
        assert verify_message(message, bytes.fromhex(record["public_key"])) is True

    def test_register_user_has_public_key(
        self, tmp_path: Path, team_backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        root = _init_root(tmp_path)
        _register(
            argparse.Namespace(
                root=str(root),
                entity_id="user://alice",
                name="Alice",
                entity_type="user",
                roles="",
                workspace=None,
            )
        )
        record = _one_record(team_backend)
        assert record["public_key"], "registered user must carry a verify key"
        assert record["did"].startswith("did:arc:")


class TestSignerForErrorBranches:
    def _registry(self, root: Path) -> Any:
        _, registry, _, _ = asyncio.run(_build_service(root))
        return registry

    def test_unknown_sender_raises_unknown_handle(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        from arcteam.registry import UnknownHandle

        registry = self._registry(_init_root(tmp_path))
        with pytest.raises(UnknownHandle):
            asyncio.run(_signer_for(registry, "agent://ghost"))

    def test_entity_without_workspace_raises_value_error(
        self, tmp_path: Path, team_backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        root = _init_root(tmp_path)
        _register(
            argparse.Namespace(
                root=str(root),
                entity_id="user://alice",
                name="Alice",
                entity_type="user",
                roles="",
                workspace=None,
            )
        )
        registry = self._registry(root)
        with pytest.raises(ValueError, match="workspace"):
            asyncio.run(_signer_for(registry, "user://alice"))

    def test_verify_only_identity_raises_value_error(
        self, tmp_path: Path, team_backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A verify-only identity has no seed; ``from_identity`` must reject it."""
        monkeypatch.setenv("HOME", str(tmp_path))
        root = _init_root(tmp_path)
        agent_dir = _scaffold_agent(tmp_path, "worker")
        _register(
            argparse.Namespace(
                root=str(root),
                entity_id="agent://worker",
                name="Worker",
                entity_type="agent",
                roles="",
                workspace=str(agent_dir / "workspace"),
            )
        )

        import arctrust

        real_identity = _agent_identity(agent_dir)
        verify_only = arctrust.AgentIdentity(
            did=real_identity.did, public_key=real_identity.public_key
        )
        monkeypatch.setattr(
            arctrust.AgentIdentity, "from_config", classmethod(lambda cls, *a, **k: verify_only)
        )

        registry = self._registry(root)
        with pytest.raises(ValueError):
            asyncio.run(_signer_for(registry, "agent://worker"))
